"""
tests/test_screener_vol_event_armor.py

Unit tests for agt_equities.screener.vol_event_armor (Phase 4: IVR
gate via IBKR reqHistoricalDataAsync + corporate calendar gates via
YFinanceCorporateIntelligenceProvider).

Mocking strategy: synthetic FakeIB and FakeCalendarProvider. Neither
makes any real network calls. All IBKR bar data is constructed from
hand-crafted float lists via the make_bars helper. All calendar data
is constructed from CorporateCalendarDTO literals with explicit
next_earnings / ex_dividend_date / pending_corporate_action fields.

NO live ib_async.IB, NO yfinance, NO httpx.

Test matrix (32 tests, matching the C4 dispatch's specification):

  IVR gate (6):
    1.  IVR 30.0 exactly → passes (>=)
    2.  IVR 30.1 → passes
    3.  IVR 29.9 → drops (ivr_below)
    4.  IVR 95.7 (MSFT-like) → passes
    5.  IVR 50.0 → passes
    6.  IVR math spot-check: known inputs → known output within 0.01

  IV data failures (6):
    7.  Empty bars → drops (iv_insufficient)
    8.  150 bars (< MIN_IV_BARS=200) → drops (iv_insufficient)
    9.  250 bars with 100 nulls → drops (iv_nulls)
   10.  iv_max == iv_min → drops (iv_degenerate)
   11.  qualifyContractsAsync returns [] → drops (qualify_failed)
   12.  reqHistoricalDataAsync raises → drops (ibkr_error, NOT iv_insufficient)

  Earnings gate (6):
   13.  earnings in +5 days → drops (earnings)
   14.  earnings in +10 days → drops (inclusive upper bound)
   15.  earnings in +11 days → passes
   16.  earnings None → passes (skip gate)
   17.  earnings in -5 days (past) → passes
   18.  earnings today (0 days) → drops

  Ex-dividend gate (3):
   19.  ex_div in +3 days → drops (ex_div)
   20.  ex_div in +6 days → passes
   21.  ex_div None → passes

  Corporate action gate (4):
   22.  MERGER → drops (corp_action)
   23.  SPINOFF → drops
   24.  NONE → passes
   25.  SPECIAL_DIVIDEND → drops

  Calendar provider failures (2):
   26.  provider returns None → drops (calendar_unavailable)
   27.  provider raises → drops (calendar_error, NOT calendar_unavailable)

  Structural (3):
   28.  Empty candidates → [] + zero IBKR calls
   29.  Per-ticker exception doesn't abort batch
   30.  Upstream CorrelationCandidate fields preserved verbatim

  Final log + rate limit (2):
   31.  Final log line contains all 13 required tokens
   32.  asyncio.sleep called the expected number of times at the
        courtesy-delay cadence (monkeypatched sleep counter)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

import pytest

from agt_equities.market_data_dtos import (
    CorporateActionType,
    CorporateCalendarDTO,
)
from agt_equities.screener import config, vol_event_armor
from agt_equities.screener.types import (
    CorrelationCandidate,
    VolArmorCandidate,
)


def _run(coro):
    """Sync wrapper for async test bodies — matches project convention."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake IBKR IB + bar data
# ---------------------------------------------------------------------------

@dataclass
class FakeBar:
    """Test double for ib_async BarData. Only .close matters here."""
    close: Optional[float]


@dataclass
class FakeContract:
    """Test double for a qualified ib_async.Contract."""
    symbol: str
    exchange: str = "SMART"
    currency: str = "USD"


class FakeIB:
    """Test double for ib_async.IB.

    qualifyContractsAsync: returns [FakeContract(symbol)] by default.
    reqHistoricalDataAsync: returns bars_by_ticker.get(symbol, []).

    Per-ticker failure injection:
      qualify_empty: set of tickers for which qualify returns []
      hist_raises: set of tickers for which reqHistoricalDataAsync raises
    """

    def __init__(
        self,
        bars_by_ticker: dict[str, list[FakeBar]],
        *,
        qualify_empty: Optional[set[str]] = None,
        hist_raises: Optional[set[str]] = None,
    ):
        self._bars = bars_by_ticker
        self._qualify_empty = qualify_empty or set()
        self._hist_raises = hist_raises or set()
        self.qualify_call_count = 0
        self.hist_call_count = 0

    async def qualifyContractsAsync(self, contract):
        self.qualify_call_count += 1
        symbol = getattr(contract, "symbol", "?")
        if symbol in self._qualify_empty:
            return []
        return [FakeContract(symbol=symbol)]

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        self.hist_call_count += 1
        symbol = getattr(contract, "symbol", "?")
        if symbol in self._hist_raises:
            raise RuntimeError(f"simulated IBKR error for {symbol}")
        return self._bars.get(symbol, [])


