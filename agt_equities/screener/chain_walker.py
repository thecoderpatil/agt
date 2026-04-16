"""
agt_equities.screener.chain_walker — Phase 5: IBKR option chain walker.

Sits between Phase 4 (vol/event armor) and Phase 6 (RAY filter, not
yet implemented). Takes the Phase 4 survivors that passed IVR +
corporate calendar gates and walks each ticker's option chain to
produce ALL valid CSP strike candidates across the two nearest
Friday expiries.

Per Yash ruling 2026-04-11: two nearest Fridays per ticker, no
weekly/monthly distinction, puts only (right='P'), walk strikes
OTM below spot with a floor at lowest_low_21d. No delta filter in
Phase 5 — that's deferred to Phase 6. Phase 5 produces a large
output (8 tickers × 2 expiries × ~10 strikes = ~160 StrikeCandidates
in the normal case), and Phase 6 picks winners by RAY.

ARCHITECTURE NOTES:

All IBKR interaction is routed through agt_equities.ib_chains.
chain_walker.py never calls ib_async directly (qualifyContractsAsync,
reqMktData, reqSecDefOptParamsAsync, etc.) — those happen inside
ib_chains.get_expirations() and ib_chains.get_chain_for_expiry().
This is a deliberate architectural split:

  - ib_chains.py owns the low-level IBKR semantics (qualification,
    snapshot timing, market data cancellation, error classification).
  - chain_walker.py owns the screener-specific logic (expiry
    selection, strike-band filtering by Phase 2 lowest_low_21d,
    mid/yield/OTM computation, StrikeCandidate construction).

The ib connection is INJECTED by the caller. chain_walker.py does
NOT establish its own connection — it receives a connected IB
object and passes it through to ib_chains. Tests inject a stub
IB and monkeypatch ib_chains entry points.

ISOLATION CONTRACT:
  Allowed imports:
    stdlib (asyncio, logging, time, datetime)
    ib_async                              (whitelisted from C1)
    agt_equities.ib_chains                (Option A per 2026-04-11)
    agt_equities.screener.{config, types} (intra-package)
  Forbidden: yfinance, httpx, pandas (unused here), numpy,
  anything in agt_equities.{walker, trade_repo, rule_engine,
  mode_engine, telegram_bot, market_data_dtos, providers.*},
  agt_deck.*, sqlite3.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from agt_equities.dates import et_today

import ib_async  # noqa: F401  (whitelisted import — ib is type-hinted below)

from agt_equities import ib_chains
from agt_equities.ib_chains import IBKRChainError
from agt_equities.screener import config
from agt_equities.screener.types import (
    StrikeCandidate,
    VolArmorCandidate,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strike band floor helpers — pure functions, no I/O (C6.1)
# ---------------------------------------------------------------------------

def _expected_strike_interval(spot: float) -> float:
    """Return a CONSERVATIVE strike interval estimate for a price level.

    Intentionally overestimates interval width — real-world option
    chains often trade on wider grids than OCC theoretical rules
    suggest, especially for illiquid mid-caps. Being conservative
    guarantees the Phase 5 strike band contains multiple walkable
    strikes regardless of actual chain density.

    The boundaries (25, 100) use strict `<` so spot == 25 falls into
    the UNDER_100 bucket and spot == 100 falls into the 100_PLUS
    bucket. Per the dispatch spec.

    Returns the estimated interval in dollars.
    """
    if spot < 25:
        return config.STRIKE_INTERVAL_UNDER_25
    elif spot < 100:
        return config.STRIKE_INTERVAL_UNDER_100
    else:
        return config.STRIKE_INTERVAL_100_PLUS


def _compute_strike_band_floor(spot: float) -> float:
    """Compute the Phase 5 strike band lower bound.

    Returns spot - (expected_interval × CHAIN_WALKER_MIN_STRIKES_IN_BAND).
    Does NOT consult lowest_low_21d — that field is intentionally
    carried forward as metadata on the StrikeCandidate/RAYCandidate
    dataclass chain but NOT used as a strike filter per Architect
    ruling 2026-04-11.

    Guarantees the resulting band [floor, spot] always contains at
    least CHAIN_WALKER_MIN_STRIKES_IN_BAND strike increments, even
    for tight-pullback names where spot is close to recent lows.

    Returns float dollars. May return a negative value for pathological
    inputs (spot < interval × min_strikes), in which case the floor
    effectively becomes "every listed put strike" — that is acceptable
    because IBKR's reqSecDefOptParams will not return negative strikes.
    """
    interval = _expected_strike_interval(spot)
    floor = spot - (interval * config.CHAIN_WALKER_MIN_STRIKES_IN_BAND)
    return floor


# ---------------------------------------------------------------------------
# Expiry selection helper — pure function, no I/O
# ---------------------------------------------------------------------------

def _select_friday_expiries(
    raw_expirations: list[str],
    *,
    min_dte: int,
    max_dte: int,
    count: int,
    today: date,
) -> list[tuple[str, int]]:
    """Filter raw expirations to the nearest N future Friday expiries
    within the [min_dte, max_dte] inclusive window.

    Args:
        raw_expirations: list of YYYY-MM-DD strings from get_expirations().
            Already sorted, already filtered to future dates.
        min_dte: minimum days-to-expiration (inclusive floor)
        max_dte: maximum days-to-expiration (inclusive ceiling)
        count: maximum number of expiries to return
        today: date reference for DTE computation (injected for testability)

    Returns:
        List of (expiry_string, dte) tuples, at most `count` entries.
        Empty list if no expirations match the filter.
    """
    selected: list[tuple[str, int]] = []
    for exp_str in raw_expirations:
        try:
            exp_date = date.fromisoformat(exp_str)
        except ValueError:
            continue
        if exp_date.weekday() != 4:  # 0=Mon, 4=Fri
            continue
        dte = (exp_date - today).days
        if dte < min_dte or dte > max_dte:
            continue
        selected.append((exp_str, dte))
        if len(selected) >= count:
            break
    return selected


# ---------------------------------------------------------------------------
# Per-strike filter + StrikeCandidate construction — pure function
# ---------------------------------------------------------------------------

def _row_to_strike_candidate(
    upstream: VolArmorCandidate,
    row: dict,
    *,
    expiry: str,
    dte: int,
) -> StrikeCandidate | None:
    """Convert one ib_chains row to a StrikeCandidate.

    Returns None if the row fails any per-strike guard (strike <= 0,
    mid < CHAIN_WALKER_MIN_MID, or dte <= 0 guard). The caller
    increments n_strikes_walked regardless of whether a candidate
    is produced, so filtered rows are visible in the phase totals.
    """
    try:
        strike_val = float(row.get("strike") or 0.0)
        bid = float(row.get("bid") or 0.0)
        ask = float(row.get("ask") or 0.0)
        last = float(row.get("last") or 0.0)
        volume = int(row.get("volume") or 0)
        open_interest = int(row.get("openInterest") or 0)
        implied_vol = float(row.get("impliedVol") or 0.0)
    except (TypeError, ValueError):
        return None

    if strike_val <= 0:
        return None

    mid = (bid + ask) / 2.0
    if mid < config.CHAIN_WALKER_MIN_MID:
        return None

    if dte <= 0:
        # Defensive: caller already filtered by CHAIN_WALKER_MIN_DTE,
        # but a 0-dte expiry would divide-by-zero below.
        return None

    # Annualized yield = (premium / capital at risk) * (365 / dte) * 100
    # For cash-secured puts, capital at risk ≈ strike (ignoring credit).
    annualized_yield = (mid / strike_val) * (365.0 / dte) * 100.0

    # OTM percentage relative to spot (positive for OTM puts)
    if upstream.spot <= 0:
        return None  # defensive; upstream Phase 2 guarantees spot > 0
    otm_pct = (upstream.spot - strike_val) / upstream.spot * 100.0

    return StrikeCandidate.from_vol_armor(
        upstream,
        expiry=expiry,
        dte=dte,
        strike=strike_val,
        bid=bid,
        ask=ask,
        mid=mid,
        last=last,
        volume=volume,
        open_interest=open_interest,
        implied_vol=implied_vol,
        annualized_yield=annualized_yield,
        otm_pct=otm_pct,
    )


# ---------------------------------------------------------------------------
# Phase 5 orchestrator
# ---------------------------------------------------------------------------

async def run_phase_5(
    candidates: list[VolArmorCandidate],
    ib: "ib_async.IB",
) -> list[StrikeCandidate]:
    """Execute Phase 5: walk option chains for each candidate.

    Args:
        candidates: Phase 4 output (VolArmorCandidate list).
        ib: connected ib_async.IB instance. Passed directly to
            ib_chains.get_expirations / get_chain_for_expiry.

    Returns:
        List of StrikeCandidate — one per valid (ticker, expiry, strike)
        triplet. Can be large (~160-320 entries for 8 tickers × 2
        expiries × 10-20 strikes). Phase 6 (RAY filter) narrows to
        the final hit list.
    """
    start_ts = time.monotonic()

    if not candidates:
        logger.info(
            "[screener.chain_walker] Phase 5: empty candidates list, "
            "returning empty result"
        )
        return []

    total_tickers = len(candidates)

    logger.info(
        "[screener.chain_walker] Phase 5 (IBKR option chain walk): "
        "starting with %d candidates", total_tickers,
    )

    n_no_expiries = 0
    n_chain_fetch_failed = 0
    n_strikes_walked = 0
    n_strikes_kept = 0
    n_ticker_zero_survivors = 0

    survivors: list[StrikeCandidate] = []
    today = et_today()

    for candidate in candidates:
        ticker = candidate.ticker

        # ── Step A: fetch expirations ──────────────────────────
        # Wrap in try/except — IBKRChainError drops the ticker entirely.
        try:
            raw_expirations = await ib_chains.get_expirations(ib, ticker)
        except IBKRChainError as exc:
            logger.warning(
                "[screener.chain_walker] TICKER_DROPPED_PHASE5_EXPIRIES_FAILED "
                "ticker=%s error=%s", ticker, exc,
            )
            n_no_expiries += 1
            continue
        except Exception as exc:
            # Defensive — any unexpected exception treated the same
            logger.warning(
                "[screener.chain_walker] TICKER_DROPPED_PHASE5_EXPIRIES_FAILED "
                "ticker=%s error_class=%s error=%s",
                ticker, type(exc).__name__, exc,
            )
            n_no_expiries += 1
            continue

        # ── Step B: select nearest N Friday expiries in DTE window ──
        selected = _select_friday_expiries(
            raw_expirations,
            min_dte=config.CHAIN_WALKER_MIN_DTE,
            max_dte=config.CHAIN_WALKER_MAX_DTE,
            count=config.CHAIN_WALKER_EXPIRY_COUNT,
            today=today,
        )

        if not selected:
            logger.info(
                "[screener.chain_walker] TICKER_DROPPED_PHASE5_NO_VALID_EXPIRIES "
                "ticker=%s raw_expirations_count=%d",
                ticker, len(raw_expirations),
            )
            n_no_expiries += 1
            continue

        # ── Step C/D: walk each selected expiry ───────────────
        # C6.1: strike band lower bound is now interval-based, not
        # lowest_low_21d-based. lowest_low_21d is still carried forward
        # in the dataclass chain (VolArmorCandidate → StrikeCandidate →
        # RAYCandidate) but is NOT used as a filter here. Per Architect
        # ruling 2026-04-11 following the paper-run CHRW failure where
        # spot was only 1.9% above the 21-day low and the resulting
        # band was too narrow for IBKR's actual strike grid.
        strike_floor = _compute_strike_band_floor(candidate.spot)

        ticker_strike_count = 0
        for idx, (expiry_str, dte) in enumerate(selected):
            try:
                chain_rows = await ib_chains.get_chain_for_expiry(
                    ib,
                    ticker,
                    expiry_str,
                    right="P",
                    min_strike=strike_floor,
                    max_strike=candidate.spot,
                )
            except IBKRChainError as exc:
                logger.warning(
                    "[screener.chain_walker] TICKER_DROPPED_PHASE5_CHAIN_FETCH_FAILED "
                    "ticker=%s expiry=%s error=%s",
                    ticker, expiry_str, exc,
                )
                n_chain_fetch_failed += 1
                # Continue to the NEXT expiry — don't abort the ticker
                # just because one expiry failed. The other expiry may
                # still succeed.
                continue
            except Exception as exc:
                logger.warning(
                    "[screener.chain_walker] TICKER_DROPPED_PHASE5_CHAIN_FETCH_FAILED "
                    "ticker=%s expiry=%s error_class=%s error=%s",
                    ticker, expiry_str, type(exc).__name__, exc,
                )
                n_chain_fetch_failed += 1
                continue

            # Per-strike walk + filter + StrikeCandidate construction
            for row in chain_rows or []:
                n_strikes_walked += 1
                sc = _row_to_strike_candidate(
                    candidate, row, expiry=expiry_str, dte=dte,
                )
                if sc is not None:
                    survivors.append(sc)
                    n_strikes_kept += 1
                    ticker_strike_count += 1

            # Inter-expiry courtesy delay — only between expiries,
            # not after the last one.
            if idx < len(selected) - 1:
                await asyncio.sleep(config.CHAIN_WALKER_INTER_EXPIRY_DELAY_S)

        # ── Step E: zero-survivor ticker note ─────────────────
        # This is an info-level note, not a drop. The ticker wasn't
        # dropped at the ticker level (expiries existed, chain
        # fetches may have succeeded), it just produced zero viable
        # strikes after per-strike filtering.
        if ticker_strike_count == 0:
            logger.info(
                "[screener.chain_walker] TICKER_ZERO_SURVIVORS_PHASE5 "
                "ticker=%s", ticker,
            )
            n_ticker_zero_survivors += 1

    # ── Final log line ────────────────────────────────────────
    elapsed = time.monotonic() - start_ts
    tickers_with_strikes = (
        total_tickers - n_no_expiries - n_ticker_zero_survivors
    )
    logger.info(
        "[screener.chain_walker] Phase 5 complete: "
        "tickers_in=%d tickers_with_strikes=%d "
        "strikes_walked=%d strikes_kept=%d "
        "(dropped: no_expiries=%d chain_fetch_failed=%d "
        "zero_survivors=%d) elapsed=%.1fs",
        total_tickers, tickers_with_strikes,
        n_strikes_walked, n_strikes_kept,
        n_no_expiries, n_chain_fetch_failed, n_ticker_zero_survivors,
        elapsed,
    )
    return survivors
