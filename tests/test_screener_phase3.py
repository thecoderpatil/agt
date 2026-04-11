"""
tests/test_screener_phase3.py

Unit tests for agt_equities.screener.fundamentals (Phase 3: per-ticker
yfinance fundamentals + Fortress Five gate).

Mocking strategy: synthetic FakeTicker class with settable .info /
.balance_sheet / .income_stmt / .cashflow attributes. Tests inject a
factory function that returns FakeTicker instances keyed on ticker
symbol. NO live yfinance, NO network, NO httpx.

Test matrix (26 tests, matching the dispatch's specification):

  Happy path + boundary (7):
    1.  All five metrics in range → survives
    2.  Altman Z exactly at 3.0 → FAIL (strict >)
    3.  Altman Z just above 3.0 (3.01) → PASS
    4.  FCF yield exactly at 0.04 → PASS (>=)
    5.  ND/EBITDA exactly at 3.0 → PASS (<=)
    6.  ROIC exactly at 0.10 → PASS (>=)
    7.  Short interest exactly at 0.10 → PASS (<=)

  Filter failures (5):
    8.  Altman Z below 3.0 → drop (filter_fail)
    9.  FCF yield below 0.04 → drop
    10. ND/EBITDA above 3.0 → drop
    11. ROIC below 0.10 → drop
    12. Short interest above 0.10 → drop

  Data failures (8):
    13. ticker.info raises → drop, info_fetch_failed
    14. balance_sheet empty → drop (statements_unavailable or field_missing)
    15. income_stmt missing EBIT → drop, field_missing:EBIT
    16. cashflow missing operating cash flow → drop
    17. ebitda <= 0 → drop, degenerate_denominator:ebitda
    18. invested_capital <= 0 → drop, degenerate_denominator:invested_capital
    19. Short interest missing from both fields → drop, short_interest_unavailable
    20. Total assets <= 0 → drop, degenerate_denominator:total_assets

  Structural (6):
    21. Empty input list → returns empty list, no crash
    22. Single bad-data ticker does not abort the batch
    23. Heartbeat fires at the 10-ticker boundary (caplog assertion)
    24. Final log line contains all four required tokens
    25. Upstream fields preserved verbatim through carry-forward
    26. fundamentals.py does NOT import FinnhubClient (AST assertion)
"""
from __future__ import annotations

import ast
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from agt_equities.screener import config, fundamentals
from agt_equities.screener.types import FundamentalCandidate, TechnicalCandidate


# ---------------------------------------------------------------------------
# Synthetic Ticker fixture — drop-in replacement for yfinance.Ticker
# ---------------------------------------------------------------------------

@dataclass
class FakeTicker:
    """Test double for yfinance.Ticker. Exposes the four attributes
    fundamentals.py reads: info, balance_sheet, income_stmt, cashflow.

    Set `raise_on_info=True` to simulate ticker.info raising on access
    (rate limit / network error).
    """
    info: dict = field(default_factory=dict)
    balance_sheet: pd.DataFrame = field(default_factory=pd.DataFrame)
    income_stmt: pd.DataFrame = field(default_factory=pd.DataFrame)
    cashflow: pd.DataFrame = field(default_factory=pd.DataFrame)
    raise_on_info: bool = False

    def __post_init__(self):
        # Replace info with a property-like raiser when requested.
        # Implemented by stashing the raise flag and using __getattribute__.
        pass


def _make_factory(tickers: dict[str, FakeTicker]):
    """Build a factory function compatible with run_phase_3's
    yf_ticker_factory parameter."""
    def factory(symbol: str) -> FakeTicker:
        if symbol not in tickers:
            raise KeyError(f"FakeTicker not configured for {symbol!r}")
        ft = tickers[symbol]
        if ft.raise_on_info:
            # Wrap in a class whose .info raises on access
            class _RaisingTicker:
                @property
                def info(self):
                    raise RuntimeError("simulated network/rate-limit error")
                balance_sheet = ft.balance_sheet
                income_stmt = ft.income_stmt
                cashflow = ft.cashflow
            return _RaisingTicker()
        return ft
    return factory