def make_bars(values: list[Optional[float]]) -> list[FakeBar]:
    """Build a synthetic IV bar series from a list of close values.
    None values become null bars that the IV nulls-filter will drop."""
    return [FakeBar(close=v) for v in values]


def make_ivr_bars(
    iv_min: float, iv_max: float, iv_latest: float, n: int = 250,
) -> list[FakeBar]:
    """Build n synthetic bars whose min/max/latest exactly match the
    provided values. The first bar is iv_min, the second-to-last is
    iv_max, the last is iv_latest, and the rest are midway."""
    if n < 3:
        raise ValueError("n must be >= 3 for make_ivr_bars")
    mid_value = (iv_min + iv_max) / 2.0
    closes = [mid_value] * n
    closes[0] = iv_min
    closes[-2] = iv_max
    closes[-1] = iv_latest
    return make_bars(closes)


# ---------------------------------------------------------------------------
# Fake calendar provider
# ---------------------------------------------------------------------------

class FakeCalendarProvider:
    """Test double for YFinanceCorporateIntelligenceProvider.

    calendars_by_ticker: maps ticker to CorporateCalendarDTO, None, or
    a sentinel RuntimeError to raise.
    """

    def __init__(
        self,
        calendars_by_ticker: dict[str, Optional[CorporateCalendarDTO]],
        *,
        raise_on: Optional[set[str]] = None,
    ):
        self._calendars = calendars_by_ticker
        self._raise_on = raise_on or set()
        self.call_count = 0

    def get_corporate_calendar(self, ticker: str) -> Optional[CorporateCalendarDTO]:
        self.call_count += 1
        if ticker in self._raise_on:
            raise RuntimeError(f"simulated calendar fetch failure: {ticker}")
        return self._calendars.get(ticker)


def make_calendar(
    ticker: str = "TEST",
    *,
    next_earnings: Optional[date] = None,
    ex_dividend_date: Optional[date] = None,
    pending_corporate_action: CorporateActionType = CorporateActionType.NONE,
    data_source: str = "yfinance_temporary",
) -> CorporateCalendarDTO:
    """Build a CorporateCalendarDTO with defaults that pass all gates."""
    return CorporateCalendarDTO(
        symbol=ticker,
        next_earnings=next_earnings,
        ex_dividend_date=ex_dividend_date,
        dividend_amount=0.0,
        pending_corporate_action=pending_corporate_action,
        data_source=data_source,
        cached_at=datetime(2026, 4, 11, 10, 0, 0),
        cache_age_hours=0.0,
    )


# ---------------------------------------------------------------------------
# Synthetic CorrelationCandidate builder
# ---------------------------------------------------------------------------

def make_correlation_candidate(
    ticker: str = "TEST",
    **overrides,
) -> CorrelationCandidate:
    """Synthetic CorrelationCandidate with safe defaults for Phase 4."""
    defaults = dict(
        ticker=ticker,
        name=f"{ticker} Inc",
        sector="Technology",
        country="US",
        market_cap_usd=50_000_000_000.0,
        spot=150.0,
        sma_200=140.0,
        rsi_14=42.0,
        bband_lower=148.5,
        bband_mid=152.0,
        bband_upper=155.5,
        lowest_low_21d=147.0,
        altman_z=4.5,
        fcf_yield=0.06,
        net_debt_to_ebitda=1.2,
        roic=0.18,
        short_interest_pct=0.04,
        max_abs_correlation=0.25,
        most_correlated_holding="HOLD1",
    )
    defaults.update(overrides)
    return CorrelationCandidate(**defaults)


