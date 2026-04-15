"""
Tests for canonical harvest rule (CC + CSP):
  - Day 1 (held 1 trading day): ≥80% profit → harvest
  - Day 2+ through day before expiry: ≥90% profit → harvest
  - Expiry day (DTE=0): let it ride (never harvest)

These tests define the CORRECT behavior per Yash verbatim 2026-04-15.
They will initially FAIL against the current implementation — that's the
point. Each fix in the hardening sprint turns a red test green.

Marker: @pytest.mark.hardening — deselected from CI until explicitly opted in.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest


# ---------------------------------------------------------------------------
# Canonical reference implementation (the "rulebook oracle")
# ---------------------------------------------------------------------------

def canonical_should_harvest(
    initial_credit: float,
    current_ask: float,
    days_held: int,
    dte: int,
) -> tuple[bool, str]:
    """Pure reference implementation of Yash's 80/90 harvest rule.

    Parameters
    ----------
    initial_credit : Premium received when position was opened (per contract).
    current_ask : Current ask to buy-to-close.
    days_held : Number of trading days the position has been open (1 = opened today).
    dte : Days to expiration remaining.

    Returns (should_harvest, reason).
    """
    if initial_credit is None or current_ask is None:
        return False, "missing_input"
    try:
        ic = float(initial_credit)
        ca = float(current_ask)
    except (TypeError, ValueError):
        return False, "uncoercible_input"
    if math.isnan(ic) or math.isnan(ca) or math.isinf(ic) or math.isinf(ca):
        return False, "nan_or_inf"
    if ic <= 0:
        return False, "zero_credit"
    if ca < 0:
        return False, "negative_ask"

    profit_pct = (ic - ca) / ic

    # Rule 0: Expiry day — let it ride
    if dte <= 0:
        return False, f"expiry_day_let_ride:profit_pct={profit_pct:.3f}"

    # Rule 1: Day-1 (opened today), 80% threshold
    if days_held <= 1 and profit_pct >= 0.80:
        return True, f"day1_80:profit_pct={profit_pct:.3f}"

    # Rule 2: Day 2+ through day before expiry, 90% threshold
    if days_held >= 2 and dte >= 1 and profit_pct >= 0.90:
        return True, f"standard_90:profit_pct={profit_pct:.3f}"

    return False, f"below_threshold:profit_pct={profit_pct:.3f},days_held={days_held},dte={dte}"


# ---------------------------------------------------------------------------
# Tests for the canonical reference implementation itself
# ---------------------------------------------------------------------------

@pytest.mark.hardening
class TestCanonicalOracle:
    """Verify the oracle behaves per Yash's verbatim rule."""

    def test_day1_80pct_harvests(self):
        ok, reason = canonical_should_harvest(1.00, 0.20, days_held=1, dte=30)
        assert ok, reason
        assert "day1_80" in reason

    def test_day1_79pct_does_not_harvest(self):
        ok, _ = canonical_should_harvest(1.00, 0.21, days_held=1, dte=30)
        assert not ok

    def test_day1_exact_80pct(self):
        ok, _ = canonical_should_harvest(1.00, 0.20, days_held=1, dte=30)
        assert ok

    def test_day2_90pct_harvests(self):
        ok, reason = canonical_should_harvest(1.00, 0.10, days_held=2, dte=15)
        assert ok
        assert "standard_90" in reason

    def test_day2_89pct_does_not_harvest(self):
        ok, _ = canonical_should_harvest(1.00, 0.11, days_held=2, dte=15)
        assert not ok

    def test_day10_85pct_does_not_harvest(self):
        """85% profit on day 10 is between thresholds — should NOT harvest."""
        ok, _ = canonical_should_harvest(1.00, 0.15, days_held=10, dte=5)
        assert not ok

    def test_expiry_day_never_harvests_at_90(self):
        """Even 90% profit on expiry day → let it ride."""
        ok, reason = canonical_should_harvest(1.00, 0.10, days_held=30, dte=0)
        assert not ok
        assert "expiry_day" in reason

    def test_expiry_day_never_harvests_at_99(self):
        ok, reason = canonical_should_harvest(1.00, 0.01, days_held=30, dte=0)
        assert not ok
        assert "expiry_day" in reason

    def test_day_before_expiry_90pct_harvests(self):
        """DTE=1 (day before last trading day) at 90% → harvest."""
        ok, reason = canonical_should_harvest(1.00, 0.10, days_held=29, dte=1)
        assert ok
        assert "standard_90" in reason

    def test_zero_credit_rejects(self):
        ok, reason = canonical_should_harvest(0.0, 0.05, days_held=1, dte=10)
        assert not ok
        assert "zero_credit" in reason

    def test_nan_rejects(self):
        ok, reason = canonical_should_harvest(float("nan"), 0.05, days_held=1, dte=10)
        assert not ok

    def test_negative_ask_rejects(self):
        ok, _ = canonical_should_harvest(1.0, -0.01, days_held=1, dte=10)
        assert not ok


