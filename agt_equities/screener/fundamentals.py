"""
agt_equities.screener.fundamentals — Phase 3: yfinance per-ticker fundamentals.

Takes the Phase 2 survivor list (TechnicalCandidate) and runs each ticker
through a per-ticker yfinance fetch to compute the Fortress Five:

  Altman Z-Score             > MIN_ALTMAN_Z          (3.0, strict)
  FCF Yield                  >= MIN_FCF_YIELD        (0.04)
  Net Debt / EBITDA          <= MAX_NET_DEBT_TO_EBITDA (3.0)
  Return on Invested Capital >= MIN_ROIC             (0.10)
  Short Interest % of Float  <= MAX_SHORT_INTEREST   (0.10)

WHY this phase runs after Phase 2 and not before: Phase 2's batched
yfinance.download narrows ~480 universe tickers to ~30 active-pullback
survivors. Phase 3 then makes ~30 × 4 = 120 yfinance per-ticker calls,
which fits comfortably under any reasonable rate limit. If we'd run
Phase 3 first, we'd be making ~2000 fundamental fetches and risking
yfinance throttling. The Tech-First reorder is the entire reason this
sequencing works.

ISOLATION CONTRACT: imports stdlib + numpy + pandas + (lazily) yfinance
+ agt_equities.screener.{config, types}. NO Finnhub, NO httpx, NO
ib_async, NO telegram_bot, NO walker, NO rule_engine. Phase 3 has a
single data source (yfinance) and zero fallback. Enforced by
tests/test_screener_isolation.py.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from agt_equities.screener import config
from agt_equities.screener.types import FundamentalCandidate, TechnicalCandidate

logger = logging.getLogger(__name__)

# Heartbeat cadence — Phase 3 input is ~30 tickers max, so 3 heartbeats per run
HEARTBEAT_INTERVAL: int = 10

# Balance-sheet row label variants. yfinance label conventions drift across
# versions and across companies; we try the canonical label first and fall
# back to alternates. Add to these tuples if a real-world ticker surfaces a
# new variant.
_BS_TOTAL_ASSETS = ("Total Assets",)
_BS_TOTAL_LIABILITIES = (
    "Total Liabilities Net Minority Interest",
    "Total Liab",
    "Total Liabilities",
)
_BS_WORKING_CAPITAL = ("Working Capital",)
_BS_RETAINED_EARNINGS = ("Retained Earnings",)
_BS_STOCKHOLDERS_EQUITY = (
    "Stockholders Equity",
    "Total Stockholder Equity",
)
_BS_TOTAL_DEBT = ("Total Debt",)
_BS_CASH = (
    "Cash And Cash Equivalents",
    "Cash",
)

_IS_TOTAL_REVENUE = ("Total Revenue", "TotalRevenue")
_IS_EBIT = ("EBIT",)
_IS_EBITDA = ("EBITDA", "Normalized EBITDA")

_CF_OPERATING = (
    "Operating Cash Flow",
    "Total Cash From Operating Activities",
    "Cash Flow From Continuing Operating Activities",
)
_CF_CAPEX = (
    "Capital Expenditure",
    "CapitalExpenditure",
    "Capital Expenditures",
)


# ---------------------------------------------------------------------------
# Drop-reason exception — flow control for fail-closed paths
# ---------------------------------------------------------------------------

class _DropReason(Exception):
    """Internal control-flow exception. Carries a structured `reason` string
    that the orchestrator surfaces in the per-ticker drop log line.

    Reason taxonomy:
      info_fetch_failed              — ticker.info raised or returned None
      statements_unavailable         — balance_sheet/income_stmt/cashflow empty
      field_missing:<name>           — required statement row not found
      nan_computation:<metric>       — math operation produced NaN/inf
      degenerate_denominator:<which> — divisor <= 0 (ebitda or invested_capital)
      short_interest_unavailable     — neither shortPercentOfFloat nor
                                       sharesShort/floatShares present
      filter_fail                    — all metrics computed; one or more
                                       gates failed
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Statement parsing helpers — pure functions on pandas DataFrames
# ---------------------------------------------------------------------------