# ---------------------------------------------------------------------------
# 1-6. IVR gate
# ---------------------------------------------------------------------------

def test_1_ivr_exactly_at_threshold_passes():
    """IVR = 30.0 exactly → (iv_latest - iv_min)/(iv_max - iv_min) == 0.30
    → ivr_pct == 30.0 → >= 30.0 passes.
    Construction: iv_min=0.1, iv_max=0.2, iv_latest=0.13 → ivr=30.0
    """
    bars = make_ivr_bars(iv_min=0.1, iv_max=0.2, iv_latest=0.13, n=250)
    fake_ib = FakeIB(bars_by_ticker={"BOUND": bars})
    provider = FakeCalendarProvider({"BOUND": make_calendar("BOUND")})
    cand = make_correlation_candidate("BOUND")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1
    assert 29.99 <= result[0].ivr_pct <= 30.01


def test_2_ivr_just_above_threshold_passes():
    """IVR = 30.1 → passes."""
    bars = make_ivr_bars(iv_min=0.10, iv_max=0.20, iv_latest=0.1301, n=250)
    fake_ib = FakeIB(bars_by_ticker={"JUSTOVER": bars})
    provider = FakeCalendarProvider({"JUSTOVER": make_calendar("JUSTOVER")})
    cand = make_correlation_candidate("JUSTOVER")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1
    assert result[0].ivr_pct > 30.0


def test_3_ivr_below_threshold_drops(caplog):
    """IVR = 20 → below 30.0 floor → drops with ivr_below_floor."""
    # iv_min=0.10, iv_max=0.20, iv_latest=0.12 → ivr = (0.12-0.10)/0.10 * 100 = 20%
    bars = make_ivr_bars(iv_min=0.10, iv_max=0.20, iv_latest=0.12, n=250)
    fake_ib = FakeIB(bars_by_ticker={"LOWIVR": bars})
    provider = FakeCalendarProvider({"LOWIVR": make_calendar("LOWIVR")})
    cand = make_correlation_candidate("LOWIVR")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("IVR_BELOW_FLOOR" in r.message for r in caplog.records)


def test_4_ivr_high_percentile_passes():
    """MSFT-like IVR = 95.7 → well above floor → passes."""
    # iv_min=0.15, iv_max=0.38, iv_latest ≈ 0.37
    # ivr = (0.37 - 0.15)/(0.38 - 0.15) * 100 ≈ 95.65
    bars = make_ivr_bars(iv_min=0.15, iv_max=0.38, iv_latest=0.37, n=250)
    fake_ib = FakeIB(bars_by_ticker={"MSFT": bars})
    provider = FakeCalendarProvider({"MSFT": make_calendar("MSFT")})
    cand = make_correlation_candidate("MSFT")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1
    assert result[0].ivr_pct > 90.0


def test_5_ivr_midrange_passes():
    """IVR = 50.0 → passes comfortably."""
    bars = make_ivr_bars(iv_min=0.10, iv_max=0.20, iv_latest=0.15, n=250)
    fake_ib = FakeIB(bars_by_ticker={"MID": bars})
    provider = FakeCalendarProvider({"MID": make_calendar("MID")})
    cand = make_correlation_candidate("MID")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1
    assert 49.99 <= result[0].ivr_pct <= 50.01


def test_6_ivr_math_spot_check():
    """Known inputs reproduce the expected IVR to 0.01.

    iv_min=0.1671, iv_max=0.4905, iv_latest=0.2775 → IVR=34.08 (matches
    the AAPL probe result from 2026-04-11 within rounding).
    """
    bars = make_ivr_bars(iv_min=0.1671, iv_max=0.4905, iv_latest=0.2775, n=250)
    fake_ib = FakeIB(bars_by_ticker={"AAPL": bars})
    provider = FakeCalendarProvider({"AAPL": make_calendar("AAPL")})
    cand = make_correlation_candidate("AAPL")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1
    expected = (0.2775 - 0.1671) / (0.4905 - 0.1671) * 100
    assert abs(result[0].ivr_pct - expected) < 0.01
    # Also check the sibling IV fields are carried through
    assert abs(result[0].iv_latest - 0.2775) < 1e-9
    assert abs(result[0].iv_52w_min - 0.1671) < 1e-9
    assert abs(result[0].iv_52w_max - 0.4905) < 1e-9
    assert result[0].iv_bars_used == 250