# ---------------------------------------------------------------------------
# Tests for current CSP harvest implementation vs canonical
# ---------------------------------------------------------------------------

@pytest.mark.hardening
class TestCSPHarvestVsCanonical:
    """These tests assert CANONICAL behavior.

    They will XFAIL against the current csp_harvest._should_harvest_csp
    implementation until fixes land. Each fix removes an xfail.
    """

    @pytest.mark.xfail(
        reason="E6: CSP 80% fires on DTE not days-held — old position at 80% incorrectly harvests",
        strict=True,
    )
    def test_old_position_80pct_should_not_harvest(self):
        """A 20-day-old position with 10 DTE at 80% profit.

        Canonical: days_held=20 → need 90%, only at 80% → NO harvest.
        Current code: dte=10 >= 1, profit=80% >= 0.80 → harvests (WRONG).
        """
        from agt_equities.csp_harvest import _should_harvest_csp
        # initial_credit=1.00, current_ask=0.20 → 80% profit, dte=10
        should, _ = _should_harvest_csp(1.00, 0.20, dte=10)
        # Canonical says NO — not day-1, and 80% < 90% threshold for day 2+
        assert not should

    def test_expiry_day_should_not_harvest(self):
        """DTE=0, 95% profit. Canonical: let it ride. (E7 FIXED 2026-04-15)"""
        from agt_equities.csp_harvest import _should_harvest_csp
        should, reason = _should_harvest_csp(1.00, 0.05, dte=0)
        assert not should
        assert "expiry_day" in reason

    def test_day1_position_80pct_should_harvest(self):
        """A day-1 position at 80% profit should harvest.

        NOTE: Current code gets this right BY ACCIDENT (dte >= 1 catches it),
        but for the wrong reason. Still, the outcome is correct so no xfail.
        """
        from agt_equities.csp_harvest import _should_harvest_csp
        # Day-1 position with 30 DTE at 80% profit
        should, _ = _should_harvest_csp(1.00, 0.20, dte=30)
        assert should

    def test_day2_position_90pct_should_harvest(self):
        """Standard 90% harvest on a multi-day position."""
        from agt_equities.csp_harvest import _should_harvest_csp
        should, _ = _should_harvest_csp(1.00, 0.10, dte=15)
        assert should

    def test_day_before_expiry_90pct_harvests(self):
        """DTE=1 at 90% → harvest. Current code gets this right."""
        from agt_equities.csp_harvest import _should_harvest_csp
        should, _ = _should_harvest_csp(1.00, 0.10, dte=1)
        assert should


# ---------------------------------------------------------------------------
# Tests for CC harvest (roll_engine) vs canonical
# ---------------------------------------------------------------------------

