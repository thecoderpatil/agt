"""WHEEL roll-logic hardening tests — R1/R2/R4/R5/R7/R8.

Covers the 7-defect hardening MR on top of the WHEEL-3 Single Unified Evaluator.
Each test holds one defect's behavior as an invariant so regressions surface
instantly.
"""
from __future__ import annotations

from dataclasses import replace
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
        assigned_basis=55.0,
        adjusted_basis=55.0,                # defense by default
        initial_credit=1.20,
        initial_dte=30,
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
# R2 — Mode-1 defense min_credit default is $0.01
# ---------------------------------------------------------------------------

def test_r2_defense_min_credit_default_is_one_cent():
    """ConstraintMatrix default: defense cascade accepts a $0.01 credit per
    rulebook v10 §Mode-1 'any net credit, even $0.01'."""
    cm = ConstraintMatrix()
    assert cm.min_credit_per_contract == 0.01


def test_r2_defense_cascade_accepts_penny_credit():
    """Sub-basis ITM with only a $0.02 credit available → must roll, not alert."""
    pos = _pos()  # 2 DTE ITM when spot > 50
    # Chain: tier1 target is strike 51 (step 1), DTE in [7,14]
    tier1_exp = TODAY + timedelta(days=10)
    chain = (
        _quote(51.0, tier1_exp, bid=0.30, ask=0.35, delta=0.28),
    )
    m = _market(pos, spot=52.0, current_ask=0.28, chain=chain)
    # Net credit = 0.30 - 0.28 = 0.02 ≥ 0.01 default
    result = evaluate(pos, m, CTX)
    assert isinstance(result, RollResult), f"got {type(result).__name__}: {result}"
    assert result.cascade_tier == 1
    assert result.new_strike == 51.0


# ---------------------------------------------------------------------------
# R1 — Offense tries $0.20 roll before ASSIGN
# ---------------------------------------------------------------------------

def test_r1_offense_short_dte_itm_rolls_at_twenty_cents():
    """Offense regime with a viable $0.25 credit at tier1 → RollResult, not ASSIGN."""
    # basis 51.8 keeps offense (spot 52 > basis) but skips opp-cost breakeven
    # (net_proceeds 51.75 !> basis 51.8).
    pos = _pos(cost_basis=51.8, assigned_basis=51.8, adjusted_basis=51.8)
    tier1_exp = TODAY + timedelta(days=10)
    chain = (
        _quote(51.0, tier1_exp, bid=0.50, ask=0.55, delta=0.28),
    )
    m = _market(pos, spot=52.0, current_ask=0.25, chain=chain)
    # Net credit = 0.50 - 0.25 = 0.25 ≥ 0.20 offense floor
    result = evaluate(pos, m, CTX)
    assert isinstance(result, RollResult), f"got {type(result).__name__}: {result}"


def test_r1_offense_short_dte_itm_falls_back_to_assign():
    """Offense regime with only $0.05 credit available (below $0.20 floor) → ASSIGN."""
    pos = _pos(cost_basis=51.8, assigned_basis=51.8, adjusted_basis=51.8)
    tier1_exp = TODAY + timedelta(days=10)
    chain = (
        _quote(51.0, tier1_exp, bid=0.30, ask=0.35, delta=0.28),
    )
    m = _market(pos, spot=52.0, current_ask=0.25, chain=chain)
    # Net credit = 0.30 - 0.25 = 0.05 < 0.20 offense floor → cascade fails → ASSIGN
    result = evaluate(pos, m, CTX)
    assert isinstance(result, AssignResult), f"got {type(result).__name__}: {result}"


# ---------------------------------------------------------------------------
# R5 — Roll candidate filter: never select same-or-earlier expiry
# ---------------------------------------------------------------------------

def test_r5_roll_candidate_filters_same_expiry():
    """_select_roll_candidate must reject a quote on current_expiry even if net
    credit is otherwise sufficient (prevents "horizontal roll to self")."""
    current_exp = TODAY + timedelta(days=10)
    later_exp = TODAY + timedelta(days=14)
    chain = (
        _quote(51.0, current_exp, bid=1.00, ask=1.10, delta=0.28),  # same expiry — must skip
        _quote(51.0, later_exp, bid=0.50, ask=0.55, delta=0.28),    # proper forward roll
    )
    cand = _select_roll_candidate(
        chain=chain, current_strike=50.0, current_expiry=current_exp,
        current_call_ask=0.25, asof=TODAY,
        strike_step=1, dte_min=7, dte_max=21, min_credit=0.01,
    )
    assert cand is not None
    assert cand.expiry == later_exp