# ---------------------------------------------------------------------------
# 7-12. IV data failures
# ---------------------------------------------------------------------------

def test_7_empty_bars_drops(caplog):
    fake_ib = FakeIB(bars_by_ticker={"EMPTY": []})
    provider = FakeCalendarProvider({"EMPTY": make_calendar("EMPTY")})
    cand = make_correlation_candidate("EMPTY")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("IV_INSUFFICIENT" in r.message for r in caplog.records)


def test_8_150_bars_drops(caplog):
    """150 bars is below MIN_IV_BARS=200 → drops iv_insufficient."""
    bars = make_ivr_bars(iv_min=0.1, iv_max=0.3, iv_latest=0.2, n=150)
    fake_ib = FakeIB(bars_by_ticker={"SHORT": bars})
    provider = FakeCalendarProvider({"SHORT": make_calendar("SHORT")})
    cand = make_correlation_candidate("SHORT")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("IV_INSUFFICIENT" in r.message for r in caplog.records)


def test_9_bars_with_many_nulls_drops(caplog):
    """250 bars but 100 have None close → only 150 valid, below MIN_IV_BARS
    → drops iv_nulls (NOT iv_insufficient — the bar count itself is fine)."""
    closes: list[Optional[float]] = [0.15] * 250
    # Null out 100 values spread through the series
    for i in range(0, 200, 2):
        closes[i] = None
    bars = make_bars(closes)
    fake_ib = FakeIB(bars_by_ticker={"NULLS": bars})
    provider = FakeCalendarProvider({"NULLS": make_calendar("NULLS")})
    cand = make_correlation_candidate("NULLS")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("IV_NULLS" in r.message for r in caplog.records)


def test_10_degenerate_iv_range_drops(caplog):
    """All 250 bars identical → iv_max == iv_min → degenerate."""
    bars = make_bars([0.20] * 250)
    fake_ib = FakeIB(bars_by_ticker={"FLAT": bars})
    provider = FakeCalendarProvider({"FLAT": make_calendar("FLAT")})
    cand = make_correlation_candidate("FLAT")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("IV_DEGENERATE" in r.message for r in caplog.records)


def test_11_qualify_failed_drops(caplog):
    """qualifyContractsAsync returns [] → drops qualify_failed."""
    fake_ib = FakeIB(bars_by_ticker={}, qualify_empty={"BADQUAL"})
    provider = FakeCalendarProvider({"BADQUAL": make_calendar("BADQUAL")})
    cand = make_correlation_candidate("BADQUAL")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("QUALIFY_FAILED" in r.message for r in caplog.records)