def _most_recent_value(df: pd.DataFrame, candidates: tuple[str, ...]) -> float:
    """Return the most recent (leftmost column) value for the first matching
    row label, or raise _DropReason("field_missing:<first_candidate>").

    yfinance financial statements use the most recent fiscal period in the
    leftmost column. Other periods are subsequent columns. We always read
    column 0 (the freshest TTM/quarter).
    """
    if df is None or df.empty:
        raise _DropReason("statements_unavailable")

    for label in candidates:
        if label in df.index:
            row = df.loc[label]
            if hasattr(row, "iloc"):
                val = row.iloc[0]
            else:
                val = row
            if val is None:
                continue
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue
            if math.isnan(f) or math.isinf(f):
                continue
            return f

    # No variant matched — surface the canonical name
    raise _DropReason(f"field_missing:{candidates[0]}")


def _info_get_float(info: dict, key: str) -> float | None:
    """Return info[key] as a finite float, or None if missing/invalid."""
    if not info or key not in info:
        return None
    val = info[key]
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ---------------------------------------------------------------------------
# Fundamentals extraction — pulls all fields needed by the five metrics
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _RawFundamentals:
    """Intermediate bag of numbers extracted from yfinance, before metrics
    are computed. Internal to fundamentals.py — not exported."""
    market_cap: float
    # Balance sheet
    total_assets: float
    total_liabilities: float
    working_capital: float
    retained_earnings: float
    stockholders_equity: float
    total_debt: float
    cash: float
    # Income statement
    revenue: float
    ebit: float
    ebitda: float
    # Cash flow
    operating_cash_flow: float
    capex: float  # negative in yfinance convention
    # Info-derived
    short_interest_pct: float
    effective_tax_rate: float


def _extract_fundamentals(
    ticker_obj: Any, market_cap_usd: float,
) -> _RawFundamentals:
    """Pull all required raw numbers from a yfinance Ticker (or test fake).

    Raises _DropReason on any data unavailability. Pure: no I/O beyond
    accessing ticker_obj attributes.
    """
    # ── info dict ──────────────────────────────────────────────────
    try:
        info = ticker_obj.info
    except Exception as exc:
        logger.debug("info fetch raised: %s", exc)
        raise _DropReason("info_fetch_failed")
    if info is None or not isinstance(info, dict):
        raise _DropReason("info_fetch_failed")

    # ── statements ─────────────────────────────────────────────────
    try:
        balance_sheet = ticker_obj.balance_sheet
        income_stmt = ticker_obj.income_stmt
        cashflow = ticker_obj.cashflow
    except Exception as exc:
        logger.debug("statements fetch raised: %s", exc)
        raise _DropReason("statements_unavailable")

    # ── balance sheet fields ───────────────────────────────────────
    total_assets = _most_recent_value(balance_sheet, _BS_TOTAL_ASSETS)
    total_liabilities = _most_recent_value(balance_sheet, _BS_TOTAL_LIABILITIES)
    working_capital = _most_recent_value(balance_sheet, _BS_WORKING_CAPITAL)
    retained_earnings = _most_recent_value(balance_sheet, _BS_RETAINED_EARNINGS)
    stockholders_equity = _most_recent_value(balance_sheet, _BS_STOCKHOLDERS_EQUITY)
    total_debt = _most_recent_value(balance_sheet, _BS_TOTAL_DEBT)
    cash = _most_recent_value(balance_sheet, _BS_CASH)

    # ── income statement fields ────────────────────────────────────
    revenue = _most_recent_value(income_stmt, _IS_TOTAL_REVENUE)
    ebit = _most_recent_value(income_stmt, _IS_EBIT)
    ebitda = _most_recent_value(income_stmt, _IS_EBITDA)

    # ── cash flow fields ───────────────────────────────────────────
    operating_cash_flow = _most_recent_value(cashflow, _CF_OPERATING)
    capex = _most_recent_value(cashflow, _CF_CAPEX)

    # ── short interest (info-only, with two-key fallback) ──────────
    # C3.7 Fix 1: fail-OPEN when both data paths are unavailable.
    # Rationale: yfinance shortPercentOfFloat is None for ~20-30% of
    # tickers. Fail-closed would silently drop a meaningful fraction
    # of viable candidates for the wrong reason. Missing data is NOT
    # equivalent to "high short interest" — treat it as 0% and emit
    # a warning so operator has audit visibility. The fallback path
    # (sharesShort / floatShares) is preserved ahead of the fail-open
    # — real data is always preferred when available.
    ticker_symbol_for_log = str(info.get("symbol") or info.get("shortName") or "?")
    short_interest_pct: float | None = _info_get_float(info, "shortPercentOfFloat")
    if short_interest_pct is None:
        shares_short = _info_get_float(info, "sharesShort")
        float_shares = _info_get_float(info, "floatShares")
        if shares_short is not None and float_shares is not None and float_shares > 0:
            short_interest_pct = shares_short / float_shares
    if short_interest_pct is None:
        logger.warning(
            "[screener.fundamentals] SHORT_INTEREST_UNAVAILABLE "
            "ticker=%s — treating as 0%% (fail-open per C3.7)",
            ticker_symbol_for_log,
        )
        short_interest_pct = 0.0

    # ── effective tax rate (optional, defaulted) ──────────────────
    tax_rate = _info_get_float(info, "effectiveTaxRate")
    if tax_rate is None:
        tax_rate = config.DEFAULT_EFFECTIVE_TAX_RATE

    return _RawFundamentals(
        market_cap=market_cap_usd,
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        working_capital=working_capital,
        retained_earnings=retained_earnings,
        stockholders_equity=stockholders_equity,
        total_debt=total_debt,
        cash=cash,
        revenue=revenue,
        ebit=ebit,
        ebitda=ebitda,
        operating_cash_flow=operating_cash_flow,
        capex=capex,
        short_interest_pct=short_interest_pct,
        effective_tax_rate=tax_rate,
    )


