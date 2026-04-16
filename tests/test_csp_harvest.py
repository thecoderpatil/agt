"""
tests/test_csp_harvest.py

Unit tests for agt_equities.csp_harvest -- the CSP profit-take
harvester dispatched in M2 (2026-04-11).

Updated 2026-04-15: _should_harvest_csp now uses days_held axis
(canonical spec: "80% in 1 trading day, 90% from day 2 onward").
DTE is only used for the E7 expiry-day let-ride gate.

Structure:
  1-8. _should_harvest_csp threshold predicate tests (days_held axis)
  9. Hypothesis property tests
  10-13. scan_csp_harvest_candidates async scanner tests with FakeIB

ISOLATION: this test file imports only stdlib + pytest + the
csp_harvest public API. It does NOT touch telegram_bot or any
screener module.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agt_equities.csp_harvest import (
    CSP_HARVEST_THRESHOLD_LAST_DAY,
    CSP_HARVEST_THRESHOLD_NEXT_DAY,
    _should_harvest_csp,
    scan_csp_harvest_candidates,
)


# ---------------------------------------------------------------------------
# 1-8. _should_harvest_csp threshold tests (days_held axis)
# ---------------------------------------------------------------------------

def test_day1_80_pct_harvests():
    """days_held=1, profit >= 80% -> harvest (day-1 rule)."""
    ok, reason = _should_harvest_csp(1.00, 0.20, dte=14, days_held=1)
    assert ok is True
    assert "day1_80" in reason


def test_day1_79_pct_does_not_harvest():
    """days_held=1, profit 79% < 80% -> no harvest."""
    ok, reason = _should_harvest_csp(1.00, 0.21, dte=14, days_held=1)
    assert ok is False
    assert "below_threshold" in reason


def test_day0_same_day_80_pct_harvests():
    """days_held=0 (same-day), profit >= 80% -> harvest (day-1 rule covers <=1)."""
    ok, reason = _should_harvest_csp(1.00, 0.15, dte=14, days_held=0)
    assert ok is True
    assert "day1_80" in reason


def test_day2_90_pct_harvests():
    """days_held=2, profit >= 90% -> harvest (standard rule)."""
    ok, reason = _should_harvest_csp(1.00, 0.10, dte=10, days_held=2)
    assert ok is True
    assert "standard_90" in reason


def test_day2_85_pct_does_not_harvest():
    """days_held=2, profit 85% < 90% -> no harvest."""
    ok, reason = _should_harvest_csp(1.00, 0.15, dte=10, days_held=2)
    assert ok is False
    assert "below_threshold" in reason


def test_day5_80_pct_does_not_harvest():
    """days_held=5, profit 80% -> no harvest (needs 90% after day 1)."""
    ok, reason = _should_harvest_csp(1.00, 0.20, dte=7, days_held=5)
    assert ok is False
    assert "below_threshold" in reason


def test_expiry_day_let_ride():
    """dte <= 0 -> never harvest regardless of profit or days_held (E7)."""
    ok, reason = _should_harvest_csp(1.00, 0.05, dte=0, days_held=1)
    assert ok is False
    assert "expiry_day_let_ride" in reason

    ok2, reason2 = _should_harvest_csp(1.00, 0.01, dte=-1, days_held=10)
    assert ok2 is False
    assert "expiry_day_let_ride" in reason2


def test_unknown_days_held_defaults_to_conservative():
    """days_held=-1 (unknown) -> effective=2, so needs 90%."""
    # 85% profit, unknown days_held -> should NOT harvest (needs 90%)
    ok, reason = _should_harvest_csp(1.00, 0.15, dte=10, days_held=-1)
    assert ok is False
    assert "below_threshold" in reason

    # 91% profit, unknown days_held -> SHOULD harvest at 90% threshold
    ok2, reason2 = _should_harvest_csp(1.00, 0.09, dte=10, days_held=-1)
    assert ok2 is True
    assert "standard_90" in reason2


def test_legacy_no_days_held_param_defaults_conservative():
    """Calling without days_held (legacy compat) -> defaults to -1 -> 90% threshold."""
    # 85% profit, no days_held kwarg -> should NOT harvest
    ok, reason = _should_harvest_csp(1.00, 0.15, dte=10)
    assert ok is False

    # 95% profit, no days_held kwarg -> should harvest at 90%
    ok2, reason2 = _should_harvest_csp(1.00, 0.05, dte=10)
    assert ok2 is True


def test_should_harvest_zero_credit_rejects():
    """Zero / negative initial_credit is rejected outright -- no div by 0."""
    ok, reason = _should_harvest_csp(0.0, 0.10, dte=5, days_held=1)
    assert ok is False
    assert reason == "zero_credit"
    ok2, reason2 = _should_harvest_csp(-0.50, 0.10, dte=5, days_held=1)
    assert ok2 is False
    assert reason2 == "zero_credit"


def test_should_harvest_nan_ask_rejects():
    """NaN ask is rejected (guards the IBKR OPRA-missing path
    documented in C6.2 ib_chains)."""
    ok, reason = _should_harvest_csp(1.00, float("nan"), dte=3, days_held=1)
    assert ok is False
    assert reason == "nan_or_inf_input"
    ok2, reason2 = _should_harvest_csp(1.00, None, dte=3, days_held=1)
    assert ok2 is False
    assert reason2 == "missing_input"


# Sanity: thresholds match the dispatch spec
def test_threshold_constants_match_dispatch_spec():
    assert CSP_HARVEST_THRESHOLD_NEXT_DAY == 0.80
    assert CSP_HARVEST_THRESHOLD_LAST_DAY == 0.90


# ---------------------------------------------------------------------------
# 9. Hypothesis property tests for _should_harvest_csp
# ---------------------------------------------------------------------------

try:
    from hypothesis import given, settings, assume
    from hypothesis import strategies as st
    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False

if HAS_HYPOTHESIS:
    class TestCSPHarvestProperties:
        """Hypothesis-driven property tests for CSP harvest."""

        @given(
            credit=st.floats(min_value=0.01, max_value=100.0),
            ask=st.floats(min_value=0.0, max_value=100.0),
            dte=st.integers(min_value=-5, max_value=60),
            days_held=st.integers(min_value=-1, max_value=90),
        )
        @settings(max_examples=300, deadline=2000)
        def test_expiry_day_never_harvests(self, credit, ask, dte, days_held):
            """E7: dte <= 0 -> never harvest, regardless of profit."""
            assume(dte <= 0)
            ok, reason = _should_harvest_csp(credit, ask, dte, days_held)
            assert ok is False

        @given(
            credit=st.floats(min_value=0.01, max_value=100.0),
            ask=st.floats(min_value=0.0, max_value=100.0),
            dte=st.integers(min_value=1, max_value=60),
            days_held=st.integers(min_value=0, max_value=1),
        )
        @settings(max_examples=300, deadline=2000)
        def test_day1_harvest_iff_80pct(self, credit, ask, dte, days_held):
            """Day-1 positions: harvest iff profit >= 80%."""
            profit_pct = (credit - ask) / credit
            ok, _ = _should_harvest_csp(credit, ask, dte, days_held)
            if profit_pct >= 0.80:
                assert ok is True

        @given(
            credit=st.floats(min_value=0.01, max_value=100.0),
            ask_frac=st.floats(min_value=0.0, max_value=0.199),
            dte=st.integers(min_value=1, max_value=60),
            days_held=st.integers(min_value=2, max_value=90),
        )
        @settings(max_examples=300, deadline=2000)
        def test_day2_plus_harvest_iff_90pct(self, credit, ask_frac, dte, days_held):
            """Day-2+ positions: harvest iff profit >= 90%."""
            ask = credit * ask_frac  # ask is 0-19.9% of credit -> profit 80.1-100%
            profit_pct = (credit - ask) / credit
            ok, _ = _should_harvest_csp(credit, ask, dte, days_held)
            if profit_pct >= 0.90:
                assert ok is True
            if profit_pct < 0.80:
                assert ok is False


# ---------------------------------------------------------------------------
# FakeIB machinery for scanner tests
# ---------------------------------------------------------------------------

def _make_fake_put_position(
    *, ticker: str, strike: float, expiry: str, qty: int,
    avg_cost: float, account: str = "U21971297",
):
    """Build a short-put position duck-typed against ib_async."""
    contract = SimpleNamespace(
        symbol=ticker,
        secType="OPT",
        right="P",
        strike=strike,
        lastTradeDateOrContractMonth=expiry,
    )
    return SimpleNamespace(
        contract=contract,
        position=-qty,           # short
        avgCost=avg_cost,        # per-contract IBKR convention (x 100 = credit)
        account=account,
    )


class FakeIB:
    """Minimal ib_async-shaped fake for scan_csp_harvest_candidates."""

    def __init__(self, positions, market_data_by_symbol, *, raise_on_positions=False):
        self._positions = positions
        self._md = market_dat