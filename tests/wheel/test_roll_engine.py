"""
tests/wheel/test_roll_engine.py — WHEEL-6 evaluator unit tests.

Pure-function tests covering the empirical-rules rewrite. No DB, no IB,
no Telegram. Each test builds minimal Position/MarketSnapshot/PortfolioContext
to exercise one routing decision.

Decision tree under test:
  1. Missing current_call → ALERT
  2. Expiry day → HOLD (let ride)
  3. 80/90 harvest → HARVEST
  4. Strike ≥ paper basis, ITM → ASSIGN (or LIQUIDATE)
  5. Strike ≥ paper basis, OTM → HOLD
  6. Strike < paper basis, OTM → HOLD
  7. Strike < paper basis, ITM, not urgent → HOLD
  8. Strike < paper basis, ITM, urgent → ROLL (+1/+1)
  9. Max rolls → ALERT
  10. No candidate → ALERT
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from agt_equities.roll_engine import (
    AlertResult,
    AssignResult,
    ConstraintMatrix,
    EvalResult,
    HarvestResult,
    HoldResult,
    LiquidateResult,
    MarketSnapshot,
    OptionQuote,
    Position,
    PortfolioContext,
    RollResult,
    evaluate,
)


ASOF = date(2026, 4, 15)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _ctx(mode="WARTIME", leverage=1.71) -> PortfolioContext:
    return PortfolioContext(household="Yash_Household", mode=mode, leverage=leverage)


def _quote(strike=100.0, expiry=None, bid=1.00, ask=1.05, delta=0.30, iv=0.40) -> OptionQuote:
    return OptionQuote(
        strike=strike,
        expiry=expiry or (ASOF + timedelta(days=10)),
        bid=bid,
        ask=ask,
        delta=delta,
        iv=iv,
    )


def _pos(
    *,
    strike=100.0,
    expiry=None,
    quantity=1,
    cost_basis=120.0,
    inception_delta=0.30,
    opened_at=None,
    initial_credit=2.00,
    initial_dte=14,
    assigned_basis=120.0,
    adjusted_basis=110.0,
    avg_premium_collected=2.00,
    cumulative_roll_debit=0.0,
    roll_count=0,
) -> Position:
    return Position(
        ticker="CRM",
        account_id="U21971297",
        household="Yash_Household",
        strike=strike,
        expiry=expiry or (ASOF + timedelta(days=10)),
        quantity=quantity,
        cost_basis=cost_basis,
        inception_delta=inception_delta,
        opened_at=opened_at or (ASOF - timedelta(days=4)),
        avg_premium_collected=avg_premium_collected,
        assigned_basis=assigned_basis,
        adjusted_basis=adjusted_basis,
        initial_credit=initial_credit,
        initial_dte=initial_dte,
        cumulative_roll_debit=cumulative_roll_debit,
        roll_count=roll_count,
    )


def _market(
    *,
    spot=90.0,
    chain=(),
    current_call=None,
    asof=ASOF,
) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="CRM",
        spot=spot,
        iv30=0.45,
        chain=tuple(chain),
        current_call=current_call or _quote(),
        asof=asof,
    )


# ---------------------------------------------------------------------------
# 1. Safety: missing current_call
# ---------------------------------------------------------------------------

def test_missing_current_call_returns_alert():
    pos = _pos()
    market = MarketSnapshot(
        ticker="CRM", spot=90.0, iv30=0.45,
        chain=(), current_call=None, asof=ASOF,  # type: ignore[arg-type]
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, AlertResult)
    assert result.severity == "CRITICAL"


# ---------------------------------------------------------------------------
# 2. Expiry day — let ride
# ---------------------------------------------------------------------------

def test_expiry_day_hold():
    pos = _pos(expiry=ASOF)  # DTE = 0
    market = _market(spot=90.0, current_call=_quote(ask=0.03))
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HoldResult)
    assert "EXPIRY_LET_RIDE" in result.reason


# ---------------------------------------------------------------------------
# 3. Canonical 80/90 harvest
# ---------------------------------------------------------------------------

def test_harvest_day1_80pct():
    """Day-1 position at ≥80% profit → harvest."""
    pos = _pos(initial_credit=2.00, opened_at=ASOF)  # days_held=0
    market = _market(spot=90.0, current_call=_quote(ask=0.40))
    # P_pct = (2.00 - 0.40) / 2.00 = 0.80
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HarvestResult)
    assert result.pnl_pct >= 0.80
    assert "DAY1_HARVEST" in result.reason


def test_harvest_day2_plus_90pct():
    """Day-2+ position at ≥90% profit → harvest."""
    pos = _pos(initial_credit=2.00, opened_at=ASOF - timedelta(days=5))
    market = _market(spot=90.0, current_call=_quote(ask=0.18))
    # P_pct = (2.00 - 0.18) / 2.00 = 0.91
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HarvestResult)
    assert result.pnl_pct >= 0.90
    assert "CANONICAL_90" in result.reason


def test_no_harvest_below_threshold():
    """Day-2 position at 60% profit → no harvest."""
    pos = _pos(initial_credit=2.00, opened_at=ASOF - timedelta(days=5))
    market = _market(spot=90.0, current_call=_quote(ask=0.80))
    # P_pct = (2.00 - 0.80) / 2.00 = 0.60 < 0.90
    result = evaluate(pos, market, _ctx())
    assert not isinstance(result, HarvestResult)


def test_no_harvest_zero_initial_credit():
    """Zero initial_credit → harvest gate disabled."""
    pos = _pos(initial_credit=0.0)
    market = _market(spot=90.0, current_call=_quote(ask=0.01))
    result = evaluate(pos, market, _ctx())
    assert not isinstance(result, HarvestResult)


# ---------------------------------------------------------------------------
# 4. Strike ≥ paper basis — welcome assignment
# ---------------------------------------------------------------------------

def test_above_basis_itm_lets_assign():
    """Strike ≥ paper basis AND ITM → ASSIGN.

    Set prices so net_proceeds (spot - ask) ≤ basis, avoiding the
    LiquidateResult opportunity-cost gate.
    """
    # paper basis = 95, strike = 100, spot = 101, ask = 7.00
    # net_proceeds = 101 - 7.00 = 94.00 ≤ basis 95 → no liquidate
    pos = _pos(strike=100.0, assigned_basis=95.0, initial_credit=0.0)
    market = _market(spot=101.0, current_call=_quote(strike=100.0, ask=7.00))
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, AssignResult)
    assert "ABOVE_PAPER_BASIS" in result.reason


def test_above_basis_otm_holds():
    """Strike ≥ paper basis AND OTM → HOLD."""
    pos = _pos(strike=100.0, assigned_basis=95.0, initial_credit=0.0)
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=0.50))
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HoldResult)
    assert "ABOVE_PAPER_BASIS_OTM" in result.reason


def test_above_basis_liquidate_opportunity():
    """Strike ≥ basis, deep ITM, BTC+STC net > basis → LIQUIDATE."""
    # basis=80, strike=100, spot=150, call ask=51 → net=99 > 80
    pos = _pos(strike=100.0, assigned_basis=80.0, quantity=2, initial_credit=0.0)
    market = _market(
        spot=150.0,
        current_call=_quote(strike=100.0, ask=51.00, delta=0.95),
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, LiquidateResult)
    assert result.requires_human_approval is True
    assert result.contracts == 2
    assert result.shares == 200


# ---------------------------------------------------------------------------
# 5. Below paper basis — OTM → hold
# ---------------------------------------------------------------------------

def test_below_basis_otm_holds():
    """Strike < paper basis, OTM → hold, theta working."""
    # paper basis = 120, strike = 100, spot = 90 → OTM, below basis
    pos = _pos(strike=100.0, assigned_basis=120.0, initial_credit=0.0)
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=0.50))
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HoldResult)
    assert "BELOW_BASIS_OTM" in result.reason


# ---------------------------------------------------------------------------
# 6. Below basis, ITM but not urgent — hold
# ---------------------------------------------------------------------------

def test_below_basis_itm_not_urgent_holds():
    """ITM but DTE=10, extrinsic=$2.00 → not urgent, hold."""
    pos = _pos(
        strike=100.0, assigned_basis=120.0, initial_credit=0.0,
        expiry=ASOF + timedelta(days=10),
    )
    market = _market(
        spot=105.0,
        current_call=_quote(
            strike=100.0, ask=7.00,  # intrinsic=5, extrinsic=2.00
            expiry=ASOF + timedelta(days=10),
        ),
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HoldResult)
    assert "NOT_URGENT" in result.reason


# ---------------------------------------------------------------------------
# 7. Below basis, ITM, urgent — ROLL
# ---------------------------------------------------------------------------

def test_below_basis_itm_short_dte_rolls():
    """Strike < basis, ITM, DTE=2 → roll +1 strike."""
    pos = _pos(
        strike=100.0, assigned_basis=120.0, initial_credit=0.0,
        expiry=ASOF + timedelta(days=2),
    )
    chain = (
        _quote(strike=105.0, expiry=ASOF + timedelta(days=10), bid=6.00, ask=6.05),
    )
    market = _market(
        spot=105.0,
        chain=chain,
        current_call=_quote(
            strike=100.0, ask=5.05,
            expiry=ASOF + timedelta(days=2),
        ),
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, RollResult)
    assert result.new_strike == 105.0
    assert "ROLL_UP_OUT" in result.reason


def test_below_basis_itm_extrinsic_depleted_rolls():
    """Strike < basis, ITM, DTE=15 but extrinsic=$0.05 → roll.

    Chain candidate must be within fallback DTE window (3-21) and have
    expiry > current expiry.
    """
    pos = _pos(
        strike=100.0, assigned_basis=120.0, initial_credit=0.0,
        expiry=ASOF + timedelta(days=15),
    )
    # Candidate: strike 105, expiry 7 days after current (DTE=22 from asof,
    # within fallback 3-21? No — need DTE within window. Use DTE=20.)
    chain = (
        _quote(strike=105.0, expiry=ASOF + timedelta(days=20), bid=6.00, ask=6.05),
    )
    market = _market(
        spot=115.0,
        chain=chain,
        current_call=_quote(
            strike=100.0, ask=15.05,  # intrinsic=15, extrinsic=0.05
            expiry=ASOF + timedelta(days=15),
        ),
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, RollResult)
    assert result.new_strike == 105.0


def test_roll_uses_fallback_dte_window():
    """No candidate in primary 5-14 DTE window, but exists in fallback 3-21."""
    pos = _pos(
        strike=100.0, assigned_basis=120.0, initial_credit=0.0,
        expiry=ASOF + timedelta(days=2),
    )
    chain = (
        _quote(strike=105.0, expiry=ASOF + timedelta(days=18), bid=6.50, ask=6.55),
    )
    market = _market(
        spot=105.0,
        chain=chain,
        current_call=_quote(
            strike=100.0, ask=5.05,
            expiry=ASOF + timedelta(days=2),
        ),
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, RollResult)
    assert result.new_strike == 105.0


# ---------------------------------------------------------------------------
# 8. Circuit breakers
# ---------------------------------------------------------------------------

def test_max_rolls_alerts():
    """roll_count ≥ max_rolls → ALERT, not ROLL."""
    pos = _pos(
        strike=100.0, assigned_basis=120.0, initial_credit=0.0,
        expiry=ASOF + timedelta(days=2),
        roll_count=10,
    )
    chain = (
        _quote(strike=105.0, expiry=ASOF + timedelta(days=10), bid=6.00, ask=6.05),
    )
    market = _market(
        spot=105.0,
        chain=chain,
        current_call=_quote(strike=100.0, ask=5.05, expiry=ASOF + timedelta(days=2)),
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, AlertResult)
    assert "MAX_ROLLS" in result.reason


# ---------------------------------------------------------------------------
# 9. No roll candidate → ALERT
# ---------------------------------------------------------------------------

def test_no_candidate_alerts():
    """Empty chain → no roll candidate → ALERT."""
    pos = _pos(
        strike=100.0, assigned_basis=120.0, initial_credit=0.0,
        expiry=ASOF + timedelta(days=2),
    )
    market = _market(
        spot=105.0,
        chain=(),
        current_call=_quote(strike=100.0, ask=5.05, expiry=ASOF + timedelta(days=2)),
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, AlertResult)
    assert "NO_ROLL_CANDIDATE" in result.reason


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_basis_unknown_assumes_below_basis():
    """All basis fields None → assume below basis (safer, never assign at unknown loss)."""
    pos = _pos(
        assigned_basis=None, adjusted_basis=None, cost_basis=None,
        initial_credit=0.0,
    )
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=0.50))
    result = evaluate(pos, market, _ctx())
    # OTM with unknown basis → should hold (below-basis OTM path)
    assert isinstance(result, HoldResult)


def test_basis_falls_back_to_cost_basis():
    """assigned_basis=None → falls back to cost_basis for paper basis."""
    # spot=101, ask=7.00 → net_proceeds=94 ≤ basis=95 → no liquidate
    pos = _pos(
        strike=100.0, assigned_basis=None, cost_basis=95.0,
        initial_credit=0.0,
    )
    market = _market(spot=101.0, current_call=_quote(strike=100.0, ask=7.00))
    # strike=100 >= cost_basis=95 → above basis → let assign
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, AssignResult)


def test_null_inception_delta_routes_correctly():
    """inception_delta=None must not crash — field is vestigial."""
    pos = _pos(inception_delta=None, initial_credit=0.0)
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=0.50))
    result = evaluate(pos, market, _ctx())
    assert hasattr(result, "kind")


def test_exception_returns_alert_critical():
    """Defensive try/except shell converts any exception to AlertResult."""
    pos = _pos()
    market = MarketSnapshot(
        ticker="CRM", spot=90.0, iv30=0.45,
        chain=(), current_call=None, asof=ASOF,  # type: ignore[arg-type]
    )
   