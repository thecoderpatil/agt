"""
tests/test_screener_ray_filter.py

Unit tests for agt_equities.screener.ray_filter (Phase 6: RAY band
filter).

Mocking strategy: NONE. Phase 6 has zero external dependencies, so
tests construct real StrikeCandidate instances via a helper and
pass them through run_phase_6 directly. No network, no monkeypatch,
no fake objects.

Test matrix (20 tests, matches the C6 dispatch spec):

  Band boundaries (6):
    1.  30.0 exactly → PASSES (inclusive lower)
    2.  30.01 → PASSES
    3.  29.99 → DROPS (below_band)
    4.  130.0 exactly → PASSES (inclusive upper)
    5.  129.99 → PASSES
    6.  130.01 → DROPS (above_band)

  Band interior (3):
    7.  50.0 → PASSES
    8.  100.0 → PASSES
    9.  75.5 → PASSES

  Malformed data (3):
    10. 0.0 → DROPS (malformed)
    11. -5.0 → DROPS (malformed)
    12. NaN → DROPS (malformed, guard critical)

  Structural (3):
    13. Empty input → []
    14. Single in-band → carry-forward spot check
    15. ray_decimal math → yield 45.2 produces ray_decimal 0.452

  Mixed batch (1):
    16. 3 below + 2 in + 2 above + 1 malformed → 2 survivors,
        counter attribution correct

  Order preservation (1):
    17. Input [A, B, C] → Output [A, B, C] — no sort

  Full carry-forward (1):
    18. All 40 StrikeCandidate fields preserved verbatim

  Final log (1):
    19. Final log line contains all required tokens

  Per-candidate exception (1):
    20. A mid-batch exception does not abort remaining candidates
"""
from __future__ import annotations

import logging
import math
from typing import Any

import pytest

from agt_equities.screener import config, ray_filter
from agt_equities.screener.types import (
    RAYCandidate,
    StrikeCandidate,
)


# ---------------------------------------------------------------------------
# Synthetic StrikeCandidate builder
# ---------------------------------------------------------------------------

def make_strike_candidate(
    ticker: str = "TEST",
    annualized_yield: float = 50.0,
    strike: float = 100.0,
    **overrides: Any,
) -> StrikeCandidate:
    """Build a StrikeCandidate with sensible defaults for testing.
    Every upstream field gets a default; only what is specified
    in kwargs is customized.
    """
    defaults: dict[str, Any] = dict(
        ticker=ticker,
        name=f"{ticker} Inc",
        sector="Software",
        country="US",
        market_cap_usd=50_000_000_000.0,
        spot=100.0,
        sma_200=90.0,
        rsi_14=40.0,
        bband_lower=95.0,
        bband_mid=100.0,
        bband_upper=105.0,
        lowest_low_21d=88.0,
        altman_z=5.0,
        fcf_yield=0.06,
        net_debt_to_ebitda=1.5,
        roic=0.15,
        short_interest_pct=0.03,
        max_abs_correlation=0.25,
        most_correlated_holding="XYZ",
        ivr_pct=45.0,
        iv_latest=0.30,
        iv_52w_min=0.20,
        iv_52w_max=0.50,
        iv_bars_used=250,
        next_earnings=None,
        ex_dividend_date=None,
        calendar_source="fake",
        expiry="2026-04-24",
        dte=7,
        strike=strike,
        bid=0.95,
        ask=1.05,
        mid=1.00,
        last=1.00,
        volume=100,
        open_interest=500,
        implied_vol=0.30,
        annualized_yield=annualized_yield,
        otm_pct=3.0,
    )
    defaults.update(overrides)
    return StrikeCandidate(**defaults)


# ---------------------------------------------------------------------------
# 1-6. Band boundaries
# ---------------------------------------------------------------------------

def test_1_yield_30_exactly_passes():
    """Inclusive lower bound: yield == 30.0 PASSES (< is strict)."""
    sc = make_strike_candidate("BOUND_LOW", annualized_yield=30.0)
    result = ray_filter.run_phase_6([sc])
    assert len(result) == 1
    assert result[0].ticker == "BOUND_LOW"
    assert isinstance(result[0], RAYCandidate)