def _bs(values: dict[str, float]) -> pd.DataFrame:
    """Build a balance_sheet-shaped DataFrame from a {label: value} dict.
    yfinance shape: rows are line items, columns are fiscal periods (newest first)."""
    return pd.DataFrame({pd.Timestamp("2025-12-31"): values})


def _is(values: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame({pd.Timestamp("2025-12-31"): values})


def _cf(values: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame({pd.Timestamp("2025-12-31"): values})


def _make_universe_tc(
    ticker: str = "TEST",
    market_cap_usd: float = 50_000_000_000.0,
) -> TechnicalCandidate:
    """Build a synthetic TechnicalCandidate to feed into Phase 3."""
    return TechnicalCandidate(
        ticker=ticker,
        name=f"{ticker} Inc",
        sector="Technology",
        country="US",
        market_cap_usd=market_cap_usd,
        current_price=150.0,
        sma_200=140.0,
        rsi_14=42.0,
        bband_lower=148.5,
        bband_middle=152.0,
        bband_upper=155.5,
        lowest_low_21d=147.0,
    )


def _make_happy_ticker(market_cap_usd: float = 50_000_000_000.0) -> FakeTicker:
    """A FakeTicker constructed so all five Phase 3 metrics pass with margin.

    Numbers chosen so:
        Altman Z ≈ 3.79  (above 3.0)
        FCF Yield ≈ 0.40 (above 0.04)
        ND/EBITDA ≈ 0.057 (below 3.0)
        ROIC ≈ 0.279     (above 0.10)
        SI ≈ 0.05        (at/below 0.10)

    With market_cap = $50B:
        total_assets = 100B, working_capital = 15B, retained_earnings = 30B,
        ebit = 30B, total_liabilities = 25B, revenue = 100B,
        ebitda = 35B, total_debt = 10B, cash = 8B,
        stockholders_equity = 75B, ocf = 25B, capex = -5B (signed)
    """
    return FakeTicker(
        info={
            "shortPercentOfFloat": 0.05,
            "effectiveTaxRate": 0.21,
        },
        balance_sheet=_bs({
            "Total Assets": 100_000_000_000,
            "Total Liabilities Net Minority Interest": 25_000_000_000,
            "Working Capital": 15_000_000_000,
            "Retained Earnings": 30_000_000_000,
            "Stockholders Equity": 75_000_000_000,
            "Total Debt": 10_000_000_000,
            "Cash And Cash Equivalents": 8_000_000_000,
        }),
        income_stmt=_is({
            "Total Revenue": 100_000_000_000,
            "EBIT": 30_000_000_000,
            "EBITDA": 35_000_000_000,
        }),
        cashflow=_cf({
            "Operating Cash Flow": 25_000_000_000,
            "Capital Expenditure": -5_000_000_000,
        }),
    )


# ---------------------------------------------------------------------------
# Pre-flight: confirm the happy ticker actually computes to the expected
# metrics. If this drifts, the boundary tests below need to be retuned.
# ---------------------------------------------------------------------------

def test_0_happy_ticker_baseline_metrics():
    """Sanity check: the happy ticker produces metrics in known bands."""
    upstream = _make_universe_tc("BASELINE")
    factory = _make_factory({"BASELINE": _make_happy_ticker()})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert len(result) == 1
    c = result[0]
    # Altman Z = 1.2*0.15 + 1.4*0.30 + 3.3*0.30 + 0.6*(50/25) + 1.0*1.0
    #         = 0.18 + 0.42 + 0.99 + 1.20 + 1.00 = 3.79
    assert 3.7 <= c.altman_z <= 3.9
    # FCF yield = (25 - 5) / 50 = 0.40
    assert 0.39 <= c.fcf_yield <= 0.41
    # ND/EBITDA = (10 - 8) / 35 = 0.057
    assert 0.05 <= c.net_debt_to_ebitda <= 0.06
    # ROIC = (30 * 0.79) / (10 + 75) = 23.7 / 85 = 0.2788
    assert 0.27 <= c.roic <= 0.29
    assert c.short_interest_pct == 0.05


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

def test_1_all_five_metrics_in_range_survives():
    upstream = _make_universe_tc("AAPL")
    factory = _make_factory({"AAPL": _make_happy_ticker()})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert len(result) == 1
    assert result[0].ticker == "AAPL"
    assert isinstance(result[0], FundamentalCandidate)


# ---------------------------------------------------------------------------
# 2-7. Boundary tests
# ---------------------------------------------------------------------------

def _build_ticker_with_metrics(
    altman_z: float, fcf_yield: float, nd_ebitda: float,
    roic: float, short_interest: float,
    market_cap_usd: float = 50_000_000_000.0,
) -> FakeTicker:
    """Build a FakeTicker that computes to (approximately) the requested metrics.

    Strategy: hold most fields fixed and tune the one degree of freedom that
    each metric is most sensitive to.

    Z = 1.2*A + 1.4*B + 3.3*C + 0.6*D + 1.0*E with total_assets fixed = 100B
    so we tune working_capital (A) to hit the target Z given other fixed terms.
    Other terms: B=0.30, C=0.30, D depends on TL (we tune TL), E=1.0.
    Easier: tune total_liabilities to hit Z (D=mc/TL is the most sensitive).

    Actually simpler: build the happy ticker, then mutate ONE field at a time
    to hit each target metric. Done per-test-case below.
    """
    raise NotImplementedError("Use per-test mutation of _make_happy_ticker()")


def test_2_altman_z_exactly_at_threshold_fails():
    """Z = 3.0 exactly → strict >, fails.

    Use clean rationals that produce Z = 3.0 with no floating-point drift:
        A = 0.10  →  1.2 * 0.10 = 0.12
        B = 0.20  →  1.4 * 0.20 = 0.28
        C = 0.20  →  3.3 * 0.20 = 0.66
        D = 2.0   →  0.6 * 2.0  = 1.20
        E = 0.74  →  1.0 * 0.74 = 0.74
        Z = 0.12 + 0.28 + 0.66 + 1.20 + 0.74 = 3.00 exact
    market_cap = 50B, total_assets = 100B:
        working_capital = 10B, retained_earnings = 20B, ebit = 20B,
        total_liabilities = 25B (D = 50/25 = 2.0), revenue = 74B
    """
    upstream = _make_universe_tc("ZBOUND")
    ft = _make_happy_ticker()
    ft.balance_sheet = _bs({
        "Total Assets": 100_000_000_000,
        "Total Liabilities Net Minority Interest": 25_000_000_000,
        "Working Capital": 10_000_000_000,
        "Retained Earnings": 20_000_000_000,
        "Stockholders Equity": 75_000_000_000,
        "Total Debt": 10_000_000_000,
        "Cash And Cash Equivalents": 8_000_000_000,
    })
    ft.income_stmt = _is({
        "Total Revenue": 74_000_000_000,
        "EBIT": 20_000_000_000,
        "EBITDA": 35_000_000_000,
    })
    factory = _make_factory({"ZBOUND": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    # All other gates intentionally pass; only Z hits the boundary.
    # ROIC = (20 * 0.79) / (10 + 75) = 15.8 / 85 = 0.186  >= 0.10 ✅
    # ND/EBITDA = (10 - 8) / 35 = 0.057  <= 3.0 ✅
    # FCF yield = 0.40  >= 0.04 ✅
    # SI = 0.05  <= 0.10 ✅
    # Z = 3.00 exact → strict > fails → drop
    assert result == []


def test_3_altman_z_just_above_threshold_passes():
    """Z = 3.01 → strict >, passes.

    Same setup as test_2 but bump revenue from 74B to 75B:
        E = 0.75  →  1.0 * 0.75 = 0.75
        Z = 0.12 + 0.28 + 0.66 + 1.20 + 0.75 = 3.01
    """
    upstream = _make_universe_tc("ZJUST")
    ft = _make_happy_ticker()
    ft.balance_sheet = _bs({
        "Total Assets": 100_000_000_000,
        "Total Liabilities Net Minority Interest": 25_000_000_000,
        "Working Capital": 10_000_000_000,
        "Retained Earnings": 20_000_000_000,
        "Stockholders Equity": 75_000_000_000,
        "Total Debt": 10_000_000_000,
        "Cash And Cash Equivalents": 8_000_000_000,
    })
    ft.income_stmt = _is({
        "Total Revenue": 75_000_000_000,
        "EBIT": 20_000_000_000,
        "EBITDA": 35_000_000_000,
    })
    factory = _make_factory({"ZJUST": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert len(result) == 1
    assert result[0].altman_z > 3.0
    assert result[0].altman_z < 3.02  # tight band — should be exactly 3.01


def test_4_fcf_yield_exactly_at_threshold_passes():
    """FCF yield = 0.04 → >= passes."""
    upstream = _make_universe_tc("FCFB")
    ft = _make_happy_ticker()
    # FCF = 0.04 * market_cap = 0.04 * 50B = 2B
    # ocf - |capex| = 2B; choose ocf=4B, capex=-2B
    ft.cashflow = _cf({
        "Operating Cash Flow": 4_000_000_000,
        "Capital Expenditure": -2_000_000_000,
    })
    factory = _make_factory({"FCFB": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert len(result) == 1
    assert abs(result[0].fcf_yield - 0.04) < 1e-9


def test_5_nd_ebitda_exactly_at_threshold_passes():
    """ND/EBITDA = 3.0 exactly → <= passes.

    Hold all happy fields except total_debt and cash. ebitda = 35B,
    want net_debt = 3.0 * 35B = 105B → total_debt - cash = 105B.
    Choose total_debt = 110B, cash = 5B.
    """
    upstream = _make_universe_tc("NDEB")
    ft = _make_happy_ticker()
    ft.balance_sheet = _bs({
        "Total Assets": 100_000_000_000,
        "Total Liabilities Net Minority Interest": 25_000_000_000,
        "Working Capital": 15_000_000_000,
        "Retained Earnings": 30_000_000_000,
        "Stockholders Equity": 75_000_000_000,
        "Total Debt": 110_000_000_000,
        "Cash And Cash Equivalents": 5_000_000_000,
    })
    factory = _make_factory({"NDEB": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    # Other gates after the mutation:
    # Altman Z = 3.79 (unchanged — total_assets and TL untouched)
    # ROIC = (30 * 0.79) / (110 + 75) = 23.7 / 185 = 0.128 ≥ 0.10 ✅
    # FCF = 0.40, SI = 0.05 — unchanged
    assert len(result) == 1
    assert abs(result[0].net_debt_to_ebitda - 3.0) < 1e-9


def test_6_roic_exactly_at_threshold_passes():
    """ROIC = 0.10 → >= passes."""
    upstream = _make_universe_tc("ROICB")
    ft = _make_happy_ticker()
    # ROIC = NOPAT / invested_capital
    # Want ROIC = 0.10 with NOPAT = ebit * (1 - 0.21) = ebit * 0.79
    # Choose ebit = 10B → NOPAT = 7.9B → invested_capital = 79B
    # invested_capital = total_debt + stockholders_equity = 79B
    # Keep stockholders_equity = 75B → total_debt = 4B
    ft.balance_sheet = _bs({
        "Total Assets": 100_000_000_000,
        "Total Liabilities Net Minority Interest": 25_000_000_000,
        "Working Capital": 15_000_000_000,
        "Retained Earnings": 30_000_000_000,
        "Stockholders Equity": 75_000_000_000,
        "Total Debt": 4_000_000_000,
        "Cash And Cash Equivalents": 1_000_000_000,
    })
    ft.income_stmt = _is({
        "Total Revenue": 100_000_000_000,
        "EBIT": 10_000_000_000,
        "EBITDA": 15_000_000_000,
    })
    factory = _make_factory({"ROICB": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    # Altman Z drops because EBIT/TA = 0.10 instead of 0.30:
    # Z = 0.18 + 0.42 + 0.33 + 1.20 + 1.00 = 3.13 — still passes
    assert len(result) == 1
    assert abs(result[0].roic - 0.10) < 1e-9


def test_7_short_interest_exactly_at_threshold_passes():
    """SI = 0.10 → <= passes."""
    upstream = _make_universe_tc("SIB")
    ft = _make_happy_ticker()
    ft.info = {"shortPercentOfFloat": 0.10, "effectiveTaxRate": 0.21}
    factory = _make_factory({"SIB": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert len(result) == 1
    assert result[0].short_interest_pct == 0.10


# ---------------------------------------------------------------------------
# 8-12. Filter failures (one per metric)
# ---------------------------------------------------------------------------

def test_8_altman_z_below_threshold_drops():
    upstream = _make_universe_tc("ZFAIL")
    ft = _make_happy_ticker()
    # Crater Z by zeroing the ebit term: EBIT=1B, working_capital=1B
    ft.balance_sheet = _bs({
        "Total Assets": 100_000_000_000,
        "Total Liabilities Net Minority Interest": 90_000_000_000,
        "Working Capital": 1_000_000_000,
        "Retained Earnings": 1_000_000_000,
        "Stockholders Equity": 75_000_000_000,
        "Total Debt": 10_000_000_000,
        "Cash And Cash Equivalents": 8_000_000_000,
    })
    ft.income_stmt = _is({
        "Total Revenue": 50_000_000_000,
        "EBIT": 1_000_000_000,
        "EBITDA": 5_000_000_000,
    })
    factory = _make_factory({"ZFAIL": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []


def test_9_fcf_yield_below_threshold_drops():
    upstream = _make_universe_tc("FCFFAIL")
    ft = _make_happy_ticker()
    # FCF = 1B, market_cap = 50B → 0.02 < 0.04
    ft.cashflow = _cf({
        "Operating Cash Flow": 6_000_000_000,
        "Capital Expenditure": -5_000_000_000,
    })
    factory = _make_factory({"FCFFAIL": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []


def test_10_nd_ebitda_above_threshold_drops():
    upstream = _make_universe_tc("NDFAIL")
    ft = _make_happy_ticker()
    # EBITDA = 10B, total_debt = 50B, cash = 0 → ND/EBITDA = 5.0 > 3.0
    ft.balance_sheet = _bs({
        "Total Assets": 100_000_000_000,
        "Total Liabilities Net Minority Interest": 25_000_000_000,
        "Working Capital": 15_000_000_000,
        "Retained Earnings": 30_000_000_000,
        "Stockholders Equity": 75_000_000_000,
        "Total Debt": 50_000_000_000,
        "Cash And Cash Equivalents": 0,
    })
    ft.income_stmt = _is({
        "Total Revenue": 100_000_000_000,
        "EBIT": 30_000_000_000,
        "EBITDA": 10_000_000_000,
    })
    factory = _make_factory({"NDFAIL": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []


def test_11_roic_below_threshold_drops():
    upstream = _make_universe_tc("ROICFAIL")
    ft = _make_happy_ticker()
    # NOPAT = 1B * 0.79 = 0.79B, invested_capital = 100B → ROIC = 0.0079 << 0.10
    ft.income_stmt = _is({
        "Total Revenue": 100_000_000_000,
        "EBIT": 1_000_000_000,
        "EBITDA": 35_000_000_000,
    })
    ft.balance_sheet = _bs({
        "Total Assets": 100_000_000_000,
        "Total Liabilities Net Minority Interest": 25_000_000_000,
        "Working Capital": 15_000_000_000,
        "Retained Earnings": 30_000_000_000,
        "Stockholders Equity": 100_000_000_000,
        "Total Debt": 10_000_000_000,
        "Cash And Cash Equivalents": 8_000_000_000,
    })
    factory = _make_factory({"ROICFAIL": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []


def test_12_short_interest_above_threshold_drops():
    upstream = _make_universe_tc("SIFAIL")
    ft = _make_happy_ticker()
    ft.info = {"shortPercentOfFloat": 0.15, "effectiveTaxRate": 0.21}
    factory = _make_factory({"SIFAIL": ft})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []


# ---------------------------------------------------------------------------
# 13-20. Data failures
# ---------------------------------------------------------------------------

def test_13_info_fetch_raises_drops(caplog):
    upstream = _make_universe_tc("INFOFAIL")
    ft = _make_happy_ticker()
    ft.raise_on_info = True
    factory = _make_factory({"INFOFAIL": ft})
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.fundamentals"):
        result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []
    assert any("info_fetch_failed" in r.message for r in caplog.records)


def test_14_balance_sheet_empty_drops(caplog):
    upstream = _make_universe_tc("BSEMPTY")
    ft = _make_happy_ticker()
    ft.balance_sheet = pd.DataFrame()  # empty
    factory = _make_factory({"BSEMPTY": ft})
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.fundamentals"):
        result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []
    # Either statements_unavailable or field_missing — both acceptable
    assert any(
        "statements_unavailable" in r.message or "field_missing" in r.message
        for r in caplog.records
    )


def test_15_income_stmt_missing_ebit_drops(caplog):
    upstream = _make_universe_tc("EBITMISS")
    ft = _make_happy_ticker()
    ft.income_stmt = _is({
        "Total Revenue": 100_000_000_000,
        # EBIT intentionally absent
        "EBITDA": 35_000_000_000,
    })
    factory = _make_factory({"EBITMISS": ft})
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.fundamentals"):
        result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []
    assert any("field_missing:EBIT" in r.message for r in caplog.records)


def test_16_cashflow_missing_op_cf_drops(caplog):
    upstream = _make_universe_tc("OCFMISS")
    ft = _make_happy_ticker()
    ft.cashflow = _cf({
        # Operating Cash Flow intentionally absent
        "Capital Expenditure": -5_000_000_000,
    })
    factory = _make_factory({"OCFMISS": ft})
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.fundamentals"):
        result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []
    assert any("field_missing:Operating Cash Flow" in r.message for r in caplog.records)


def test_17_ebitda_zero_drops(caplog):
    upstream = _make_universe_tc("EBITDAZERO")
    ft = _make_happy_ticker()
    ft.income_stmt = _is({
        "Total Revenue": 100_000_000_000,
        "EBIT": 30_000_000_000,
        "EBITDA": 0.0,
    })
    factory = _make_factory({"EBITDAZERO": ft})
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.fundamentals"):
        result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []
    assert any("degenerate_denominator:ebitda" in r.message for r in caplog.records)


def test_18_invested_capital_zero_drops(caplog):
    upstream = _make_universe_tc("ICZERO")
    ft = _make_happy_ticker()
    # invested_capital = total_debt + stockholders_equity; both negative-summing to 0
    ft.balance_sheet = _bs({
        "Total Assets": 100_000_000_000,
        "Total Liabilities Net Minority Interest": 25_000_000_000,
        "Working Capital": 15_000_000_000,
        "Retained Earnings": 30_000_000_000,
        "Stockholders Equity": 0.0,
        "Total Debt": 0.0,
        "Cash And Cash Equivalents": 8_000_000_000,
    })
    factory = _make_factory({"ICZERO": ft})
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.fundamentals"):
        result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []
    assert any("degenerate_denominator:invested_capital" in r.message for r in caplog.records)


def test_19_short_interest_unavailable_drops(caplog):
    upstream = _make_universe_tc("SINOPE")
    ft = _make_happy_ticker()
    ft.info = {"effectiveTaxRate": 0.21}  # neither shortPercentOfFloat nor sharesShort/floatShares
    factory = _make_factory({"SINOPE": ft})
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.fundamentals"):
        result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []
    assert any("short_interest_unavailable" in r.message for r in caplog.records)


def test_20_total_assets_zero_drops(caplog):
    upstream = _make_universe_tc("TAZERO")
    ft = _make_happy_ticker()
    ft.balance_sheet = _bs({
        "Total Assets": 0.0,
        "Total Liabilities Net Minority Interest": 25_000_000_000,
        "Working Capital": 15_000_000_000,
        "Retained Earnings": 30_000_000_000,
        "Stockholders Equity": 75_000_000_000,
        "Total Debt": 10_000_000_000,
        "Cash And Cash Equivalents": 8_000_000_000,
    })
    factory = _make_factory({"TAZERO": ft})
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.fundamentals"):
        result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert result == []
    assert any("degenerate_denominator:total_assets" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 21-26. Structural
# ---------------------------------------------------------------------------

def test_21_empty_input_returns_empty():
    result = fundamentals.run_phase_3([], yf_ticker_factory=lambda s: None, heartbeat_interval=0)
    assert result == []


def test_22_bad_ticker_does_not_abort_batch(caplog):
    """One ticker fails data fetch; the other survives. Batch keeps going."""
    upstream_good = _make_universe_tc("GOOD")
    upstream_bad = _make_universe_tc("BAD")
    factory = _make_factory({
        "GOOD": _make_happy_ticker(),
        "BAD": FakeTicker(raise_on_info=True),
    })
    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.fundamentals"):
        result = fundamentals.run_phase_3(
            [upstream_bad, upstream_good],  # bad first to ensure it doesn't kill the loop
            yf_ticker_factory=factory,
            heartbeat_interval=0,
        )
    assert len(result) == 1
    assert result[0].ticker == "GOOD"
    assert any("BAD" in r.message and "info_fetch_failed" in r.message for r in caplog.records)


def test_23_heartbeat_fires_at_boundary(caplog):
    """With heartbeat_interval=10 and 20 candidates, expect 2 heartbeats."""
    upstreams = [_make_universe_tc(f"T{i:02d}") for i in range(20)]
    factory = _make_factory({f"T{i:02d}": _make_happy_ticker() for i in range(20)})
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.fundamentals"):
        fundamentals.run_phase_3(upstreams, yf_ticker_factory=factory, heartbeat_interval=10)
    heartbeat_msgs = [
        r.message for r in caplog.records if "Phase 3 progress" in r.message
    ]
    assert len(heartbeat_msgs) == 2


def test_24_final_log_line_contains_required_tokens(caplog):
    upstream = _make_universe_tc("LOGCHECK")
    factory = _make_factory({"LOGCHECK": _make_happy_ticker()})
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.fundamentals"):
        fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    final_lines = [r.message for r in caplog.records if "Phase 3 complete" in r.message]
    assert len(final_lines) == 1
    line = final_lines[0]
    for token in ("processed=", "survivors=", "dropped=", "elapsed="):
        assert token in line, f"Missing token {token!r} in: {line}"


def test_25_upstream_fields_preserved_through_carryforward():
    """Every Phase 1 + Phase 2 field on the input must appear identically
    on the FundamentalCandidate output (with the dispatch's name remapping)."""
    upstream = TechnicalCandidate(
        ticker="CARRY",
        name="Carry Industries",
        sector="Technology",
        country="US",
        market_cap_usd=80_000_000_000.0,
        current_price=200.0,
        sma_200=185.0,
        rsi_14=39.5,
        bband_lower=198.0,
        bband_middle=205.0,
        bband_upper=212.0,
        lowest_low_21d=195.5,
    )
    factory = _make_factory({"CARRY": _make_happy_ticker(market_cap_usd=80_000_000_000.0)})
    result = fundamentals.run_phase_3([upstream], yf_ticker_factory=factory, heartbeat_interval=0)
    assert len(result) == 1
    out = result[0]

    # Phase 1 carry-forward
    assert out.ticker == "CARRY"
    assert out.name == "Carry Industries"
    assert out.sector == "Technology"
    assert out.country == "US"
    assert out.market_cap_usd == 80_000_000_000.0

    # Phase 2 carry-forward (with name remap: current_price → spot, bband_middle → bband_mid)
    assert out.spot == 200.0
    assert out.sma_200 == 185.0
    assert out.rsi_14 == 39.5
    assert out.bband_lower == 198.0
    assert out.bband_mid == 205.0  # remapped from bband_middle
    assert out.bband_upper == 212.0
    assert out.lowest_low_21d == 195.5


def test_26_no_finnhub_imports_in_fundamentals():
    """AST guard: fundamentals.py must NOT import FinnhubClient or anything
    from agt_equities.screener.finnhub_client. Phase 3 has a single data
    source (yfinance) and zero fallback to Finnhub."""
    path = Path(__file__).resolve().parent.parent / "agt_equities" / "screener" / "fundamentals.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_modules = {"agt_equities.screener.finnhub_client", "httpx"}
    forbidden_names = {"FinnhubClient", "FinnhubRateLimiter"}

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                violations.append(f"forbidden import: from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_names:
                    violations.append(f"forbidden name import: {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    violations.append(f"forbidden import: import {alias.name}")
        elif isinstance(node, ast.Name) and node.id in forbidden_names:
            violations.append(f"forbidden name reference: {node.id}")

    assert not violations, "fundamentals.py isolation violation: " + ", ".join(violations)