def test_r5_roll_candidate_filters_earlier_expiry():
    earlier_exp = TODAY + timedelta(days=5)
    current_exp = TODAY + timedelta(days=10)
    chain = (
        _quote(51.0, earlier_exp, bid=1.00, ask=1.10, delta=0.28),
    )
    cand = _select_roll_candidate(
        chain=chain, current_strike=50.0, current_expiry=current_exp,
        current_call_ask=0.25, asof=TODAY,
        strike_step=1, dte_min=3, dte_max=21, min_credit=0.01,
    )
    assert cand is None


# ---------------------------------------------------------------------------
# R4 — Ex-dividend gate (defense + offense)
# ---------------------------------------------------------------------------

def test_r4_defense_ex_div_within_dte_triggers_cascade():
    """ITM + ex-div inside DTE window + extrinsic < div → force cascade."""
    # Mid-DTE position to avoid R8 (short-DTE ITM) firing first.
    pos = _pos(
        expiry=TODAY + timedelta(days=7),
        opened_at=TODAY - timedelta(days=23),
    )
    ex_div = TODAY + timedelta(days=5)  # inside DTE window
    tier1_exp = TODAY + timedelta(days=14)  # tier1 DTE [7,14], > current_expiry
    chain = (
        _quote(51.0, tier1_exp, bid=2.35, ask=2.40, delta=0.30),
    )
    # Current call ITM (spot 52 > strike 50), extrinsic ≈ 0.30, div 0.50 → gate fires.
    m = _market(
        pos, spot=52.0, current_ask=2.30, chain=chain,
        next_ex_div_date=ex_div, next_div_amount=0.50,
    )
    result = evaluate(pos, m, CTX)
    assert isinstance(result, RollResult), f"got {type(result).__name__}: {result}"
    assert "EX_DIV_RISK" in result.reason


def test_r4_defense_ex_div_outside_dte_does_not_trigger():
    """Ex-div AFTER expiry → gate does not fire."""
    pos = _pos(
        expiry=TODAY + timedelta(days=10),
        opened_at=TODAY - timedelta(days=5),
    )
    ex_div_far = TODAY + timedelta(days=30)  # after expiry
    chain = (
        _quote(51.0, TODAY + timedelta(days=18), bid=0.60, ask=0.65, delta=0.30),
    )
    # Spot < strike (OTM), current_ask high enough that velocity harvest doesn't fire.
    m = _market(
        pos, spot=49.0, current_ask=0.90, chain=chain,
        next_ex_div_date=ex_div_far, next_div_amount=0.50,
    )
    result = evaluate(pos, m, CTX)
    assert isinstance(result, HoldResult), f"got {type(result).__name__}: {result}"


def test_r4_offense_ex_div_with_viable_roll_returns_roll():
    # basis 50 keeps offense (spot 52 > 50) but skips opp-cost breakeven
    # (net_proceeds 49.70 !> basis 50).
    pos = _pos(
        cost_basis=50.0, assigned_basis=50.0, adjusted_basis=50.0,
        expiry=TODAY + timedelta(days=7),
        opened_at=TODAY - timedelta(days=23),
    )
    ex_div = TODAY + timedelta(days=5)
    chain = (
        _quote(51.0, TODAY + timedelta(days=14), bid=2.80, ask=2.85, delta=0.40),
    )
    m = _market(
        pos, spot=52.0, current_ask=2.30, chain=chain,
        next_ex_div_date=ex_div, next_div_amount=0.50,
    )
    # Net credit 2.80 - 2.30 = 0.50 ≥ 0.20 offense floor
    result = evaluate(pos, m, CTX)
    assert isinstance(result, RollResult)
    assert "OFFENSE_EX_DIV" in result.reason


