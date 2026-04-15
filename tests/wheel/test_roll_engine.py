"""
tests/wheel/test_roll_engine.py — pure-function evaluator unit tests.

WHEEL-3. No DB, no IB, no Telegram, no fixtures beyond stdlib. Each test
constructs the minimal Position / MarketSnapshot / PortfolioContext needed
to exercise one routing decision in isolation.

Convention:
- Helpers `_make_*` build minimal-valid instances with safe defaults.
- Tests override only the fields under test.
- `_chain` builds OptionQuote tuples for cascade tests.
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


def _chain(
    *,
    base_strike=100.0,
    strike_steps=(0, 5, 10),
    dte_offsets=(10, 17, 24, 35),
    bid_func=None,
    delta_func=None,
) -> tuple[OptionQuote, ...]:
    """Build a synthetic chain. bid_func(strike, dte) overrides default bid."""
    quotes = []
    for sd in strike_steps:
        for dte_off in dte_offsets:
            strike = base_strike + sd
            expiry = ASOF + timedelta(days=dte_off)
            if bid_func is not None:
                bid = bid_func(strike, dte_off)
            else:
                bid = max(0.05, 2.0 - sd * 0.10 + dte_off * 0.05)
            delta = (delta_func(strike, dte_off) if delta_func else max(0.05, 0.50 - sd * 0.05))
            quotes.append(OptionQuote(
                strike=strike,
                expiry=expiry,
                bid=bid,
                ask=round(bid + 0.05, 2),
                delta=delta,
                iv=0.40,
            ))
    return tuple(quotes)


# ---------------------------------------------------------------------------
# Regime gating
# ---------------------------------------------------------------------------

def test_regime_defense_when_spot_below_adjusted_basis():
    # initial_credit=0 disables the harvest gate so the regime branch is observable
    pos = _pos(adjusted_basis=110.0, initial_credit=0.0)
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=0.50, delta=0.20))
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HoldResult)
    assert "DEFENSE_HOLD" in result.reason


def test_regime_offense_when_spot_above_adjusted_basis():
    pos = _pos(adjusted_basis=80.0, strike=100.0, initial_credit=0.0)
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=0.50, delta=0.20))
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HoldResult)
    assert "OFFENSE_HOLD" in result.reason


def test_regime_defense_when_basis_unknown():
    """All basis fields None → safer to assume defense."""
    pos = _pos(adjusted_basis=None, assigned_basis=None, cost_basis=None,
               initial_credit=0.0)
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=0.50, delta=0.20))
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HoldResult)
    assert "DEFENSE_HOLD" in result.reason


# ---------------------------------------------------------------------------
# Defense — velocity-ratio harvest gate
# ---------------------------------------------------------------------------

def test_defense_harvest_when_velocity_ratio_high_and_pnl_high():
    # initial_credit=2.00, current ask=0.40 → P_pct=0.80
    # initial_dte=14, opened 3 days ago → T_pct=3/14=0.214
    # V_r = 0.80 / 0.214 = 3.74
    pos = _pos(initial_credit=2.00, initial_dte=14,
               opened_at=ASOF - timedelta(days=3))
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=0.40, delta=0.20))
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HarvestResult)
    assert result.pnl_pct >= 0.50
    assert result.velocity_ratio >= 1.5


def test_defense_no_harvest_when_pnl_below_floor():
    # P_pct = 0.20 — even if V_r is high, sub-50% blocks harvest
    pos = _pos(initial_credit=2.00, initial_dte=14,
               opened_at=ASOF - timedelta(days=1))
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=1.60, delta=0.20))
    result = evaluate(pos, market, _ctx())
    assert not isinstance(result, HarvestResult)


def test_defense_no_harvest_when_velocity_ratio_below_threshold():
    # P_pct=0.50 but T_pct=0.50 → V_r=1.0 (below 1.5 threshold)
    pos = _pos(initial_credit=2.00, initial_dte=14,
               opened_at=ASOF - timedelta(days=7))
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=1.00, delta=0.20))
    result = evaluate(pos, market, _ctx())
    assert not isinstance(result, HarvestResult)


def test_defense_harvest_skipped_when_initial_credit_zero():
    """V_r calc returns 0 when initial_credit ≤ 0; harvest gate must not fire."""
    pos = _pos(initial_credit=0.0)
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=0.10, delta=0.10))
    result = evaluate(pos, market, _ctx())
    assert not isinstance(result, HarvestResult)


# ---------------------------------------------------------------------------
# Defense — defensive roll trigger (extrinsic ≤ $0.10 AND ITM)
# ---------------------------------------------------------------------------

def test_defense_roll_when_itm_and_extrinsic_depleted():
    # spot=105, strike=100 → ITM by $5. ask=5.05 → extrinsic=$0.05
    chain = _chain(base_strike=100.0, strike_steps=(5, 10),
                   dte_offsets=(10,),
                   bid_func=lambda s, d: 6.00)  # generous bids on roll candidates
    market = _market(
        spot=105.0,
        chain=chain,
        current_call=_quote(strike=100.0, ask=5.05, delta=0.85),
    )
    pos = _pos(strike=100.0)
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, RollResult)
    assert result.cascade_tier == 1
    assert result.new_strike == 105.0


def test_defense_no_roll_when_extrinsic_above_threshold():
    # spot=105, strike=100, ask=5.50 → extrinsic=$0.50 > $0.10
    market = _market(
        spot=105.0,
        current_call=_quote(strike=100.0, ask=5.50, delta=0.85),
    )
    pos = _pos(strike=100.0)
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HoldResult)


def test_defense_no_roll_when_otm():
    # spot=95 < strike=100 → OTM, no defensive trigger even if ext is low.
    # initial_credit=0 disables the unrelated harvest gate.
    market = _market(
        spot=95.0,
        current_call=_quote(strike=100.0, ask=0.05, delta=0.10),
    )
    pos = _pos(strike=100.0, initial_credit=0.0)
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HoldResult)


# ---------------------------------------------------------------------------
# Defense — cascade tier coverage
# ---------------------------------------------------------------------------

def test_cascade_tier1_match_strike_plus_one_dte_7_to_14():
    # Force a defensive roll, build chain so only Tier 1 has a match
    chain = (
        OptionQuote(strike=105.0, expiry=ASOF + timedelta(days=10),
                    bid=6.00, ask=6.05, delta=0.50, iv=0.40),
    )
    market = _market(
        spot=105.0, chain=chain,
        current_call=_quote(strike=100.0, ask=5.05, delta=0.85),
    )
    pos = _pos(strike=100.0)
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, RollResult)
    assert result.cascade_tier == 1


def test_cascade_tier2_match_when_tier1_dte_window_empty():
    # Tier 1 needs DTE 7-14; Tier 2 needs DTE 14-21. Chain only has DTE 18.
    chain = (
        OptionQuote(strike=105.0, expiry=ASOF + timedelta(days=18),
                    bid=6.50, ask=6.55, delta=0.50, iv=0.40),
    )
    market = _market(
        spot=105.0, chain=chain,
        current_call=_quote(strike=100.0, ask=5.05, delta=0.85),
    )
    pos = _pos(strike=100.0)
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, RollResult)
    assert result.cascade_tier == 2


def test_cascade_tier3_match_strike_plus_two_dte_21_to_45():
    # Only candidate is +2 strikes at DTE 30
    chain = (
        OptionQuote(strike=110.0, expiry=ASOF + timedelta(days=30),
                    bid=6.50, ask=6.55, delta=0.45, iv=0.40),
        # Add the +1 strike that doesn't qualify (only one strike above current
        # would not let us request strike_step=2 if we only had 1, so must
        # include +1 at non-matching DTE)
        OptionQuote(strike=105.0, expiry=ASOF + timedelta(days=60),
                    bid=8.00, ask=8.05, delta=0.45, iv=0.40),
    )
    market = _market(
        spot=105.0, chain=chain,
        current_call=_quote(strike=100.0, ask=5.05, delta=0.85),
    )
    pos = _pos(strike=100.0)
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, RollResult)
    assert result.cascade_tier == 3
    assert result.new_strike == 110.0


def test_cascade_tier4_same_strike_fallback():
    # No higher strikes available — only same-strike at DTE 10
    chain = (
        OptionQuote(strike=100.0, expiry=ASOF + timedelta(days=10),
                    bid=6.00, ask=6.05, delta=0.85, iv=0.40),
    )
    market = _market(
        spot=105.0, chain=chain,
        current_call=_quote(strike=100.0, ask=5.05, delta=0.85),
    )
    pos = _pos(strike=100.0)
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, RollResult)
    assert result.cascade_tier == 4
    assert result.new_strike == 100.0


def test_cascade_exhausted_returns_alert_critical():
    # Chain exists but no candidate yields net credit ≥ $0.05
    chain = (
        OptionQuote(strike=105.0, expiry=ASOF + timedelta(days=10),
                    bid=4.00, ask=4.05, delta=0.50, iv=0.40),  # net credit = -1.05
        OptionQuote(strike=100.0, expiry=ASOF + timedelta(days=10),
                    bid=4.50, ask=4.55, delta=0.85, iv=0.40),  # net credit = -0.55
    )
    market = _market(
        spot=105.0, chain=chain,
        current_call=_quote(strike=100.0, ask=5.05, delta=0.85),
    )
    pos = _pos(strike=100.0)
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, AlertResult)
    assert result.severity == "CRITICAL"
    assert "cascade exhausted" in result.reason


def test_cascade_empty_chain_returns_alert():
    market = _market(
        spot=105.0, chain=(),
        current_call=_quote(strike=100.0, ask=5.05, delta=0.85),
    )
    pos = _pos(strike=100.0)
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, AlertResult)


# ---------------------------------------------------------------------------
# Defense — gamma cutoff
# ---------------------------------------------------------------------------

def test_defense_gamma_cutoff_forces_cascade():
    # DTE=2, ext=0.03, delta=0.50 → all three cutoff conditions met
    chain = (
        OptionQuote(strike=105.0, expiry=ASOF + timedelta(days=10),
                    bid=6.00, ask=6.05, delta=0.50, iv=0.40),
    )
    market = _market(
        spot=100.5, chain=chain,
        current_call=_quote(strike=100.0,
                            expiry=ASOF + timedelta(days=2),
                            ask=0.53, delta=0.50),
    )
    pos = _pos(strike=100.0, expiry=ASOF + timedelta(days=2),
               initial_credit=0.0)  # disables harvest gate
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, RollResult)
    assert "GAMMA_CUTOFF" in result.reason


# ---------------------------------------------------------------------------
# Offense — Opportunity Cost Breakeven
# ---------------------------------------------------------------------------

def test_offense_liquidate_when_breakeven_holds():
    # adjusted_basis=80, spot=150, deep ITM strike=100, call ask=51 (mostly intrinsic)
    # net_per_share = 150 - 51 = 99 > basis=80 → LIQUIDATE
    pos = _pos(strike=100.0, adjusted_basis=80.0, quantity=2)
    market = _market(
        spot=150.0,
        current_call=_quote(strike=100.0, ask=51.00, delta=0.95),
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, LiquidateResult)
    assert result.requires_human_approval is True
    assert result.contracts == 2
    assert result.shares == 200
    assert result.net_proceeds_per_share == pytest.approx(99.0, abs=0.01)


def test_offense_no_liquidate_when_breakeven_fails():
    # spot just barely above basis but buyback eats too much
    pos = _pos(strike=100.0, adjusted_basis=80.0)
    market = _market(
        spot=82.0,
        current_call=_quote(strike=100.0, ask=0.50, delta=0.20),  # OTM, not deep ITM
    )
    result = evaluate(pos, market, _ctx())
    assert not isinstance(result, LiquidateResult)


# ---------------------------------------------------------------------------
# Offense — gamma cutoff → AssignResult
# ---------------------------------------------------------------------------

def test_offense_gamma_cutoff_lets_assign():
    # Set adjusted_basis high enough that breakeven (spot - call_ask) < basis,
    # otherwise LiquidateResult fires before gamma cutoff.
    # spot=102, ask=2.03, basis=101 → net_per_share=99.97 < basis → no liquidate
    pos = _pos(strike=100.0, adjusted_basis=101.0,
               expiry=ASOF + timedelta(days=2),
               initial_credit=0.0)  # disables harvest path
    market = _market(
        spot=102.0,
        current_call=_quote(strike=100.0, expiry=ASOF + timedelta(days=2),
                            ask=2.03, delta=0.85),
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, AssignResult)
    assert "OFFENSE_LET_IT_CALL" in result.reason


# ---------------------------------------------------------------------------
# Offense — 90% harvest
# ---------------------------------------------------------------------------

def test_offense_harvest_at_90_pct_pnl():
    # initial_credit=2.00, ask=0.18 → P_pct = 0.91
    pos = _pos(adjusted_basis=80.0, initial_credit=2.00,
               opened_at=ASOF - timedelta(days=10))
    market = _market(
        spot=90.0,
        current_call=_quote(strike=100.0, ask=0.18, delta=0.10),
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, HarvestResult)
    assert result.pnl_pct >= 0.90
    assert "OFFENSE_HARVEST" in result.reason


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_legacy_null_inception_delta_routes_via_extrinsic():
    """The 9 fill_log NULL rows from WHEEL-1b — evaluator must not depend on
    inception_delta for routing. Position with inception_delta=None still
    routes correctly via extrinsic-based defensive trigger."""
    chain = (
        OptionQuote(strike=105.0, expiry=ASOF + timedelta(days=10),
                    bid=6.00, ask=6.05, delta=0.50, iv=0.40),
    )
    market = _market(
        spot=105.0, chain=chain,
        current_call=_quote(strike=100.0, ask=5.05, delta=0.85),
    )
    pos = _pos(strike=100.0, inception_delta=None)
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, RollResult)
    assert result.cascade_tier == 1


def test_evaluator_exception_returns_alert_critical():
    """Defensive try/except shell must convert any exception to AlertResult."""
    pos = _pos()
    # Pass a market with current_call=None — explicitly handled before regime gate
    market = MarketSnapshot(
        ticker="CRM", spot=90.0, iv30=0.45,
        chain=(), current_call=None, asof=ASOF,  # type: ignore[arg-type]
    )
    result = evaluate(pos, market, _ctx())
    assert isinstance(result, AlertResult)
    assert result.severity == "CRITICAL"


def test_eval_result_is_discriminated_union_kind_field():
    """Every result variant must expose a unique .kind for caller dispatch."""
    pos = _pos()
    market = _market(spot=90.0, current_call=_quote(strike=100.0, ask=0.50, delta=0.20))
    result = evaluate(pos, market, _ctx())
    assert hasattr(result, "kind")
    assert result.kind in {"HOLD", "HARVEST", "ROLL", "ASSIGN", "LIQUIDATE", "ALERT"}
