"""
agt_equities.screener.ray_filter — Phase 6: RAY (Ratio of Annualized Yield) filter.

TERMINAL screener phase. Sits after Phase 5 (option chain walker)
and is the final gate before the output is handed to the
orchestrator (future C7 wiring to /scan in telegram_bot.py).

Phase 6 takes the ~160-320 StrikeCandidates that Phase 5 produced
and filters them down to the ones whose annualized_yield falls
within the Rulebook Rule 7 Mode 2 RAY band: 30% to 130% inclusive.

SCOPE per Yash ruling 2026-04-11:
  - Filter-only. Phase 6 does NOT rank, sort, or select winners.
    Downstream orchestrator / Telegram display logic picks which
    candidate to present to the operator.
  - Band check is INCLUSIVE on both ends. A strike at exactly
    30.0% PASSES. A strike at exactly 130.0% PASSES. Implementation
    uses `<` for below-band and `>` for above-band rejection, NOT
    `<=` or `>=`.
  - Return order matches input order verbatim. No sorting, no
    deduplication.
  - Sync function. Phase 6 is the first screener phase that does
    NOT touch the network or any I/O — it is a pure list-to-list
    transformation that runs in sub-second time.

ARCHITECTURAL NOTES:

Phase 6 has ZERO external dependencies beyond stdlib and the
screener's own types/config. No network, no database, no provider
imports. This is the ONLY screener phase that can run against a
purely synthetic fixture without any mocking at all — tests
construct real StrikeCandidate instances and pass them through
run_phase_6 directly.

The NaN guard in the malformed-data check (step 4 of the dispatch
spec) is critical. A NaN annualized_yield would otherwise fall
through all three comparison branches because `float('nan') < x`,
`float('nan') > x`, and `float('nan') == x` are all False. Without
the explicit isnan check, a NaN strike would silently pass through
as in-band. The guard uses math.isnan() rather than a self-equality
check for readability.

ISOLATION CONTRACT:
  Allowed imports:
    stdlib (logging, time, math, typing)
    agt_equities.screener.{config, types} (intra-package)
  Forbidden: everything else. No ib_async, no yfinance, no httpx,
  no pandas, no numpy, no Finnhub client, no providers, no telegram_bot,
  no walker, no trade_repo, no rule_engine, no sqlite3.
"""
from __future__ import annotations

import logging
import math
import time

from agt_equities.screener import config
from agt_equities.screener.types import (
    RAYCandidate,
    StrikeCandidate,
)

logger = logging.getLogger(__name__)


def run_phase_6(
    candidates: list[StrikeCandidate],
) -> list[RAYCandidate]:
    """Execute Phase 6: RAY band filter.

    Args:
        candidates: Phase 5 output (StrikeCandidate list). Empty list
            returns empty list with zero crash risk.

    Returns:
        List of RAYCandidate — StrikeCandidates whose annualized_yield
        falls in [MIN_RAY*100, MAX_RAY*100] inclusive, in the same
        order as the input. Phase 6 does NOT sort or rank.

    Drop reasons (logged per-strike at info level, counted in the
    final log line):
      below_band:   yield < MIN_RAY * 100
      above_band:   yield > MAX_RAY * 100
      malformed:    yield is None, NaN, or <= 0
                    (data quality issue, not a legitimate filter miss)
    """
    start_ts = time.monotonic()

    if not candidates:
        logger.info(
            "[screener.ray_filter] Phase 6: empty candidates list, "
            "returning empty result"
        )
        return []

    total = len(candidates)
    logger.info(
        "[screener.ray_filter] Phase 6 (RAY band filter): "
        "starting with %d candidates", total,
    )

    n_below_band = 0
    n_above_band = 0
    n_malformed = 0
    survivors: list[RAYCandidate] = []

    # Convert band to percent form for comparison against annualized_yield
    # (which is stored in percent form on StrikeCandidate per Phase 5 spec).
    min_ray_pct = config.MIN_RAY * 100.0   # e.g. 30.0
    max_ray_pct = config.MAX_RAY * 100.0   # e.g. 130.0

    for candidate in candidates:
        try:
            yield_pct = candidate.annualized_yield

            # Malformed data guard: None, NaN, or non-positive yield
            # is a data quality issue, not a legitimate filter miss.
            # Phase 5 should never produce these, but the guard is
            # defense in depth. The explicit math.isnan check is
            # critical — NaN comparisons all return False, so a NaN
            # yield would otherwise fall through to the "in-band"
            # branch without being caught by the <= 0 check alone.
            if (
                yield_pct is None
                or (isinstance(yield_pct, float) and math.isnan(yield_pct))
                or yield_pct <= 0.0
            ):
                logger.info(
                    "[screener.ray_filter] STRIKE_DROPPED_PHASE6_MALFORMED "
                    "ticker=%s expiry=%s strike=%.2f yield=%s",
                    candidate.ticker, candidate.expiry, candidate.strike,
                    yield_pct,
                )
                n_malformed += 1
                continue

            # Below-band filter (strict < — 30.0 exactly PASSES)
            if yield_pct < min_ray_pct:
                logger.info(
                    "[screener.ray_filter] STRIKE_DROPPED_PHASE6_BELOW_BAND "
                    "ticker=%s expiry=%s strike=%.2f yield=%.2f%% "
                    "min=%.2f%%",
                    candidate.ticker, candidate.expiry, candidate.strike,
                    yield_pct, min_ray_pct,
                )
                n_below_band += 1
                continue

            # Above-band filter (strict > — 130.0 exactly PASSES)
            if yield_pct > max_ray_pct:
                logger.info(
                    "[screener.ray_filter] STRIKE_DROPPED_PHASE6_ABOVE_BAND "
                    "ticker=%s expiry=%s strike=%.2f yield=%.2f%% "
                    "max=%.2f%%",
                    candidate.ticker, candidate.expiry, candidate.strike,
                    yield_pct, max_ray_pct,
                )
                n_above_band += 1
                continue

            # In-band — construct RAYCandidate with decimal-form yield
            ray_decimal = yield_pct / 100.0
            survivors.append(RAYCandidate.from_strike(
                candidate,
                ray_decimal=ray_decimal,
            ))

        except Exception as exc:
            # Per-candidate defensive guard. A single malformed
            # StrikeCandidate with missing fields (e.g., constructed
            # bypassing the normal Phase 5 pipeline) must not abort
            # the batch. Count as malformed and continue.
            logger.warning(
                "[screener.ray_filter] STRIKE_DROPPED_PHASE6_ERROR "
                "ticker=%s expiry=%s strike=%.2f error_class=%s error=%s",
                getattr(candidate, "ticker", "?"),
                getattr(candidate, "expiry", "?"),
                getattr(candidate, "strike", 0.0),
                type(exc).__name__, exc,
            )
            n_malformed += 1
            continue

    elapsed = time.monotonic() - start_ts
    logger.info(
        "[screener.ray_filter] Phase 6 complete: "
        "strikes_in=%d survivors=%d dropped=%d "
        "(below_band=%d above_band=%d malformed=%d) "
        "elapsed=%.2fs",
        total, len(survivors), total - len(survivors),
        n_below_band, n_above_band, n_malformed,
        elapsed,
    )
    return survivors