# ---------------------------------------------------------------------------
# Metric computations — pure functions on _RawFundamentals
# ---------------------------------------------------------------------------

def _compute_altman_z(raw: _RawFundamentals) -> float:
    """Public-firm Altman Z-Score:
        Z = 1.2*A + 1.4*B + 3.3*C + 0.6*D + 1.0*E
    where
        A = working_capital / total_assets
        B = retained_earnings / total_assets
        C = ebit / total_assets
        D = market_cap / total_liabilities
        E = revenue / total_assets

    Raises _DropReason on division-by-zero or NaN.
    """
    if raw.total_assets <= 0:
        raise _DropReason("degenerate_denominator:total_assets")
    if raw.total_liabilities <= 0:
        raise _DropReason("degenerate_denominator:total_liabilities")

    a = raw.working_capital / raw.total_assets
    b = raw.retained_earnings / raw.total_assets
    c = raw.ebit / raw.total_assets
    d = raw.market_cap / raw.total_liabilities
    e = raw.revenue / raw.total_assets

    z = 1.2 * a + 1.4 * b + 3.3 * c + 0.6 * d + 1.0 * e
    if math.isnan(z) or math.isinf(z):
        raise _DropReason("nan_computation:altman_z")
    return z


def _compute_fcf_yield(raw: _RawFundamentals) -> float:
    """Free cash flow yield: FCF / market_cap.

    FCF = operating_cash_flow - abs(capex)  (capex is negative in yfinance).
    Raises _DropReason on degenerate market cap or NaN.
    """
    if raw.market_cap <= 0:
        raise _DropReason("degenerate_denominator:market_cap")

    # capex is signed negative in yfinance; we want |capex| as the cash outflow
    fcf = raw.operating_cash_flow - abs(raw.capex)
    yield_ = fcf / raw.market_cap
    if math.isnan(yield_) or math.isinf(yield_):
        raise _DropReason("nan_computation:fcf_yield")
    return yield_


def _compute_net_debt_to_ebitda(raw: _RawFundamentals) -> float:
    """Net Debt / EBITDA: (total_debt - cash) / ebitda.

    C3.7 Fix 2: check `net_debt <= 0` BEFORE the EBITDA guard.
    TIKR calibration 2026-04-11 revealed that mega-cap tech (NVDA,
    GOOGL, AAPL, META) and Berkshire carry NEGATIVE net debt — more
    cash on the balance sheet than total debt. A net-cash company
    has no leverage to service, so the leverage gate must pass
    UNCONDITIONALLY regardless of EBITDA sign. The old C3 logic
    dropped net-cash names with temporarily negative EBITDA via
    degenerate_denominator:ebitda when it should pass cleanly.

    Sentinel: net-cash companies return 0.0 as the sentinel for
    "no leverage". Downstream phases (3.5, 4, 5, 6) cannot
    distinguish "exactly zero leverage" from "net cash" via this
    field alone. Acceptable for C3.7 — the distinction doesn't
    affect any downstream gate logic. If it ever does, introduce
    a separate leverage_state field in a future sprint.

    New failure mode: positive net debt AND negative/zero EBITDA.
    This is a genuine credit risk (real leverage, no earnings to
    service it) and is rejected with a more specific reason.
    """
    net_debt = raw.total_debt - raw.cash

    # Net cash short-circuit — company has more cash than debt,
    # no leverage to service, pass unconditionally.
    if net_debt <= 0:
        return 0.0

    # Positive net debt with no earnings to service it — genuine
    # credit risk. Reject with a specific reason that distinguishes
    # this case from the clean net-cash path above.
    if raw.ebitda <= 0:
        raise _DropReason("degenerate_denominator:positive_net_debt_negative_ebitda")

    ratio = net_debt / raw.ebitda
    if math.isnan(ratio) or math.isinf(ratio):
        raise _DropReason("nan_computation:net_debt_to_ebitda")
    return ratio