def test_r4_offense_ex_div_cascade_failure_assigns():
    pos = _pos(
        cost_basis=50.0, assigned_basis=50.0, adjusted_basis=50.0,
        expiry=TODAY + timedelta(days=7),
        opened_at=TODAY - timedelta(days=23),
    )
    ex_div = TODAY + timedelta(days=5)
    chain = (
        _quote(51.0, TODAY + timedelta(days=14), bid=2.35, ask=2.40, delta=0.40),
    )
    m = _market(
        pos, spot=52.0, current_ask=2.30, chain=chain,
        next_ex_div_date=ex_div, next_div_amount=0.50,
    )
    # Net credit 2.35 - 2.30 = 0.05 < 0.20 offense floor → cascade fails → OFFENSE_EX_DIV_ASSIGN
    result = evaluate(pos, m, CTX)
    assert isinstance(result, AssignResult)
    assert "OFFENSE_EX_DIV_ASSIGN" in result.reason


# ---------------------------------------------------------------------------
# R8 — Short-DTE ITM trigger replaces gamma cutoff
# ---------------------------------------------------------------------------

def test_r8_defense_short_dte_itm_forces_cascade_regardless_of_extrinsic():
    """2 DTE ITM with $0.25 extrinsic remaining → must still cascade.
    Prior gamma cutoff required extrinsic ≤ $0.05 AND delta ≥ 0.40; R8 fires
    purely on dte + ITM."""
    pos = _pos()  # 2 DTE ITM
    tier1_exp = TODAY + timedelta(days=10)
    chain = (
        _quote(51.0, tier1_exp, bid=0.50, ask=0.55, delta=0.28),
    )
    # Current call: spot 51 > strike 50, ask 2.25 → intrinsic 1, extrinsic 1.25
    # Prior gamma-cutoff would NOT fire (extrinsic >> 0.05).
    # R8 fires purely on dte ≤ 3 AND ITM.
    m = _market(pos, spot=51.0, current_ask=0.25, chain=chain)
    result = evaluate(pos, m, CTX)
    assert isinstance(result, RollResult), f"got {type(result).__name__}: {result}"
    assert "SHORT_DTE_ITM" in result.reason


def test_r8_defense_short_dte_otm_does_not_trigger():
    """2 DTE OTM → R8 does not fire, falls through to hold."""
    pos = _pos()
    chain = (
        _quote(51.0, TODAY + timedelta(days=10), bid=0.50, ask=0.55, delta=0.28),
    )
    m = _market(pos, spot=48.0, current_ask=0.05, chain=chain)  # OTM
    result = evaluate(pos, m, CTX)
    assert isinstance(result, HoldResult), f"got {type(result).__name__}: {result}"


def test_r8_offense_short_dte_itm_rolls_before_assigning():
    pos = _pos(
        cost_basis=51.3, assigned_basis=51.3, adjusted_basis=51.3,
    )  # offense but breakeven skipped (net 51.25 !> basis 51.3)
    chain = (
        _quote(51.0, TODAY + timedelta(days=10), bid=0.50, ask=0.55, delta=0.28),
    )
    m = _market(pos, spot=51.5, current_ask=0.25, chain=chain)
    # Net credit 0.50 - 0.25 = 0.25 ≥ 0.20
    result = evaluate(pos, m, CTX)
    assert isinstance(result, RollResult)
    assert "OFFENSE_SHORT_DTE_ITM" in result.reason


def test_r8_offense_short_dte_itm_cascade_exhausted_assigns():
    pos = _pos(cost_basis=51.3, assigned_basis=51.3, adjusted_basis=51.3)
    chain = (
        _quote(51.0, TODAY + timedelta(days=10), bid=0.30, ask=0.35, delta=0.28),
    )
    m = _market(pos, spot=51.5, current_ask=0.25, chain=chain)
    # Net credit 0.05 < 0.20 → fails → ASSIGN
    result = evaluate(pos, m, CTX)
    assert isinstance(result, AssignResult)
    assert "OFFENSE_LET_IT_CALL" in result.reason


# ---------------------------------------------------------------------------
# Constraint defaults sanity
# ---------------------------------------------------------------------------

def test_constraints_defaults_reflect_hardening():
    cm = ConstraintMatrix()
    assert cm.min_credit_per_contract == 0.01, "R2"
    assert cm.offense_roll_min_credit == 0.20, "R1"
    assert cm.offense_harvest_pnl == 0.90, "R3 override (deliberate)"