def test_12_ibkr_exception_drops_correctly(caplog):
    """reqHistoricalDataAsync raises → drops ibkr_error, NOT iv_insufficient."""
    fake_ib = FakeIB(bars_by_ticker={}, hist_raises={"BADHIST"})
    provider = FakeCalendarProvider({"BADHIST": make_calendar("BADHIST")})
    cand = make_correlation_candidate("BADHIST")
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    # Must be IBKR_ERROR, NOT IV_INSUFFICIENT
    assert any("IBKR_ERROR" in r.message for r in caplog.records)
    assert not any("IV_INSUFFICIENT" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 13-18. Earnings gate
# ---------------------------------------------------------------------------

def _make_happy_ib_and_bars(symbol: str) -> FakeIB:
    """Build a FakeIB that returns bars producing IVR ~50 for the given symbol."""
    bars = make_ivr_bars(iv_min=0.10, iv_max=0.20, iv_latest=0.15, n=250)
    return FakeIB(bars_by_ticker={symbol: bars})


def test_13_earnings_5_days_out_drops(caplog):
    today = date.today()
    provider = FakeCalendarProvider({
        "EARN5": make_calendar("EARN5", next_earnings=today + timedelta(days=5)),
    })
    fake_ib = _make_happy_ib_and_bars("EARN5")
    cand = make_correlation_candidate("EARN5")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("EARNINGS_BLACKOUT" in r.message for r in caplog.records)


def test_14_earnings_10_days_out_drops_inclusive(caplog):
    """Upper bound is INCLUSIVE — 10 days out still drops."""
    today = date.today()
    provider = FakeCalendarProvider({
        "EARN10": make_calendar("EARN10", next_earnings=today + timedelta(days=10)),
    })
    fake_ib = _make_happy_ib_and_bars("EARN10")
    cand = make_correlation_candidate("EARN10")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("EARNINGS_BLACKOUT" in r.message for r in caplog.records)


def test_15_earnings_11_days_out_passes():
    """One day outside the blackout → passes."""
    today = date.today()
    provider = FakeCalendarProvider({
        "EARN11": make_calendar("EARN11", next_earnings=today + timedelta(days=11)),
    })
    fake_ib = _make_happy_ib_and_bars("EARN11")
    cand = make_correlation_candidate("EARN11")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1


def test_16_earnings_none_passes():
    """No scheduled earnings → earnings gate skipped → passes."""
    provider = FakeCalendarProvider({
        "NOEARN": make_calendar("NOEARN", next_earnings=None),
    })
    fake_ib = _make_happy_ib_and_bars("NOEARN")
    cand = make_correlation_candidate("NOEARN")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1
    assert result[0].next_earnings is None


def test_17_earnings_past_passes():
    """Past earnings (days_to_earnings < 0) → passes (only future blocks)."""
    today = date.today()
    provider = FakeCalendarProvider({
        "PASTEARN": make_calendar("PASTEARN", next_earnings=today - timedelta(days=5)),
    })
    fake_ib = _make_happy_ib_and_bars("PASTEARN")
    cand = make_correlation_candidate("PASTEARN")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1


def test_18_earnings_today_drops(caplog):
    """Earnings today (days_to_earnings == 0) → drops (inclusive lower bound)."""
    today = date.today()
    provider = FakeCalendarProvider({
        "EARN0": make_calendar("EARN0", next_earnings=today),
    })
    fake_ib = _make_happy_ib_and_bars("EARN0")
    cand = make_correlation_candidate("EARN0")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("EARNINGS_BLACKOUT" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 19-21. Ex-dividend gate
# ---------------------------------------------------------------------------

def test_19_ex_div_3_days_out_drops(caplog):
    today = date.today()
    provider = FakeCalendarProvider({
        "EXDIV3": make_calendar("EXDIV3", ex_dividend_date=today + timedelta(days=3)),
    })
    fake_ib = _make_happy_ib_and_bars("EXDIV3")
    cand = make_correlation_candidate("EXDIV3")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("EX_DIV_BLACKOUT" in r.message for r in caplog.records)


def test_20_ex_div_6_days_out_passes():
    today = date.today()
    provider = FakeCalendarProvider({
        "EXDIV6": make_calendar("EXDIV6", ex_dividend_date=today + timedelta(days=6)),
    })
    fake_ib = _make_happy_ib_and_bars("EXDIV6")
    cand = make_correlation_candidate("EXDIV6")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1


def test_21_ex_div_none_passes():
    provider = FakeCalendarProvider({
        "NODIV": make_calendar("NODIV", ex_dividend_date=None),
    })
    fake_ib = _make_happy_ib_and_bars("NODIV")
    cand = make_correlation_candidate("NODIV")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1


# ---------------------------------------------------------------------------
# 22-25. Corporate action gate
# ---------------------------------------------------------------------------

def test_22_merger_drops(caplog):
    provider = FakeCalendarProvider({
        "MERGER": make_calendar("MERGER", pending_corporate_action=CorporateActionType.MERGER),
    })
    fake_ib = _make_happy_ib_and_bars("MERGER")
    cand = make_correlation_candidate("MERGER")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("CORP_ACTION" in r.message and "merger" in r.message for r in caplog.records)


def test_23_spinoff_drops(caplog):
    provider = FakeCalendarProvider({
        "SPIN": make_calendar("SPIN", pending_corporate_action=CorporateActionType.SPINOFF),
    })
    fake_ib = _make_happy_ib_and_bars("SPIN")
    cand = make_correlation_candidate("SPIN")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("CORP_ACTION" in r.message and "spinoff" in r.message for r in caplog.records)


def test_24_none_corp_action_passes():
    provider = FakeCalendarProvider({
        "CLEAN": make_calendar("CLEAN"),  # default is NONE
    })
    fake_ib = _make_happy_ib_and_bars("CLEAN")
    cand = make_correlation_candidate("CLEAN")
    result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert len(result) == 1


def test_25_special_dividend_drops(caplog):
    provider = FakeCalendarProvider({
        "SPECDIV": make_calendar(
            "SPECDIV",
            pending_corporate_action=CorporateActionType.SPECIAL_DIVIDEND,
        ),
    })
    fake_ib = _make_happy_ib_and_bars("SPECDIV")
    cand = make_correlation_candidate("SPECDIV")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("CORP_ACTION" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 26-27. Calendar provider failures
# ---------------------------------------------------------------------------

def test_26_provider_returns_none_drops(caplog):
    provider = FakeCalendarProvider({"NOCAL": None})
    fake_ib = _make_happy_ib_and_bars("NOCAL")
    cand = make_correlation_candidate("NOCAL")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    assert any("CALENDAR_UNAVAILABLE" in r.message for r in caplog.records)


def test_27_provider_raises_drops_correctly(caplog):
    """Provider raises → calendar_error counter, NOT calendar_unavailable."""
    provider = FakeCalendarProvider({}, raise_on={"BADCAL"})
    fake_ib = _make_happy_ib_and_bars("BADCAL")
    cand = make_correlation_candidate("BADCAL")
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.vol_event_armor"):
        result = _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    assert result == []
    # Must be CALENDAR_ERROR, NOT CALENDAR_UNAVAILABLE
    assert any("CALENDAR_ERROR" in r.message for r in caplog.records)
    assert not any("CALENDAR_UNAVAILABLE" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 28-30. Structural
# ---------------------------------------------------------------------------

def test_28_empty_candidates_no_ibkr_calls():
    fake_ib = FakeIB(bars_by_ticker={})
    provider = FakeCalendarProvider({})
    result = _run(vol_event_armor.run_phase_4([], fake_ib, provider))
    assert result == []
    assert fake_ib.qualify_call_count == 0
    assert fake_ib.hist_call_count == 0
    assert provider.call_count == 0


def test_29_per_ticker_exception_does_not_abort_batch():
    """One candidate with an IBKR exception, one clean candidate.
    The batch must process both — the clean one must survive."""
    good_bars = make_ivr_bars(iv_min=0.10, iv_max=0.20, iv_latest=0.15, n=250)
    fake_ib = FakeIB(
        bars_by_ticker={"GOOD": good_bars},
        hist_raises={"BAD"},
    )
    provider = FakeCalendarProvider({
        "GOOD": make_calendar("GOOD"),
        "BAD": make_calendar("BAD"),
    })
    bad_cand = make_correlation_candidate("BAD")
    good_cand = make_correlation_candidate("GOOD")
    # BAD first to prove its exception doesn't kill the loop
    result = _run(vol_event_armor.run_phase_4([bad_cand, good_cand], fake_ib, provider))
    assert len(result) == 1
    assert result[0].ticker == "GOOD"


def test_30_upstream_fields_preserved():
    """All 19 CorrelationCandidate fields must appear verbatim on the
    output VolArmorCandidate."""
    upstream = CorrelationCandidate(
        ticker="CARRY",
        name="Carry Industries",
        sector="Technology",
        country="US",
        market_cap_usd=80_000_000_000.0,
        spot=200.0,
        sma_200=185.0,
        rsi_14=39.5,
        bband_lower=198.0,
        bband_mid=205.0,
        bband_upper=212.0,
        lowest_low_21d=195.5,
        altman_z=8.4,
        fcf_yield=0.07,
        net_debt_to_ebitda=0.5,
        roic=0.28,
        short_interest_pct=0.012,
        max_abs_correlation=0.42,
        most_correlated_holding="SOMEHOLD",
    )
    bars = make_ivr_bars(iv_min=0.15, iv_max=0.35, iv_latest=0.25, n=250)
    fake_ib = FakeIB(bars_by_ticker={"CARRY": bars})
    provider = FakeCalendarProvider({"CARRY": make_calendar("CARRY")})
    result = _run(vol_event_armor.run_phase_4([upstream], fake_ib, provider))
    assert len(result) == 1
    out = result[0]
    assert out.ticker == "CARRY"
    assert out.name == "Carry Industries"
    assert out.sector == "Technology"
    assert out.country == "US"
    assert out.market_cap_usd == 80_000_000_000.0
    assert out.spot == 200.0
    assert out.sma_200 == 185.0
    assert out.rsi_14 == 39.5
    assert out.bband_lower == 198.0
    assert out.bband_mid == 205.0
    assert out.bband_upper == 212.0
    assert out.lowest_low_21d == 195.5
    assert out.altman_z == 8.4
    assert out.fcf_yield == 0.07
    assert out.net_debt_to_ebitda == 0.5
    assert out.roic == 0.28
    assert out.short_interest_pct == 0.012
    assert out.max_abs_correlation == 0.42
    assert out.most_correlated_holding == "SOMEHOLD"
    assert out.calendar_source == "yfinance_temporary"


# ---------------------------------------------------------------------------
# 31-32. Final log + rate limit
# ---------------------------------------------------------------------------

def test_31_final_log_line_contains_all_required_tokens(caplog):
    bars = make_ivr_bars(iv_min=0.10, iv_max=0.20, iv_latest=0.15, n=250)
    fake_ib = FakeIB(bars_by_ticker={"LOG": bars})
    provider = FakeCalendarProvider({"LOG": make_calendar("LOG")})
    cand = make_correlation_candidate("LOG")
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.vol_event_armor"):
        _run(vol_event_armor.run_phase_4([cand], fake_ib, provider))
    final_lines = [r.message for r in caplog.records if "Phase 4 complete" in r.message]
    assert len(final_lines) == 1
    line = final_lines[0]
    required_tokens = (
        "processed=", "survivors=", "dropped=", "elapsed=",
        "qualify_failed=", "iv_insufficient=", "iv_nulls=",
        "iv_degenerate=", "ibkr_error=", "ivr_below=",
        "earnings=", "ex_div=", "corp_action=",
        "calendar_unavailable=", "calendar_error=",
    )
    for token in required_tokens:
        assert token in line, f"Missing token {token!r} in final log line: {line}"


def test_32_rate_limit_courtesy_delay_called(monkeypatch):
    """asyncio.sleep must be called exactly once per candidate that
    reaches the delay line (after the IBKR block succeeds).

    3 candidates: 1 IBKR-error (no sleep), 1 calendar-drop (reaches sleep
    but fails the calendar block), 1 happy path (reaches sleep + passes).
    Expected sleep call count: 2.
    """
    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        # Don't actually sleep — yield control instantly
        await real_sleep(0)

    monkeypatch.setattr(
        vol_event_armor.asyncio, "sleep", fake_sleep,
    )

    good_bars = make_ivr_bars(iv_min=0.10, iv_max=0.20, iv_latest=0.15, n=250)
    fake_ib = FakeIB(
        bars_by_ticker={"HAPPY": good_bars, "NOCAL": good_bars},
        hist_raises={"BADIB"},
    )
    provider = FakeCalendarProvider({
        "HAPPY": make_calendar("HAPPY"),
        "NOCAL": None,  # calendar returns None → drops AFTER the sleep
        "BADIB": make_calendar("BADIB"),  # never reached
    })
    candidates = [
        make_correlation_candidate("BADIB"),
        make_correlation_candidate("NOCAL"),
        make_correlation_candidate("HAPPY"),
    ]
    result = _run(vol_event_armor.run_phase_4(candidates, fake_ib, provider))
    assert len(result) == 1
    assert result[0].ticker == "HAPPY"
    # BADIB raised before sleep, NOCAL reached sleep then failed calendar,
    # HAPPY reached sleep and passed → 2 sleep calls total
    assert len(sleep_calls) == 2
    for delay in sleep_calls:
        assert abs(delay - config.IBKR_HIST_DATA_COURTESY_DELAY_S) < 1e-9