def _compute_roic(raw: _RawFundamentals) -> float:
    """Return on Invested Capital: NOPAT / invested_capital.

    NOPAT = EBIT * (1 - effective_tax_rate)
    invested_capital = total_debt + stockholders_equity

    Raises _DropReason if invested_capital is zero or negative.
    """
    invested_capital = raw.total_debt + raw.stockholders_equity
    if invested_capital <= 0:
        raise _DropReason("degenerate_denominator:invested_capital")

    nopat = raw.ebit * (1.0 - raw.effective_tax_rate)
    roic = nopat / invested_capital
    if math.isnan(roic) or math.isinf(roic):
        raise _DropReason("nan_computation:roic")
    return roic


@dataclass(frozen=True, slots=True)
class _Metrics:
    altman_z: float
    fcf_yield: float
    net_debt_to_ebitda: float
    roic: float
    short_interest_pct: float


def _compute_all_metrics(raw: _RawFundamentals) -> _Metrics:
    """Run all five metric computations. Each may raise _DropReason."""
    return _Metrics(
        altman_z=_compute_altman_z(raw),
        fcf_yield=_compute_fcf_yield(raw),
        net_debt_to_ebitda=_compute_net_debt_to_ebitda(raw),
        roic=_compute_roic(raw),
        short_interest_pct=raw.short_interest_pct,
    )


# ---------------------------------------------------------------------------
# Filter predicate — pure function on _Metrics
# ---------------------------------------------------------------------------

def _passes_fortress_filters(m: _Metrics) -> bool:
    """All five gates must pass.

    Predicate matches the dispatch's exact comparison operators:
      altman_z              > MIN_ALTMAN_Z          (strict)
      fcf_yield             >= MIN_FCF_YIELD
      net_debt_to_ebitda    <= MAX_NET_DEBT_TO_EBITDA
      roic                  >= MIN_ROIC
      short_interest_pct    <= MAX_SHORT_INTEREST
    """
    if m.altman_z <= config.MIN_ALTMAN_Z:
        return False
    if m.fcf_yield < config.MIN_FCF_YIELD:
        return False
    if m.net_debt_to_ebitda > config.MAX_NET_DEBT_TO_EBITDA:
        return False
    if m.roic < config.MIN_ROIC:
        return False
    if m.short_interest_pct > config.MAX_SHORT_INTEREST:
        return False
    return True


# ---------------------------------------------------------------------------
# Default yfinance Ticker factory — production wiring
# ---------------------------------------------------------------------------

def _default_yf_ticker_factory(symbol: str) -> Any:
    """Production yfinance Ticker factory.

    Wraps yfinance.Ticker(symbol). Importing yfinance is lazy so the
    module can be unit-tested without yfinance installed (tests inject
    a synthetic factory).
    """
    import yfinance as yf
    return yf.Ticker(symbol)


# ---------------------------------------------------------------------------
# Per-ticker processing — extracted into a helper so the orchestrator's
# heartbeat counter fires unconditionally regardless of survival path.
# ---------------------------------------------------------------------------

