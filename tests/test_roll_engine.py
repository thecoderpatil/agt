"""WHEEL-6 roll-engine hardening tests.

Covers empirical-rules edge cases on top of the core decision tree tests
in tests/wheel/test_roll_engine.py. Focus: trigger boundaries, roll
candidate selection, backward compat, paper-basis resolution.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

# CI gate: Sprint A marker + explicit file list in .gitlab-ci.yml.
pytestmark = pytest.mark.sprint_a

from agt_equities.roll_engine import (
    AssignResult,
    AlertResult,
    ConstraintMatrix,
    HarvestResult,
    HoldResult,
    LiquidateResult,
    MarketSnapshot,
    OptionQuote,
    PortfolioContext,
    Position,
    RollResult,
    _annualized_roll_yield,
    _find_roll_target,
    _select_roll_candidate,
    evaluate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TODAY = date(2026, 4, 15)
CTX = PortfolioContext(household="Test_Household", mode="WARTIME", leverage=1.50)


def _pos(**overrides) -> Position:
    base = dict(
        ticker="XYZ",
        account_id="U12345",
        household="Test_Household",
        strike=50.0,
        expiry=TODAY + timedelta(days=2),   # 2 DTE (short-DTE)
        quantity=1,
        cost_basis=55.0,
        inception_delta=0.25,
        opened_at=TODAY - timedelta(days=28),
        avg_premium_collected=1.20,
        assigned_basis=55.0,                # paper basis
        adjusted_basis=52.0,                # adjusted (not used for roll target)
        initial_credit=1.20,
        initial_dte=30,
        cumulative_roll_debit=0.0,
        roll_count=0,
    )
    base.update(overrides)
    return Position(**base)


def _quote(strike: float, expiry: date, bid: float = 0.50, ask: float = 0.55, delta: float = 0.30) -> OptionQuote:
    return OptionQuote(strike=strike, expiry=expiry, bid=bid, ask=ask, delta=delta, iv=0.35)


def _market(pos: Position, spot: float, current_ask: float, chain: tuple[OptionQuote, ...], **extras) -> MarketSnapshot:
    current = OptionQuote(
        strike=pos.strike, expiry=pos.expiry, bid=current_ask - 0.05,
        ask=current_ask, delta=0.60, iv=0.35,
    )
    return MarketSnapshot(
        ticker=pos.ticker, spot=spot, iv30=0.35,
        chain=chain, current_call=current, asof=TODAY,
        **extras,
    )


# ---------------------------------------------------------------------------
# Roll trigger boundary tests
# ---------------------------------------------------------------------------

def test_trigger_dte_boundary_at_3():
    """DTE=3 ITM → triggers roll (≤ 3)."""
    pos = _pos(expiry=TODAY + timedelta(days=3), initial_credit=0.0)
    chain = (_quote(51.0, TODAY + timedelta(days=10), bid=0.50, ask=0.55),)
    m = _market(pos, spot=52.0, current_ask=0.28, chain=chain)
    result = evaluate(pos, m, CTX)
    assert isinstance(result, RollResult), f"got {type(result).__name__}: {result}"


def test_trigger_dte_4_itm_not_urgent():
    """DTE=4 ITM with healthy extrinsic → NOT urgent, hold."""
    pos = _pos(
        expiry=TODAY + timedelta(days=4),
        initial_credit=0.0,
    )
    m = _market(
        pos, spot=51.0,
        current_ask=2.00,  # intrinsic=1, extrinsic=1.00 >> 0.10
        chain=(),
    )
    result = evaluate(pos, m, CTX)
    assert isinstance(result, HoldResult)
    assert "NOT_URGENT" in result.reason


def test_trigger_extrinsic_at_009():
    """DTE=15 ITM but extrinsic=$0.09 → triggers roll (< 0.10)."""
    pos = _pos(
        expiry=TODAY + timedelta(days=15),
        initial_credit=0.0,
    )
    # Chain candidate within fallback DTE window, expiry > current
    chain = (_quote(51.0, TODAY + timedelta(days=20), bid=2.50, ask=2.55),)
    # spot=52, strike=50, ask=2.09 → intrinsic=2, extrinsic=0.09
    m = _market(pos, spot=52.0, current_ask=2.09, chain=chain)
    result = evaluate(pos, m, CTX)
    assert isinstance(result, RollResult), f"got {type(result).__name__}: {result}"


def test_trigger_extrinsic_above_threshold_holds():
    """DTE=15 ITM but extrinsic=$0.50 → not urgent, hold."""
    pos = _pos(
        expiry=TODAY + timedelta(days=15),
        initial_credit=0.0,
    )
    m = _market(pos, spot=52.0, current_ask=2.50, chain=())  # ext=0.50
    result = evaluate(pos, m, CTX)
    assert isinstance(result, HoldResult)


# ---------------------------------------------------------------------------
# Paper basis vs adjusted basis
# ---------------------------------------------------------------------------

def test_paper_basis_not_adjusted_basis_gates_roll():
    """Roll decision uses paper basis (assigned_basis), not adjusted_basis.

    assigned_basis=55 (paper), adjusted_basis=52 (after premiums).
    strike=53, spot=54 → ITM.
    strike=53 < paper_basis=55 → below basis, should roll.
    strike=53 > adjusted_basis=52 → would be 'above basis' if using wrong field.
    """
    pos = _pos(
        strike=53.0, assigned_basis=55.0, adjusted_basis=52.0,
        expiry=TODAY + timedelta(days=2),
        initial_credit=0.0,
    )
    chain = (_quote(54.0, TODAY + timedelta(days=10), bid=0.60, ask=0.65),)
    m = _market(pos, spot=54.0, current_ask=1.10, chain=chain)
    result = evaluate(pos, m, CTX)
    # Must ROLL (below paper basis), NOT ASSIGN (which would happen with adjusted_basis)
    assert isinstance(result, RollResult), (
        f"Expected RollResult (below paper basis), got {type(result).__name__}: {result}"
    )


def test_above_paper_basis_assigns():
    """Strike ≥ paper basis → let assign even if below adjusted_basis.

    Set ask high enough that net_proceeds (spot - ask) ≤ basis,
    avoiding the LiquidateResult opportunity-cost gate.
    """
    pos = _pos(
        strike=56.0, assigned_basis=55.0, adjusted_basis=52.0,
        expiry=TODAY + timedelta(days=2),
        initial_credit=0.0,
    )
    # spot=56.5, ask=2.00 → net_proceeds=54.5 ≤ basis=55 → no liquidate
    m = _market(pos, spot=56.5, current_ask=2.00, chain=())
    result = evaluate(pos, m, CTX)
    assert isinstance(result, AssignResult)
    assert "ABOVE_PAPER_BASIS" in result.reason


# ---------------------------------------------------------------------------
# _find_roll_target unit tests
# ---------------------------------------------------------------------------

def test_find_roll_target_picks_next_strike_up():
    chain = (
        _quote(51.0, TODAY + timedelta(days=10), bid=1.00),
        _quote(52.0, TODAY + timedelta(days=10), bid=0.80),
        _quote(53.0, TODAY + timedelta(days=10), bid=0.60),
    )
    cand = _find_roll_target(
        chain=chain, current_strike=50.0,
        current_expiry=TODAY + timedelta(days=2),
        asof=TODAY, dte_min=5, dte_max=14,
    )
    assert cand is not None
    assert cand.strike == 51.0  # one strike up, not two


def test_find_roll_target_rejects_same_or_earlier_expiry():
    chain = (
        _quote(51.0, TODAY + timedelta(days=2), bid=1.00),   # same expiry
        _quote(51.0, TODAY + timedelta(days=1), bid=1.20),   # earlier expiry
    )
    cand = _find_roll_target(
        chain=chain, current_strike=50.0,
        current_expiry=TODAY + timedelta(days=2),
        asof=TODAY, dte_min=1, dte_max=14,
    )
    assert cand is None


def test_find_roll_target_picks_closest_to_7dte():
    chain = (
        _quote(51.0, TODAY + timedelta(days=5), bid=0.50),   # 5 DTE
        _quote(51.0, TODAY + timedelta(days=8), bid=0.70),   # 8 DTE (closest to 7)
        _quote(51.0, TODAY + timedelta(days=14), bid=1.00),  # 14 DTE
    )
    cand = _find_roll_target(
        chain=chain, current_strike=50.0,
        current_expiry=TODAY + timedelta(days=2),
        asof=TODAY, dte_min=5, dte_max=14,
    )
    assert cand is not None
    assert cand.expiry == TODAY + timedelta(days=8)


def test_find_roll_target_no_strikes_above():
    chain = (
        _quote(49.0, TODAY + timedelta(days=10), bid=2.00),
        _quote(50.0, TODAY + timedelta(days=10), bid=1.50),
    )
    cand = _find_roll_target(
        chain=chain, current_strike=50.0,
        current_expiry=TODAY + timedelta(days=2),
        asof=TODAY, dte_min=5, dte_max=14,
    )
    assert cand is None


# ---------------------------------------------------------------------------
# _select_roll_candidate backward compat
# ---------------------------------------------------------------------------

def test_legacy_select_roll_candidate_filters_same_expiry():
    """Backward-compat wrapper rejects same/earlier expiry."""
    current_exp = TODAY + timedelta(days=10)
    later_exp = TODAY + timedelta(days=14)
    chain = (
        _quote(51.0, current_exp, bid=1.00),    # same expiry — skip
        _quote(51.0, later_exp, bid=0.50),       # forward roll
    )
    cand = _select_roll_candidate(
        chain=chain, current_strike=50.0, current_expiry=current_exp,
        current_call_ask=0.25, asof=TODAY,
        strike_step=1, dte_min=7, dte_max=21, min_credit=0.01,
    )
    assert cand is not None
    assert cand.expiry == later_exp


# ---------------------------------------------------------------------------
# Harvest at expiry boundary
# ---------------------------------------------------------------------------

def test_harvest_skipped_at_expiry_even_if_profitable():
    """DTE=0 with 95% profit → no harvest, let ride."""
    pos = _pos(
        initial_credit=1.20, initial_dte=30,
        opened_at=TODAY - timedelta(days=30),
        expiry=TODAY,  # DTE = 0
    )
    m = _market(pos, spot=48.0, current_ask=0.06, chain=())
    result = evaluate(pos, m, CTX)
    assert isinstance(result, HoldResult)
    assert "EXPIRY_LET_RIDE" in result.reason


# ---------------------------------------------------------------------------
# Constraint defaults
# ---------------------------------------------------------------------------

def test_constraints_defaults_wheel6():
    cm = ConstraintMatrix()
    assert cm.roll_trigger_dte == 3
    assert cm.roll_trigger_extrinsic == 0.10
    assert cm.max_rolls == 10
    assert cm.harvest_day1_pct == 0.80
    assert cm.harvest_standard_pct == 0.90
    assert cm.min_annualized_roll_yield == 0.10
    # Legacy fields still present
    assert cm.min_credit_per_contract == 0.01
    assert cm.offense_roll_min_credit == 0.20


# ---------------------------------------------------------------------------
# Roll count tracking
# ---------------------------------------------------------------------------

def test_roll_count_in_reason():
    """RollResult reason includes the roll count for operator visibility."""
    pos = _pos(
        strike=50.0, assigned_basis=55.0, initial_credit=0.0,
        expiry=TODAY + timedelta(days=2), roll_count=3,
    )
    chain = (_quote(51.0, TODAY + timedelta(days=10), bid=0.50, ask=0.55),)
    m = _market(pos, spot=52.0, current_ask=0.28, chain=chain)
    result = evaluate(pos, m, CTX)
    assert isinstance(result, RollResult)
    assert "rolls=4" in result.reason


# ---------------------------------------------------------------------------
# 1a: Dividend assignment risk gate
# ---------------------------------------------------------------------------

class TestDividendAssignmentRisk:
    """Verify CRITICAL alert when ITM CC is near ex-div with low extrinsic."""

    def test_itm_extrinsic_below_dividend_exdiv_tomorrow(self):
        """ITM CC, ex-div tomorrow, extrinsic $0.05 < dividend $0.50 → CRITICAL."""
        pos = _pos(
            strike=50.0, assigned_basis=55.0, initial_credit=0.0,
            expiry=TODAY + timedelta(days=5),
        )
        chain = (_quote(51.0, TODAY + timedelta(days=10), bid=0.50, ask=0.55),)
        m = _market(
            pos, spot=52.0, current_ask=2.05,  # intrinsic=2, extrinsic=0.05
            chain=chain,
            next_ex_div_date=TODAY + timedelta(days=1),
            next_div_amount=0.50,
        )
        result = evaluate(pos, m, CTX)
        assert isinstance(result, AlertResult), f"got {type(result).__name__}: {result}"
        assert result.severity == "CRITICAL"
        assert "DIVIDEND_ASSIGNMENT_RISK" in result.reason
        assert result.context["next_div_amount"] == 0.50

    def test_itm_extrinsic_above_dividend_no_alert(self):
        """ITM CC, ex-div tomorrow, but extrinsic $1.00 > dividend $0.50 → no alert."""
        pos = _pos(
            strike=50.0, assigned_basis=55.0, initial_credit=0.0,
            expiry=TODAY + timedelta(days=5),
        )
        m = _market(
            pos, spot=52.0, current_ask=3.00,  # intrinsic=2, extrinsic=1.00
            chain=(),
            next_ex_div_date=TODAY + timedelta(days=1),
            next_div_amount=0.50,
        )
        result = evaluate(pos, m, CTX)
        # Should NOT be dividend alert — enough extrinsic to protect
        assert not (isinstance(result, AlertResult) and "DIVIDEND" in result.reason)

    def test_otm_near_exdiv_no_alert(self):
        """OTM CC near ex-div → no dividend assignment risk (assignment not rational)."""
        pos = _pos(
            strike=55.0, assigned_basis=60.0, initial_credit=0.0,
            expiry=TODAY + timedelta(days=5),
        )
        m = _market(
            pos, spot=52.0, current_ask=0.10,
            chain=(),
            next_ex_div_date=TODAY + timedelta(days=1),
            next_div_amount=0.50,
        )
        result = evaluate(pos, m, CTX)
        assert not (isinstance(result, AlertResult) and "DIVIDEND" in result.reason)

    def test_exdiv_far_away_no_alert(self):
        """ITM CC, but ex-div 10 days away → no alert (time to manage)."""
        pos = _pos(
            strike=50.0, assigned_basis=55.0, initial_credit=0.0,
            expiry=TODAY + timedelta(days=15),
        )
        m = _market(
            pos, spot=52.0, current_ask=2.05,
            chain=(),
            next_ex_div_date=TODAY + timedelta(days=10),
            next_div_amount=0.50,
        )
        result = evaluate(pos, m, CTX)
        assert not (isinstance(result, AlertResult) and "DIVIDEND" in result.reason)

    def test_exdiv_today_fires(self):
        """Ex-div is TODAY (days=0) → should fire (≤ 1 day)."""
        pos = _pos(
            strike=50.0, assigned_basis=55.0, initial_credit=0.0,
            expiry=TODAY + timedelta(days=5),
        )
        chain = (_quote(51.0, TODAY + timedelta(days=10), bid=0.50, ask=0.55),)
        m = _market(
            pos, spot=52.0, current_ask=2.03,  # extrinsic=0.03
            chain=chain,
            next_ex_div_date=TODAY,
            next_div_amount=0.50,
        )
        result = evaluate(pos, m, CTX)
        assert isinstance(result, AlertResult)
        assert "DIVIDEND_ASSIGNMENT_RISK" in result.reason

    def test_no_div_data_passes_through(self):
        """No dividend data → check is skipped, normal evaluation continues."""
        pos = _pos(
            strike=50.0, assigned_basis=55.0, initial_credit=0.0,
            expiry=TODAY + timedelta(days=2),
        )
        chain = (_quote(51.0, TODAY + timedelta(days=10), bid=0.50, ask=0.55),)
        m = _market(
            pos, spot=52.0, current_ask=2.05,
            chain=chain,
            next_ex_div_date=None,
            next_div_amount=None,
        )
        result = evaluate(pos, m, CTX)
        # Should proceed to normal evaluation (roll in this case)
        assert not (isinstance(result, AlertResult) and "DIVIDEND" in result.reason)


# ---------------------------------------------------------------------------
# 1c: MIN_ANNUALIZED_ROLL_YIELD gate
# ---------------------------------------------------------------------------

class TestAnnualizedRollYield:
    """Verify yield floor rejects micro-credit rolls."""

    def test_annualized_roll_yield_helper(self):
        """Basic math: $0.50 credit on $50 strike, 7 DTE → ~52.1% ann."""
        y = _annualized_roll_yield(0.50, 50.0, 7)
        assert 0.52 < y < 0.53

    def test_annualized_roll_yield_zero_inputs(self):
        assert _annualized_roll_yield(0.0, 50.0, 7) == 0.0
        assert _annualized_roll_yield(0.50, 0.0, 7) == 0.0
        assert _annualized_roll_yield(0.50, 50.0, 0) == 0.0
        assert _annualized_roll_yield(-0.10, 50.0, 7) == 0.0

    def test_deeply_itm_micro_credit_recommends_assignment(self):
        """Deeply ITM CC, only roll candidate yields 1.7% ann → assignment recommended."""
        pos = _pos(
            strike=40.0, assigned_basis=55.0, initial_credit=0.0,
            expiry=TODAY + timedelta(days=2), roll_count=5,
        )
        # Roll candidate: strike=41, 7 DTE, bid=0.02 → credit=0.02-0.28=-0.26 (debit)
        # Actually we need a CREDIT roll that's just too small.
        # strike=41, bid=0.30, current ask=0.28 → net credit=0.02
        # ann yield = (0.02/41) * (365/7) = 0.0254 = 2.5% < 10%
        chain = (_quote(41.0, TODAY + timedelta(days=9), bid=0.30, ask=0.35),)
        m = _market(pos, spot=52.0, current_ask=0.28, chain=chain)
        result = evaluate(pos, m, CTX)
        assert isinstance(result, AlertResult), f"got {type(result).__name__}: {result}"
        assert "ROLL_YIELD_BELOW_FLOOR" in result.reason
        assert result.context["ann_yield"] < 0.10

    def test_healthy_credit_roll_proceeds(self):
        """Roll candidate with healthy yield → RollResult, not blocked."""
        pos = _pos(
            strike=50.0, assigned_basis=55.0, initial_credit=0.0,
            expiry=TODAY + timedelta(days=2),
        )
        # strike=51, bid=0.80, current ask=0.28 → net credit=0.52
        # ann yield = (0.52/51) * (365/7) = 0.5318 = 53% >> 10%
        chain = (_quote(51.0, TODAY + timedelta(days=9), bid=0.80, ask=0.85),)
        m = _market(pos, spot=52.0, current_ask=0.28, chain=chain)
        result = evaluate(pos, m, CTX)
        assert isinstance(result, RollResult)

    def test_debit_roll_exempt_from_yield_floor(self):
        """Below-basis defense debit roll proceeds — yield floor only on credits."""
        pos = _pos(
            strike=50.0, assigned_basis=55.0, initial_credit=0.0,
            expiry=TODAY + timedelta(days=2),
        )
        # strike=51, bid=0.20, current ask=0.28 → net credit=-0.08 (debit)
        # Debit rolls are exempt from yield floor
        chain = (_quote(51.0, TODAY + timedelta(days=9), bid=0.20, ask=0.25),)
        m = _market(pos, spot=52.0, current_ask=0.28, chain=chain)
        result = evaluate(pos, m, CTX)
        assert isinstance(result, RollResult)

    def test_custom_yield_floor(self):
        """Custom floor of 25% blocks a roll that passes default 10%."""
        pos = _pos(
            strike=50.0, assigned_basis=55.0, initial_credit=0.0,
            expiry=TODAY + timedelta(days=2),
        )
        # strike=51, bid=0.40, current ask=0.28 → net credit=0.12
        # ann yield = (0.12/51) * (365/7) = 0.1227 = 12.3% — passes 10% but not 25%
        chain = (_quote(51.0, TODAY + timedelta(days=9), bid=0.40, ask=0.45),)
        m = _market(pos, spot=52.0, current_ask=0.28, chain=chain)
        constraints = ConstraintMatrix(min_annualized_roll_yield=0.25)
        result = evaluate(pos, m, CTX, constraints)
        assert isinstance(result, AlertResult)
        assert "ROLL_YIELD_BELOW_FLOOR" in result.reason
