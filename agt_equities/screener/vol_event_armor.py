"""
agt_equities.screener.vol_event_armor — Phase 4: Volatility / Event Armor.

Sits between Phase 3.5 (correlation fit) and Phase 5 (option chains).
Takes the Phase 3.5 uncorrelated-and-fundamentally-strong survivor list
and verifies four additional gates before handing to Phase 5:

  1. IVR >= MIN_IVR_PCT (30%)
     — IV Rank floor, computed from live IBKR OPTION_IMPLIED_VOLATILITY
       historical bars over the trailing ~252 trading days.
     — Rationale: Wheel sellers monetize IV richness. Names at the 5th
       percentile of their own IV history produce premium that will not
       compensate for assignment risk.
     — Data source: IBKR reqHistoricalDataAsync with
       whatToShow="OPTION_IMPLIED_VOLATILITY". Probe-verified 2026-04-11
       against AAPL / MSFT / SPY.

  2. No scheduled earnings within EARNINGS_BLACKOUT_DAYS (10)
     — Prevents opening a CSP directly into a binary earnings event.
     — Data source: YFinanceCorporateIntelligenceProvider corporate
       calendar cache.

  3. No ex-dividend within EX_DIV_BLACKOUT_DAYS (5)
     — Early-assignment risk on short calls around ex-div dates.

  4. pending_corporate_action == CorporateActionType.NONE
     — Any pending M&A, spinoff, special dividend, tender, or "other"
       corporate action is an automatic disqualifier. The wheel does
       not underwrite event risk.

ARCHITECTURE NOTES:

This is the FIRST screener phase that makes live IBKR calls. Previous
phases were either pure HTTP (Finnhub profile2 in Phase 1), batched
yfinance (Phase 2 price history), per-ticker yfinance (Phase 3
fundamentals), or in-process pandas math (Phase 3.5 correlation).

Because of the live IBKR dependency, vol_event_armor.py is the
SECOND file in the screener package allowed to import ib_async (the
first being chain_walker.py, reserved for Phase 5). The AST guard
test at tests/test_screener_isolation.py enforces this whitelist.
Every OTHER screener file remains blocked from importing ib_async.

The ib connection is INJECTED by the caller (future /scan
orchestrator). vol_event_armor.py does NOT establish its own
connection — it receives a connected IB object and uses it. This
keeps lifecycle management at the orchestrator level where
reconnection logic already exists in telegram_bot.py.

The calendar_provider is constructed lazily. Tests inject a fake;
production constructs YFinanceCorporateIntelligenceProvider inside
run_phase_4 only when calendar_provider is None. The runtime import
of YFinanceCorporateIntelligenceProvider happens INSIDE the function
body, not at module load time, so Phase 4 only pays the provider
import cost when it's actually run.

ISOLATION CONTRACT:
  Allowed imports:
    stdlib (asyncio, logging, time, datetime, typing)
    ib_async                              (whitelisted, C4)
    agt_equities.market_data_dtos         (CorporateCalendarDTO + CorporateActionType)
    agt_equities.providers.yfinance_...   (lazy, inside function body)
    agt_equities.screener.{config, types} (intra-package)
  Forbidden: yfinance (direct), httpx, pandas (unused here — pandas
  is used by Phase 2/3.5 but not Phase 4), numpy, anything in
  agt_equities.{walker, trade_repo, rule_engine, mode_engine,
  telegram_bot}, agt_deck.*, sqlite3.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import TYPE_CHECKING, Any, Optional

import ib_async

from agt_equities.market_data_dtos import (
    CorporateActionType,
    CorporateCalendarDTO,
)
from agt_equities.screener import config
from agt_equities.screener.types import (
    CorrelationCandidate,
    VolArmorCandidate,
)

if TYPE_CHECKING:
    from agt_equities.providers.yfinance_corporate_intelligence import (
        YFinanceCorporateIntelligenceProvider,
    )

logger = logging.getLogger(__name__)


async def run_phase_4(
    candidates: list[CorrelationCandidate],
    ib: ib_async.IB,
    calendar_provider: Optional["YFinanceCorporateIntelligenceProvider"] = None,
) -> list[VolArmorCandidate]:
    """Execute Phase 4: IVR gate + corporate calendar gates.

    Args:
        candidates: Phase 3.5 output (CorrelationCandidate list).
        ib: connected ib_async.IB instance, provided by the caller.
            vol_event_armor does NOT establish its own connection.
        calendar_provider: optional injection point for tests. Default
            constructs YFinanceCorporateIntelligenceProvider() lazily
            inside this function if None.

    Returns:
        List of VolArmorCandidate survivors with IVR snapshot and
        corporate calendar data populated for audit. Failed or
        gate-missing candidates are dropped fail-closed with
        structured log lines.

    Design notes:
      - Two separate try/except blocks (IBKR, calendar). Attribution
        of drop reasons must distinguish IBKR errors from calendar
        errors in the final counter totals.
      - Per-ticker try/except is MANDATORY. One bad ticker cannot
        abort the batch.
      - The courtesy delay sits BETWEEN the IBKR block and the
        calendar block, not after both, so back-to-back IBKR calls
        are paced but calendar calls don't waste wall time.
      - No heartbeat. Phase 4 input is ~10-15 tickers maximum; total
        runtime ~20-40 seconds.
    """
    start_ts = time.monotonic()

    if not candidates:
        logger.info(
            "[screener.vol_event_armor] Phase 4: empty candidates list, "
            "returning empty result"
        )
        return []

    # Lazy construction of the calendar provider. The runtime import
    # sits inside this branch so Phase 4 only pays the provider import
    # cost when actually executed.
    if calendar_provider is None:
        from agt_equities.providers.yfinance_corporate_intelligence import (
            YFinanceCorporateIntelligenceProvider,
        )
        calendar_provider = YFinanceCorporateIntelligenceProvider()

    # Drop-reason counters
    n_qualify_failed = 0
    n_iv_insufficient = 0
    n_iv_nulls = 0
    n_iv_degenerate = 0
    n_ibkr_error = 0
    n_ivr_below = 0
    n_earnings = 0
    n_ex_div = 0
    n_corp_action = 0
    n_calendar_unavailable = 0
    n_calendar_error = 0

    survivors: list[VolArmorCandidate] = []
    total = len(candidates)

    logger.info(
        "[screener.vol_event_armor] Phase 4 (IVR + corporate calendar): "
        "starting with %d candidates",
        total,
    )

    for candidate in candidates:
        ticker = candidate.ticker

        # ── Block A: IBKR IVR gate ──────────────────────────────
        # All IBKR-side failures (qualify, historical data fetch,
        # unexpected exceptions) are attributed to ibkr_* counters.
        # Calendar errors are handled in the separate block below
        # so they don't get lumped into ibkr_error.
        ivr_pct: Optional[float] = None
        iv_latest: Optional[float] = None
        iv_min: Optional[float] = None
        iv_max: Optional[float] = None
        iv_bars_used: int = 0

        try:
            stock = ib_async.Stock(
                symbol=ticker,
                exchange="SMART",
                currency="USD",
            )
            qualified = await ib.qualifyContractsAsync(stock)
            if not qualified:
                logger.info(
                    "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_QUALIFY_FAILED "
                    "ticker=%s", ticker,
                )
                n_qualify_failed += 1
                continue
            contract = qualified[0]

            bars = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=config.IV_HISTORY_DURATION,
                barSizeSetting="1 day",
                whatToShow="OPTION_IMPLIED_VOLATILITY",
                useRTH=True,
                formatDate=1,
            )

            if not bars or len(bars) < config.MIN_IV_BARS:
                logger.info(
                    "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_IV_INSUFFICIENT "
                    "ticker=%s bars=%d min=%d",
                    ticker, len(bars) if bars else 0, config.MIN_IV_BARS,
                )
                n_iv_insufficient += 1
                continue

            iv_values = [
                float(b.close) for b in bars
                if b.close is not None and b.close > 0
            ]
            if len(iv_values) < config.MIN_IV_BARS:
                logger.info(
                    "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_IV_NULLS "
                    "ticker=%s valid_bars=%d min=%d",
                    ticker, len(iv_values), config.MIN_IV_BARS,
                )
                n_iv_nulls += 1
                continue

            iv_min = min(iv_values)
            iv_max = max(iv_values)
            iv_latest = iv_values[-1]
            iv_bars_used = len(iv_values)

            if iv_max <= iv_min:
                logger.info(
                    "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_IV_DEGENERATE "
                    "ticker=%s iv_min=%.4f iv_max=%.4f",
                    ticker, iv_min, iv_max,
                )
                n_iv_degenerate += 1
                continue

            ivr_pct = (iv_latest - iv_min) / (iv_max - iv_min) * 100.0

            if ivr_pct < config.MIN_IVR_PCT:
                logger.info(
                    "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_IVR_BELOW_FLOOR "
                    "ticker=%s ivr=%.1f%% floor=%.1f%%",
                    ticker, ivr_pct, config.MIN_IVR_PCT,
                )
                n_ivr_below += 1
                continue

        except Exception as exc:
            logger.warning(
                "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_IBKR_ERROR "
                "ticker=%s error_class=%s error=%s",
                ticker, type(exc).__name__, exc,
            )
            n_ibkr_error += 1
            continue

        # Rate-limit courtesy delay between IBKR block and calendar
        # block. This spaces out back-to-back reqHistoricalData calls
        # across candidates without wasting wall time on calendar
        # fetches (which don't hit IBKR).
        await asyncio.sleep(config.IBKR_HIST_DATA_COURTESY_DELAY_S)

        # ── Block B: Corporate calendar gates ───────────────────
        # Separate try/except so calendar failures are attributed
        # to calendar_* counters, not lumped under ibkr_error.
        try:
            calendar: Optional[CorporateCalendarDTO] = await asyncio.to_thread(
                calendar_provider.get_corporate_calendar, ticker,
            )
        except Exception as exc:
            logger.warning(
                "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_CALENDAR_ERROR "
                "ticker=%s error_class=%s error=%s",
                ticker, type(exc).__name__, exc,
            )
            n_calendar_error += 1
            continue

        if calendar is None:
            logger.info(
                "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_CALENDAR_UNAVAILABLE "
                "ticker=%s", ticker,
            )
            n_calendar_unavailable += 1
            continue

        today = date.today()

        # Earnings window gate — drop if earnings fall inside
        # [today, today + EARNINGS_BLACKOUT_DAYS] inclusive.
        if calendar.next_earnings is not None:
            days_to_earnings = (calendar.next_earnings - today).days
            if 0 <= days_to_earnings <= config.EARNINGS_BLACKOUT_DAYS:
                logger.info(
                    "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_EARNINGS_BLACKOUT "
                    "ticker=%s days_to_earnings=%d blackout=%d",
                    ticker, days_to_earnings, config.EARNINGS_BLACKOUT_DAYS,
                )
                n_earnings += 1
                continue

        # Ex-dividend window gate
        if calendar.ex_dividend_date is not None:
            days_to_ex_div = (calendar.ex_dividend_date - today).days
            if 0 <= days_to_ex_div <= config.EX_DIV_BLACKOUT_DAYS:
                logger.info(
                    "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_EX_DIV_BLACKOUT "
                    "ticker=%s days_to_ex_div=%d blackout=%d",
                    ticker, days_to_ex_div, config.EX_DIV_BLACKOUT_DAYS,
                )
                n_ex_div += 1
                continue

        # Pending corporate action gate
        if calendar.pending_corporate_action != CorporateActionType.NONE:
            logger.info(
                "[screener.vol_event_armor] TICKER_DROPPED_PHASE4_CORP_ACTION "
                "ticker=%s action=%s",
                ticker, calendar.pending_corporate_action.value,
            )
            n_corp_action += 1
            continue

        # ── All gates passed — construct survivor ──────────────
        # iv_min / iv_max / iv_latest are known-not-None here because
        # the IVR gate would have continued on any of the earlier
        # failure paths. The type checker sees them as Optional, so
        # we coerce via assert for narrowing — the asserts can never
        # fire at runtime because they're preceded by the explicit
        # `continue` statements above.
        assert ivr_pct is not None
        assert iv_latest is not None
        assert iv_min is not None
        assert iv_max is not None

        survivors.append(VolArmorCandidate.from_correlation(
            candidate,
            ivr_pct=ivr_pct,
            iv_latest=iv_latest,
            iv_52w_min=iv_min,
            iv_52w_max=iv_max,
            iv_bars_used=iv_bars_used,
            next_earnings=calendar.next_earnings,
            ex_dividend_date=calendar.ex_dividend_date,
            calendar_source=calendar.data_source,
        ))

    # ── Final log line ────────────────────────────────────────
    elapsed = time.monotonic() - start_ts
    dropped = total - len(survivors)
    logger.info(
        "[screener.vol_event_armor] Phase 4 complete: "
        "processed=%d survivors=%d dropped=%d "
        "(qualify_failed=%d iv_insufficient=%d iv_nulls=%d "
        "iv_degenerate=%d ibkr_error=%d ivr_below=%d "
        "earnings=%d ex_div=%d corp_action=%d "
        "calendar_unavailable=%d calendar_error=%d) "
        "elapsed=%.1fs",
        total, len(survivors), dropped,
        n_qualify_failed, n_iv_insufficient, n_iv_nulls,
        n_iv_degenerate, n_ibkr_error, n_ivr_below,
        n_earnings, n_ex_div, n_corp_action,
        n_calendar_unavailable, n_calendar_error,
        elapsed,
    )
    return survivors