def test_2_yield_30_01_passes():
    sc = make_strike_candidate("JUST_IN", annualized_yield=30.01)
    result = ray_filter.run_phase_6([sc])
    assert len(result) == 1


def test_3_yield_29_99_drops_below_band(caplog):
    sc = make_strike_candidate("LOW", annualized_yield=29.99)
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.ray_filter"):
        result = ray_filter.run_phase_6([sc])
    assert result == []
    assert any(
        "STRIKE_DROPPED_PHASE6_BELOW_BAND" in r.message
        for r in caplog.records
    )


def test_4_yield_130_exactly_passes():
    """Inclusive upper bound: yield == 130.0 PASSES (> is strict)."""
    sc = make_strike_candidate("BOUND_HIGH", annualized_yield=130.0)
    result = ray_filter.run_phase_6([sc])
    assert len(result) == 1
    assert result[0].ticker == "BOUND_HIGH"


def test_5_yield_129_99_passes():
    sc = make_strike_candidate("JUST_UNDER", annualized_yield=129.99)
    result = ray_filter.run_phase_6([sc])
    assert len(result) == 1


def test_6_yield_130_01_drops_above_band(caplog):
    sc = make_strike_candidate("HIGH", annualized_yield=130.01)
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.ray_filter"):
        result = ray_filter.run_phase_6([sc])
    assert result == []
    assert any(
        "STRIKE_DROPPED_PHASE6_ABOVE_BAND" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# 7-9. Band interior
# ---------------------------------------------------------------------------

def test_7_yield_50_passes():
    sc = make_strike_candidate("MID", annualized_yield=50.0)
    result = ray_filter.run_phase_6([sc])
    assert len(result) == 1
    assert result[0].annualized_yield == 50.0


def test_8_yield_100_passes():
    sc = make_strike_candidate("HUNDRED", annualized_yield=100.0)
    result = ray_filter.run_phase_6([sc])
    assert len(result) == 1


def test_9_yield_75_5_passes():
    sc = make_strike_candidate("SEVENFIVE", annualized_yield=75.5)
    result = ray_filter.run_phase_6([sc])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# 10-12. Malformed data
# ---------------------------------------------------------------------------

def test_10_yield_zero_drops_malformed(caplog):
    """yield=0 is a data quality issue, not a legitimate filter miss."""
    sc = make_strike_candidate("ZERO", annualized_yield=0.0)
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.ray_filter"):
        result = ray_filter.run_phase_6([sc])
    assert result == []
    assert any(
        "STRIKE_DROPPED_PHASE6_MALFORMED" in r.message
        for r in caplog.records
    )


def test_11_yield_negative_drops_malformed(caplog):
    sc = make_strike_candidate("NEG", annualized_yield=-5.0)
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.ray_filter"):
        result = ray_filter.run_phase_6([sc])
    assert result == []
    assert any(
        "STRIKE_DROPPED_PHASE6_MALFORMED" in r.message
        for r in caplog.records
    )


def test_12_yield_nan_drops_malformed(caplog):
    """NaN guard: without the explicit math.isnan check, a NaN yield
    would fall through all three comparison branches (NaN comparisons
    all return False) and silently pass as "in-band". Test locks
    the guard semantics."""
    sc = make_strike_candidate("NAN", annualized_yield=float("nan"))
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.ray_filter"):
        result = ray_filter.run_phase_6([sc])
    assert result == []
    assert any(
        "STRIKE_DROPPED_PHASE6_MALFORMED" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# 13-15. Structural
# ---------------------------------------------------------------------------

def test_13_empty_input_returns_empty():
    result = ray_filter.run_phase_6([])
    assert result == []


def test_14_single_in_band_spot_check_carry_forward():
    sc = make_strike_candidate(
        "CARRY",
        annualized_yield=85.0,
        strike=145.0,
        altman_z=8.4,
        ivr_pct=72.5,
    )
    result = ray_filter.run_phase_6([sc])
    assert len(result) == 1
    out = result[0]
    assert out.ticker == "CARRY"
    assert out.altman_z == 8.4
    assert out.ivr_pct == 72.5
    assert out.strike == 145.0
    assert out.mid == 1.00  # default from builder
    assert out.annualized_yield == 85.0


def test_15_ray_decimal_math():
    """Input yield 45.2 → output ray_decimal == 0.452."""
    sc = make_strike_candidate("DECIMAL", annualized_yield=45.2)
    result = ray_filter.run_phase_6([sc])
    assert len(result) == 1
    assert abs(result[0].ray_decimal - 0.452) < 0.0001


# ---------------------------------------------------------------------------
# 16. Mixed batch
# ---------------------------------------------------------------------------

def test_16_mixed_batch_correct_counter_attribution(caplog):
    """3 below + 2 in + 2 above + 1 malformed = 8 inputs → 2 survivors."""
    candidates = [
        make_strike_candidate("LOW1", annualized_yield=10.0),    # below
        make_strike_candidate("IN1", annualized_yield=50.0),      # in
        make_strike_candidate("LOW2", annualized_yield=20.0),    # below
        make_strike_candidate("HIGH1", annualized_yield=150.0),   # above
        make_strike_candidate("IN2", annualized_yield=85.0),      # in
        make_strike_candidate("BAD", annualized_yield=0.0),       # malformed
        make_strike_candidate("HIGH2", annualized_yield=200.0),   # above
        make_strike_candidate("LOW3", annualized_yield=15.0),    # below
    ]
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.ray_filter"):
        result = ray_filter.run_phase_6(candidates)

    assert len(result) == 2
    survivor_tickers = {sc.ticker for sc in result}
    assert survivor_tickers == {"IN1", "IN2"}

    # Inspect the final log line for counter attribution
    final_lines = [r.message for r in caplog.records if "Phase 6 complete" in r.message]
    assert len(final_lines) == 1
    line = final_lines[0]
    assert "strikes_in=8" in line
    assert "survivors=2" in line
    assert "dropped=6" in line
    assert "below_band=3" in line
    assert "above_band=2" in line
    assert "malformed=1" in line


# ---------------------------------------------------------------------------
# 17. Return order preservation
# ---------------------------------------------------------------------------

def test_17_return_order_matches_input_order():
    """Phase 6 does NOT sort. Input [A(50), B(70), C(40)] → output [A, B, C]."""
    a = make_strike_candidate("A", annualized_yield=50.0)
    b = make_strike_candidate("B", annualized_yield=70.0)
    c = make_strike_candidate("C", annualized_yield=40.0)
    result = ray_filter.run_phase_6([a, b, c])
    assert len(result) == 3
    assert [r.ticker for r in result] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# 18. Full carry-forward of all 40 StrikeCandidate fields
# ---------------------------------------------------------------------------

def test_18_all_40_upstream_fields_preserved():
    """Detailed carry-forward assertion — every StrikeCandidate field
    must appear verbatim on the RAYCandidate output."""
    sc = StrikeCandidate(
        ticker="FULL",
        name="Full Carry Inc",
        sector="Technology",
        country="US",
        market_cap_usd=123_456_789.0,
        spot=150.5,
        sma_200=140.0,
        rsi_14=41.3,
        bband_lower=148.0,
        bband_mid=151.0,
        bband_upper=154.0,
        lowest_low_21d=145.0,
        altman_z=6.7,
        fcf_yield=0.062,
        net_debt_to_ebitda=1.1,
        roic=0.19,
        short_interest_pct=0.02,
        max_abs_correlation=0.33,
        most_correlated_holding="MSFT",
        ivr_pct=55.5,
        iv_latest=0.287,
        iv_52w_min=0.19,
        iv_52w_max=0.41,
        iv_bars_used=248,
        next_earnings=None,
        ex_dividend_date=None,
        calendar_source="yfinance_temporary",
        expiry="2026-04-24",
        dte=13,
        strike=145.0,
        bid=1.12,
        ask=1.22,
        mid=1.17,
        last=1.15,
        volume=422,
        open_interest=1834,
        implied_vol=0.295,
        annualized_yield=65.5,
        otm_pct=3.65,
    )
    result = ray_filter.run_phase_6([sc])
    assert len(result) == 1
    out = result[0]

    # Identity
    assert out.ticker == "FULL"
    assert out.name == "Full Carry Inc"
    assert out.sector == "Technology"
    assert out.country == "US"
    assert out.market_cap_usd == 123_456_789.0
    # Phase 2 technicals
    assert out.spot == 150.5
    assert out.sma_200 == 140.0
    assert out.rsi_14 == 41.3
    assert out.bband_lower == 148.0
    assert out.bband_mid == 151.0
    assert out.bband_upper == 154.0
    assert out.lowest_low_21d == 145.0
    # Phase 3 fundamentals
    assert out.altman_z == 6.7
    assert out.fcf_yield == 0.062
    assert out.net_debt_to_ebitda == 1.1
    assert out.roic == 0.19
    assert out.short_interest_pct == 0.02
    # Phase 3.5 correlation
    assert out.max_abs_correlation == 0.33
    assert out.most_correlated_holding == "MSFT"
    # Phase 4 vol/event armor
    assert out.ivr_pct == 55.5
    assert out.iv_latest == 0.287
    assert out.iv_52w_min == 0.19
    assert out.iv_52w_max == 0.41
    assert out.iv_bars_used == 248
    assert out.next_earnings is None
    assert out.ex_dividend_date is None
    assert out.calendar_source == "yfinance_temporary"
    # Phase 5 strike
    assert out.expiry == "2026-04-24"
    assert out.dte == 13
    assert out.strike == 145.0
    assert out.bid == 1.12
    assert out.ask == 1.22
    assert out.mid == 1.17
    assert out.last == 1.15
    assert out.volume == 422
    assert out.open_interest == 1834
    assert out.implied_vol == 0.295
    assert out.annualized_yield == 65.5
    assert out.otm_pct == 3.65
    # Phase 6 addition
    assert abs(out.ray_decimal - 0.655) < 0.0001


# ---------------------------------------------------------------------------
# 19. Final log line
# ---------------------------------------------------------------------------

def test_19_final_log_line_contains_required_tokens(caplog):
    sc = make_strike_candidate("LOGCHECK", annualized_yield=55.0)
    with caplog.at_level(logging.INFO, logger="agt_equities.screener.ray_filter"):
        ray_filter.run_phase_6([sc])
    final_lines = [r.message for r in caplog.records if "Phase 6 complete" in r.message]
    assert len(final_lines) == 1
    line = final_lines[0]
    required_tokens = (
        "strikes_in=",
        "survivors=",
        "dropped=",
        "below_band=",
        "above_band=",
        "malformed=",
        "elapsed=",
    )
    for token in required_tokens:
        assert token in line, f"Missing token {token!r} in final log line: {line}"


# ---------------------------------------------------------------------------
# 20. Per-candidate exception isolation
# ---------------------------------------------------------------------------

class _BrokenCandidate:
    """A stub that raises on annualized_yield attribute access.
    Used to prove the per-candidate try/except guard works when a
    malformed StrikeCandidate (constructed bypassing the normal
    Phase 5 pipeline) slips into Phase 6.
    """

    def __init__(self, ticker: str, expiry: str, strike: float):
        self.ticker = ticker
        self.expiry = expiry
        self.strike = strike

    def __getattribute__(self, name):
        # Raise on annualized_yield access specifically so the try
        # block triggers the except handler. All other attributes
        # resolve normally via object.__getattribute__.
        if name == "annualized_yield":
            raise RuntimeError(f"simulated attribute error for {name}")
        return object.__getattribute__(self, name)


def test_20_per_candidate_exception_does_not_abort_batch(caplog):
    """One candidate raises mid-batch → batch continues, survivor
    from the remaining candidates."""
    valid_a = make_strike_candidate("A", annualized_yield=50.0)
    broken = _BrokenCandidate("BROKEN", "2026-04-24", 100.0)
    valid_c = make_strike_candidate("C", annualized_yield=60.0)

    with caplog.at_level(logging.WARNING, logger="agt_equities.screener.ray_filter"):
        # pass _BrokenCandidate in the middle; run_phase_6 expects
        # StrikeCandidate-duck-typed objects, so this is structurally
        # valid for the test's purpose
        result = ray_filter.run_phase_6([valid_a, broken, valid_c])  # type: ignore[list-item]

    # A and C survive; BROKEN is caught by the except guard
    assert len(result) == 2
    survivor_tickers = {sc.ticker for sc in result}
    assert survivor_tickers == {"A", "C"}
    # Warning log fired for BROKEN
    assert any(
        "STRIKE_DROPPED_PHASE6_ERROR" in r.message and "BROKEN" in r.message
        for r in caplog.records
    )