@pytest.mark.hardening
class TestCCHarvestVsCanonical:
    """Roll engine evaluate() harvest behavior vs canonical 80/90 rule.

    E2-E5 fixes landed — all tests now pass without xfail.
    """

    def test_offense_day1_80pct_should_harvest(self):
        """Day-1 position in offense at 80% profit → canonical says harvest."""
        from datetime import date
        from agt_equities.roll_engine import (
            Position, MarketSnapshot, PortfolioContext, ConstraintMatrix,
            OptionQuote, evaluate, HarvestResult,
        )
        today = date.today()
        exp = today + timedelta(days=30)
        pos = Position(
            ticker="TEST",
            account_id="U00000001",
            household="Test_HH",
            strike=100.0,
            expiry=exp,
            quantity=1,
            cost_basis=90.0,
            inception_delta=-0.30,
            opened_at=today,  # day 1
            avg_premium_collected=1.00,
            assigned_basis=90.0,
            adjusted_basis=88.0,
            initial_credit=1.00,
            initial_dte=30,
        )
        call_quote = OptionQuote(
            strike=100.0, expiry=exp,
            bid=0.18, ask=0.20,  # (1.00 - 0.20)/1.00 = 80% profit
            delta=-0.15, iv=0.25,
        )
        market = MarketSnapshot(
            ticker="TEST", spot=95.0, iv30=0.25, chain=(),
            current_call=call_quote, asof=today,
        )
        ctx = PortfolioContext(mode="PEACETIME", leverage=1.0)
        result = evaluate(pos, market, ctx, ConstraintMatrix())
        assert isinstance(result, HarvestResult), f"Expected HarvestResult, got {result}"

    def test_offense_expiry_day_should_hold(self):
        """DTE=0 at 95% profit in offense → canonical says let it ride."""
        from datetime import date
        from agt_equities.roll_engine import (
            Position, MarketSnapshot, PortfolioContext, ConstraintMatrix,
            OptionQuote, evaluate, HarvestResult,
        )
        today = date.today()
        pos = Position(
            ticker="TEST",
            account_id="U00000001",
            household="Test_HH",
            strike=100.0,
            expiry=today,  # expiry day
            quantity=1,
            cost_basis=90.0,
            inception_delta=-0.30,
            opened_at=today - timedelta(days=30),
            avg_premium_collected=1.00,
            assigned_basis=90.0,
            adjusted_basis=88.0,
            initial_credit=1.00,
            initial_dte=30,
        )
        call_quote = OptionQuote(
            strike=100.0, expiry=today,
            bid=0.03, ask=0.05,  # (1.00 - 0.05)/1.00 = 95% profit
            delta=-0.05, iv=0.20,
        )
        market = MarketSnapshot(
            ticker="TEST", spot=95.0, iv30=0.20, chain=(),
            current_call=call_quote, asof=today,
        )
        ctx = PortfolioContext(mode="PEACETIME", leverage=1.0)
        result = evaluate(pos, market, ctx, ConstraintMatrix())
        assert not isinstance(result, HarvestResult), (
            f"Expected HOLD on expiry day, got {result}"
        )

    def test_defense_slow_90pct_should_harvest(self):
        """Defense regime, 20-day position, slow grind to 91% profit.

        V_r = (0.91) / (20/30) = 1.37 < 1.5 → velocity gate rejects.
        Canonical: 91% >= 90% on day 20 → harvest.
        """
        from datetime import date
        from agt_equities.roll_engine import (
            Position, MarketSnapshot, PortfolioContext, ConstraintMatrix,
            OptionQuote, evaluate, HarvestResult,
        )
        today = date.today()
        exp = today + timedelta(days=10)
        pos = Position(
            ticker="TEST",
            account_id="U00000001",
            household="Test_HH",
            strike=100.0,
            expiry=exp,
            quantity=1,
            cost_basis=105.0,
            inception_delta=-0.40,
            opened_at=today - timedelta(days=20),  # 20 days held
            avg_premium_collected=1.00,
            assigned_basis=105.0,
            adjusted_basis=103.0,  # spot < adjusted_basis → defense
            initial_credit=1.00,
            initial_dte=30,
        )
        call_quote = OptionQuote(
            strike=100.0, expiry=exp,
            bid=0.07, ask=0.09,  # (1.00 - 0.09)/1.00 = 91% profit
            delta=-0.10, iv=0.20,
        )
        market = MarketSnapshot(
            ticker="TEST", spot=98.0, iv30=0.20, chain=(),
            current_call=call_quote, asof=today,
        )
        ctx = PortfolioContext(mode="PEACETIME", leverage=1.0)
        result = evaluate(pos, market, ctx, ConstraintMatrix())
        assert isinstance(result, HarvestResult), f"Expected HarvestResult, got {result}"
