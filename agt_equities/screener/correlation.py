"""
agt_equities.screener.correlation — Phase 3.5: Correlation-fit portfolio gate.

Sits between Phase 3 (fundamentals) and Phase 4 (volatility/event armor).
Takes the Phase 3 fundamentally-strong survivor list, the price-history
dataframe hoisted from Phase 2, and the operator-supplied list of current
holdings, and rejects candidates that are too closely correlated with
the existing Wheel book.

WHY this phase exists: Portfolio Risk Rulebook v10 Rule 4 — diversification.
A wheel candidate that passes every fundamental gate but moves in lockstep
with three things we already own adds nothing but concentration risk to
the book. The pairwise correlation gate is the structural defense.

WHY it runs after fundamentals: correlation requires price history (which
Phase 2 already has) AND a fundamentally-acceptable candidate set (which
Phase 3 produces). Running before fundamentals would waste pandas work on
candidates that are about to be rejected anyway. Running before Phase 2
is impossible — we wouldn't have the price history yet.

GLOBAL FIT (Yash ruling 2026-04-11):
The Wheel candidate universe is identical across all households, so this
phase computes ONE correlation matrix against ONE pooled holdings list,
not per-household variants. There is no per-household routing.

EXCLUSIONS (Yash ruling 2026-04-11):
SLS, GTLB, and TRAW.CVR are stripped from the holdings list before
any correlation work. They are residual / fully-amortized / legacy
positions, not active Wheel state. Excluded BEFORE the already-held
check — a candidate named SLS is NOT dropped as already-held just
because SLS is in the raw current_holdings list.

ISOLATION CONTRACT: imports stdlib + pandas + (lazily) yfinance +
agt_equities.screener.{config, types}. NO Finnhub, NO httpx, NO
ib_async, NO walker, NO rule_engine. Enforced by
tests/test_screener_isolation.py.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable

import pandas as pd

from agt_equities.screener import config
from agt_equities.screener.types import CorrelationCandidate, FundamentalCandidate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default supplemental yfinance downloader
# ---------------------------------------------------------------------------

def _default_yf_download(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Production yfinance batch downloader for the supplemental holdings
    path. Same shape as technicals._default_yf_download — returns a
    {ticker: DataFrame} dict.

    Lazy yfinance import keeps the module unit-testable without yfinance
    installed (tests inject a synthetic factory).

    Returns an empty dict on any failure. The orchestrator treats missing
    holdings as effectively excluded — they're logged but not raised.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning(
            "[screener.correlation] yfinance not installed; supplemental "
            "holdings download returning empty result"
        )
        return {}

    if not symbols:
        return {}

    try:
        raw = yf.download(
            tickers=symbols,
            period=config.YFINANCE_HISTORY_PERIOD,
            interval=config.YFINANCE_HISTORY_INTERVAL,
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.warning(
            "[screener.correlation] supplemental yfinance download failed: %s",
            exc,
        )
        return {}

    if raw is None or raw.empty:
        return {}

    result: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for tk in raw.columns.get_level_values(0).unique():
            try:
                sub = raw[tk].dropna(how="all")
                if not sub.empty:
                    result[str(tk)] = sub
            except (KeyError, ValueError):
                continue
    else:
        if len(symbols) == 1:
            sub = raw.dropna(how="all")
            if not sub.empty:
                result[symbols[0]] = sub

    return result


# ---------------------------------------------------------------------------
# Holdings preparation — exclusions, supplementation, returns extraction
# ---------------------------------------------------------------------------

def _apply_holdings_exclusions(current_holdings: list[str]) -> tuple[list[str], list[str]]:
    """Strip CORRELATION_HOLDINGS_EXCLUSIONS from the raw holdings list.

    Returns:
        (effective_holdings, excluded_holdings) — the survivors and the
        ones that were stripped, both in input order, deduplicated.
    """
    seen: set[str] = set()
    effective: list[str] = []
    excluded: list[str] = []
    for h in current_holdings:
        if h in seen:
            continue
        seen.add(h)
        if h in config.CORRELATION_HOLDINGS_EXCLUSIONS:
            excluded.append(h)
        else:
            effective.append(h)
    return effective, excluded


def _identify_missing_holdings(
    effective_holdings: list[str],
    price_history: pd.DataFrame,
) -> list[str]:
    """Return the subset of effective_holdings whose ticker is not present
    at the top column level of price_history."""
    if price_history is None or price_history.empty:
        return list(effective_holdings)
    if not isinstance(price_history.columns, pd.MultiIndex):
        # Single-ticker DataFrame — no top-level ticker dimension
        return list(effective_holdings)
    present = set(price_history.columns.get_level_values(0))
    return [h for h in effective_holdings if h not in present]


def _supplement_price_history(
    price_history: pd.DataFrame,
    missing_holdings: list[str],
    download_fn: Callable[[list[str]], dict[str, pd.DataFrame]],
) -> tuple[pd.DataFrame, list[str]]:
    """Download the missing holdings' history and merge into price_history.

    Returns:
        (merged_df, still_missing) — the merged dataframe (may be the
        original if nothing was added) and the list of holdings that
        STILL couldn't be resolved after the supplemental download.

    Tickers in `still_missing` are dropped from the effective_holdings
    set by the caller — a holding we can't price-history is invisible
    to correlation fit.
    """
    if not missing_holdings:
        return price_history, []

    download_result = download_fn(missing_holdings)
    if not download_result:
        logger.warning(
            "[screener.correlation] supplemental download returned empty; "
            "%d holdings cannot be correlated and will be dropped from the gate: %s",
            len(missing_holdings), missing_holdings,
        )
        # Per-ticker log for each unresolvable holding so the audit trail
        # shows exactly which tickers couldn't be priced. Same log format
        # the dispatch specified for the merge-failure path.
        for h in missing_holdings:
            logger.warning(
                "[screener.correlation] HOLDING_DROPPED_PHASE35_NO_DATA "
                "ticker=%s reason=supplemental_download_missing", h,
            )
        return price_history, list(missing_holdings)

    # Reconstruct a MultiIndex DataFrame from the dict using the same
    # pd.concat pattern as technicals.run_phase_2
    try:
        supplement = pd.concat(
            list(download_result.values()),
            keys=list(download_result.keys()),
            axis=1,
        )
    except (ValueError, TypeError) as exc:
        logger.warning(
            "[screener.correlation] supplement reconstruction failed (%s); "
            "all %d missing holdings dropped",
            exc, len(missing_holdings),
        )
        return price_history, list(missing_holdings)

    # Merge with the original price_history on the date index. Outer join
    # preserves all dates from both frames; columns are unioned at the
    # MultiIndex top level.
    if price_history is None or price_history.empty:
        merged = supplement
    else:
        try:
            merged = pd.concat([price_history, supplement], axis=1)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[screener.correlation] merge with original price_history "
                "failed (%s); falling back to supplement-only", exc,
            )
            merged = supplement

    # Determine which missing tickers are still absent
    if isinstance(merged.columns, pd.MultiIndex):
        present = set(merged.columns.get_level_values(0))
    else:
        present = set()
    still_missing = [h for h in missing_holdings if h not in present]

    if still_missing:
        for h in still_missing:
            logger.warning(
                "[screener.correlation] HOLDING_DROPPED_PHASE35_NO_DATA "
                "ticker=%s reason=supplemental_download_missing", h,
            )

    return merged, still_missing


def _extract_returns(
    price_history: pd.DataFrame, tickers: list[str], window_days: int,
) -> pd.DataFrame:
    """Extract the Close-price returns for the given tickers, sliced to
    the trailing window. Returns an empty DataFrame on any extraction
    failure.

    Output shape: (window_days - 1, len(tickers)) DataFrame of daily
    returns. Each column is one ticker; index is the trading day.
    """
    if price_history is None or price_history.empty:
        return pd.DataFrame()
    if not tickers:
        return pd.DataFrame()
    if not isinstance(price_history.columns, pd.MultiIndex):
        return pd.DataFrame()

    try:
        # .xs("Close", level=1, axis=1) → DataFrame with one column per ticker
        closes = price_history.xs("Close", level=1, axis=1)
    except KeyError:
        return pd.DataFrame()

    # Restrict to the requested tickers (drop missing — caller filters)
    available = [t for t in tickers if t in closes.columns]
    if not available:
        return pd.DataFrame()

    closes_subset = closes[available].tail(window_days)
    return closes_subset.pct_change().dropna(how="all")


# ---------------------------------------------------------------------------
# Phase 3.5 orchestrator
# ---------------------------------------------------------------------------

def run_phase_3_5(
    candidates: list[FundamentalCandidate],
    price_history: Any,
    current_holdings: list[str],
    *,
    yf_download_factory: Callable[[list[str]], dict[str, pd.DataFrame]] | None = None,
) -> list[CorrelationCandidate]:
    """Execute Phase 3.5: correlation-fit gate against the current Wheel book.

    Args:
        candidates: Phase 3 output (FundamentalCandidate list).
        price_history: MultiIndex pd.DataFrame from Phase2Output.price_history.
            Top column level is ticker, second level is OHLCV field.
        current_holdings: pooled list of ticker symbols currently held in
            the Wheel book. Global, not per-household per Yash ruling.
        yf_download_factory: optional injection point for tests. Used ONLY
            for the supplemental holdings download path (holdings whose
            history isn't in price_history). Default wraps yfinance.

    Returns:
        List of CorrelationCandidate survivors with max_abs_correlation
        and most_correlated_holding populated for audit. Failed/missing
        candidates are dropped fail-closed with structured warning logs.
    """
    start_ts = time.monotonic()

    # ── Step 0: empty input short-circuit ─────────────────────────
    if not candidates:
        logger.info(
            "[screener.correlation] Phase 3.5: empty candidates list, "
            "returning empty result"
        )
        return []

    total = len(candidates)

    # ── Step 1: apply holdings exclusions ─────────────────────────
    effective_holdings, excluded_holdings = _apply_holdings_exclusions(
        current_holdings or []
    )
    if excluded_holdings:
        logger.info(
            "[screener.correlation] Holdings exclusions applied: stripped %d "
            "tickers from correlation set: %s",
            len(excluded_holdings), excluded_holdings,
        )

    # ── Step 2: empty effective holdings → all candidates pass ────
    if not effective_holdings:
        logger.info(
            "[screener.correlation] No active holdings to correlate against "
            "(effective_holdings=0); all %d candidates pass correlation gate",
            total,
        )
        survivors = [
            CorrelationCandidate.from_fundamental(
                c,
                max_abs_correlation=0.0,
                most_correlated_holding="",
            )
            for c in candidates
        ]
        elapsed = time.monotonic() - start_ts
        logger.info(
            "[screener.correlation] Phase 3.5 complete: processed=%d "
            "survivors=%d dropped=0 (by reason: already_held=0 "
            "insufficient_overlap=0 nan_corr=0 correlation_gate=0) "
            "effective_holdings=0 excluded_holdings=%d elapsed=%.1fs",
            total, len(survivors), len(excluded_holdings), elapsed,
        )
        return survivors

    # ── Step 3: identify missing holdings, supplement if needed ──
    download_fn = yf_download_factory if yf_download_factory is not None else _default_yf_download
    missing = _identify_missing_holdings(effective_holdings, price_history)

    merged_history = price_history
    if missing:
        logger.info(
            "[screener.correlation] %d effective holdings missing from "
            "Phase 2 dataframe; supplementing: %s",
            len(missing), missing,
        )
        merged_history, still_missing = _supplement_price_history(
            price_history, missing, download_fn,
        )
        # Drop any holdings that supplemental download couldn't resolve
        if still_missing:
            effective_holdings = [
                h for h in effective_holdings if h not in still_missing
            ]
            if not effective_holdings:
                # All effective holdings turned out to be unresolvable.
                # Fall through to the "no holdings" case — all candidates pass.
                logger.warning(
                    "[screener.correlation] All %d effective holdings "
                    "unresolvable after supplemental download; treating "
                    "as empty holdings set", len(missing),
                )
                survivors = [
                    CorrelationCandidate.from_fundamental(
                        c,
                        max_abs_correlation=0.0,
                        most_correlated_holding="",
                    )
                    for c in candidates
                ]
                elapsed = time.monotonic() - start_ts
                logger.info(
                    "[screener.correlation] Phase 3.5 complete: processed=%d "
                    "survivors=%d dropped=0 (by reason: already_held=0 "
                    "insufficient_overlap=0 nan_corr=0 correlation_gate=0) "
                    "effective_holdings=0 excluded_holdings=%d elapsed=%.1fs",
                    total, len(survivors), len(excluded_holdings), elapsed,
                )
                return survivors

    # ── Step 4: build returns dataframes ──────────────────────────
    candidate_tickers = [c.ticker for c in candidates]
    candidate_returns = _extract_returns(
        merged_history, candidate_tickers, config.CORRELATION_WINDOW_DAYS,
    )
    holdings_returns = _extract_returns(
        merged_history, effective_holdings, config.CORRELATION_WINDOW_DAYS,
    )

    if holdings_returns.empty:
        # Holdings present in name but no usable price data — same outcome
        # as empty effective_holdings, all candidates pass.
        logger.warning(
            "[screener.correlation] Effective holdings have no usable "
            "return data after windowing; all candidates pass correlation gate"
        )
        survivors = [
            CorrelationCandidate.from_fundamental(
                c,
                max_abs_correlation=0.0,
                most_correlated_holding="",
            )
            for c in candidates
        ]
        elapsed = time.monotonic() - start_ts
        logger.info(
            "[screener.correlation] Phase 3.5 complete: processed=%d "
            "survivors=%d dropped=0 (by reason: already_held=0 "
            "insufficient_overlap=0 nan_corr=0 correlation_gate=0) "
            "effective_holdings=%d excluded_holdings=%d elapsed=%.1fs",
            total, len(survivors), len(effective_holdings),
            len(excluded_holdings), elapsed,
        )
        return survivors

    # ── Step 5: per-candidate processing ──────────────────────────
    survivors: list[CorrelationCandidate] = []
    n_already_held = 0
    n_insufficient_overlap = 0
    n_nan_corr = 0
    n_correlation_gate = 0
    n_missing_from_df = 0
    effective_set = set(effective_holdings)

    for candidate in candidates:
        ticker = candidate.ticker

        # 5a: already-held check (uses post-exclusion effective_holdings)
        if ticker in effective_set:
            logger.warning(
                "[screener.correlation] TICKER_DROPPED_PHASE35_ALREADY_HELD "
                "ticker=%s", ticker,
            )
            n_already_held += 1
            continue

        # Defensive: candidate must have a column in candidate_returns
        if candidate_returns.empty or ticker not in candidate_returns.columns:
            logger.warning(
                "[screener.correlation] TICKER_DROPPED_PHASE35_NO_DATA "
                "ticker=%s reason=candidate_missing_from_phase2_df", ticker,
            )
            n_missing_from_df += 1
            continue

        candidate_series = candidate_returns[ticker].dropna()

        # 5b: overlap check
        overlap_index = candidate_series.index.intersection(holdings_returns.index)
        if len(overlap_index) < config.MIN_CORRELATION_OVERLAP_DAYS:
            logger.warning(
                "[screener.correlation] TICKER_DROPPED_PHASE35_INSUFFICIENT_OVERLAP "
                "ticker=%s overlap_days=%d min_required=%d",
                ticker, len(overlap_index), config.MIN_CORRELATION_OVERLAP_DAYS,
            )
            n_insufficient_overlap += 1
            continue

        # 5c: build aligned frame and compute correlation
        try:
            aligned_data: dict[str, pd.Series] = {
                "candidate": candidate_series.loc[overlap_index],
            }
            for h in effective_holdings:
                if h in holdings_returns.columns:
                    aligned_data[h] = holdings_returns[h].loc[overlap_index]
            aligned = pd.DataFrame(aligned_data)
            corr_row = aligned.corr().loc["candidate"].drop("candidate")
            abs_corr = corr_row.abs()
        except Exception as exc:
            logger.warning(
                "[screener.correlation] TICKER_DROPPED_PHASE35_NO_DATA "
                "ticker=%s reason=corr_computation_error:%s",
                ticker, type(exc).__name__,
            )
            n_nan_corr += 1
            continue

        # 5d: NaN guard
        if abs_corr.isna().any() or abs_corr.empty:
            logger.warning(
                "[screener.correlation] TICKER_DROPPED_PHASE35_NAN_CORR "
                "ticker=%s", ticker,
            )
            n_nan_corr += 1
            continue

        # 5e: extract max + corresponding holding
        max_abs = float(abs_corr.max())
        most_correlated = str(abs_corr.idxmax())

        # 5f: gate check
        if max_abs > config.MAX_HOLDING_CORRELATION:
            logger.info(
                "[screener.correlation] TICKER_DROPPED_PHASE35_CORRELATION_GATE "
                "ticker=%s max_abs=%.3f most_corr=%s threshold=%.2f",
                ticker, max_abs, most_correlated, config.MAX_HOLDING_CORRELATION,
            )
            n_correlation_gate += 1
            continue

        # 5g: survivor
        survivors.append(CorrelationCandidate.from_fundamental(
            candidate,
            max_abs_correlation=max_abs,
            most_correlated_holding=most_correlated,
        ))

    # ── Step 6: final log line ───────────────────────────────────
    elapsed = time.monotonic() - start_ts
    dropped = total - len(survivors)
    logger.info(
        "[screener.correlation] Phase 3.5 complete: processed=%d "
        "survivors=%d dropped=%d "
        "(by reason: already_held=%d insufficient_overlap=%d "
        "nan_corr=%d correlation_gate=%d) "
        "effective_holdings=%d excluded_holdings=%d elapsed=%.1fs",
        total, len(survivors), dropped,
        n_already_held, n_insufficient_overlap,
        n_nan_corr + n_missing_from_df, n_correlation_gate,
        len(effective_holdings), len(excluded_holdings), elapsed,
    )
    return survivors