def _process_one_ticker(
    upstream: TechnicalCandidate,
    factory: Callable[[str], Any],
) -> tuple[FundamentalCandidate | None, str]:
    """Process a single ticker through the Phase 3 pipeline.

    Returns:
        (FundamentalCandidate, "survived")  on full pass
        (None, "no_data")                   on data fetch / extraction failure
        (None, "filter_fail")               on metric out-of-bounds

    All branches log a structured warning before returning. The orchestrator
    only needs to read the second tuple element to update its counters.
    """
    ticker = upstream.ticker

    # ── Step 1: instantiate the yfinance Ticker (or test fake) ────
    try:
        ticker_obj = factory(ticker)
    except Exception as exc:
        logger.warning(
            "[screener.fundamentals] TICKER_DROPPED_PHASE3_NO_DATA "
            "ticker=%s reason=factory_raised:%s",
            ticker, type(exc).__name__,
        )
        return None, "no_data"

    # ── Step 2: extract raw fundamentals + compute metrics ────────
    try:
        raw = _extract_fundamentals(ticker_obj, upstream.market_cap_usd)
        metrics = _compute_all_metrics(raw)
    except _DropReason as drop:
        logger.warning(
            "[screener.fundamentals] TICKER_DROPPED_PHASE3_NO_DATA "
            "ticker=%s reason=%s",
            ticker, drop.reason,
        )
        return None, "no_data"
    except Exception as exc:
        # Defensive: anything unexpected is treated as no_data. We never
        # let an exception escape this function — a single bad ticker
        # can't kill the batch.
        logger.warning(
            "[screener.fundamentals] TICKER_DROPPED_PHASE3_NO_DATA "
            "ticker=%s reason=unexpected:%s",
            ticker, type(exc).__name__,
        )
        return None, "no_data"

    # ── Step 3: apply the Fortress Five gate ──────────────────────
    if not _passes_fortress_filters(metrics):
        logger.warning(
            "[screener.fundamentals] TICKER_DROPPED_PHASE3_NO_DATA "
            "ticker=%s reason=filter_fail "
            "altman_z=%.2f fcf_yield=%.4f nd_ebitda=%.2f roic=%.4f si=%.4f",
            ticker, metrics.altman_z, metrics.fcf_yield,
            metrics.net_debt_to_ebitda, metrics.roic, metrics.short_interest_pct,
        )
        return None, "filter_fail"

    # ── Step 4: construct the survivor ────────────────────────────
    candidate = FundamentalCandidate.from_technical(
        upstream,
        altman_z=metrics.altman_z,
        fcf_yield=metrics.fcf_yield,
        net_debt_to_ebitda=metrics.net_debt_to_ebitda,
        roic=metrics.roic,
        short_interest_pct=metrics.short_interest_pct,
    )
    return candidate, "survived"


# ---------------------------------------------------------------------------
# Phase 3 orchestrator — sync (yfinance has no async API)
# ---------------------------------------------------------------------------

def run_phase_3(
    candidates: list[TechnicalCandidate],
    *,
    yf_ticker_factory: Callable[[str], Any] | None = None,
    heartbeat_interval: int = HEARTBEAT_INTERVAL,
) -> list[FundamentalCandidate]:
    """Execute Phase 3: per-ticker yfinance fundamentals + Fortress Five gate.

    Args:
        candidates: Phase 2 output. Empty list returns empty list.
        yf_ticker_factory: optional injection point for tests. Default
            wraps yfinance.Ticker. Tests inject a factory returning
            synthetic objects with .info/.balance_sheet/.income_stmt/
            .cashflow attributes.
        heartbeat_interval: log progress every N tickers (0 to disable).

    Returns:
        List of FundamentalCandidate survivors. Failed/missing tickers
        are dropped with structured reason logs (fail-closed). NO
        fallback to any other data source.
    """
    if not candidates:
        logger.info("[screener.fundamentals] Phase 3: empty input, returning empty result")
        return []

    factory = yf_ticker_factory if yf_ticker_factory is not None else _default_yf_ticker_factory
    total = len(candidates)

    logger.info(
        "[screener.fundamentals] Phase 3 (yfinance per-ticker fundamentals): "
        "starting with %d candidates",
        total,
    )

    survivors: list[FundamentalCandidate] = []
    n_no_data = 0
    n_filter_fail = 0
    start_ts = time.monotonic()

    for idx, upstream in enumerate(candidates, start=1):
        candidate, status = _process_one_ticker(upstream, factory)
        if status == "survived":
            survivors.append(candidate)  # type: ignore[arg-type]
        elif status == "no_data":
            n_no_data += 1
        elif status == "filter_fail":
            n_filter_fail += 1

        # Heartbeat — every N tickers, fires regardless of survival path
        if heartbeat_interval > 0 and idx % heartbeat_interval == 0:
            logger.info(
                "[screener.fundamentals] Phase 3 progress: %d/%d processed, "
                "survivors so far: %d",
                idx, total, len(survivors),
            )

    elapsed = time.monotonic() - start_ts
    logger.info(
        "[screener.fundamentals] Phase 3 complete: processed=%d "
        "survivors=%d dropped=%d (by reason: no_data=%d filter_fail=%d) "
        "elapsed=%.1fs",
        total, len(survivors), total - len(survivors),
        n_no_data, n_filter_fail, elapsed,
    )
    return survivors
