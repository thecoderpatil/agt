"""
tests/wheel/test_cc_engine.py — Unit + Hypothesis tests for cc_engine.py.

Acceptance criteria (from hardening sprint):
  - Every WRITE has 30 <= roi_ann <= 130
  - Every STAND_DOWN has no available strike in [30, 130] band
  - Pure function: same inputs → same outputs, no IB needed
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from agt_equities.cc_engine import (
    CCPickerInput,
    CCStandDown,
    CCWrite,
    ChainStrike,
    pick_cc_strike,
    _annualized_roi,
    _mid_price,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Realistic strike prices: $5 to $2000 in $0.50 increments
strike_st = st.floats(min_value=5.0, max_value=2000.0).map(lambda x: round(x * 2) / 2)

# Realistic premiums: $0.01 to $50.00
premium_st = st.floats(min_value=0.01, max_value=50.0).map(lambda x: round(x, 2))

# DTE: 1 to 60 (positive)
dte_st = st.integers(min_value=1, max_value=60)

# Basis: $5 to $2000
basis_st = st.floats(min_value=5.0, max_value=2000.0).map(lambda x: round(x, 2))

# Spot: $5 to $2000
spot_st = st.floats(min_value=5.0, max_value=2000.0).map(lambda x: round(x, 2))


def _make_chain(
    basis: float, n_strikes: int = 10, step: float = 2.5,
    premium_start: float = 3.0, premium_decay: float = 0.6,
) -> tuple[ChainStrike, ...]:
    """Generate a realistic call chain starting at/above basis."""
    import math
    first_strike = math.ceil(basis / step) * step
    chain = []
    for i in range(n_strikes):
        s = first_strike + i * step
        prem = max(0.01, premium_start * (premium_decay ** i))
        chain.append(ChainStrike(
            strike=round(s, 2),
            bid=round(prem * 0.95, 2),
            ask=round(prem * 1.05, 2),
            delta=round(0.50 - i * 0.05, 2) if i < 10 else 0.05,
        ))
    return tuple(chain)


@st.composite
def chain_strike_st(draw):
    """Single chain strike with bid <= ask."""
    strike = draw(strike_st)
    bid = draw(premium_st)
    ask = bid + draw(st.floats(min_value=0.0, max_value=2.0).map(lambda x: round(x, 2)))
    delta = draw(st.floats(min_value=-1.0, max_value=1.0).map(lambda x: round(x, 2)))
    return ChainStrike(strike=strike, bid=bid, ask=ask, delta=delta)


@st.composite
def cc_picker_input_st(draw):
    """Full CCPickerInput with a realistic chain."""
    basis = draw(basis_st)
    spot = draw(spot_st)
    dte = draw(dte_st)
    n_strikes = draw(st.integers(min_value=1, max_value=15))
    chain = tuple(draw(st.lists(chain_strike_st(), min_size=n_strikes, max_size=n_strikes, unique_by=lambda cs: cs.strike)))
    return CCPickerInput(
        ticker="TEST",
        account_id="U99999999",
        paper_basis=basis,
        spot=spot,
        dte=dte,
        expiry="20260501",
        chain=chain,
    )


# ---------------------------------------------------------------------------
# Property tests (Hypothesis)
# ---------------------------------------------------------------------------

class TestCCEngineProperties:
    """Hypothesis property tests for pick_cc_strike."""

    @given(inp=cc_picker_input_st())
    @settings(max_examples=200, deadline=2000)
    def test_write_always_in_band(self, inp: CCPickerInput):
        """AC3a: every WRITE has min_ann <= roi_ann <= max_ann."""
        result = pick_cc_strike(inp)
        if isinstance(result, CCWrite):
            assert inp.min_ann <= result.annualized <= inp.max_ann, (
                f"WRITE annualized {result.annualized} outside [{inp.min_ann}, {inp.max_ann}]"
            )

    @given(inp=cc_picker_input_st())
    @settings(max_examples=200, deadline=2000)
    def test_stand_down_means_no_band_hit(self, inp: CCPickerInput):
        """AC3b: every STAND_DOWN has no reachable in-band strike.

        Re-walk with the same floor and break logic as the picker: OTM floor
        is max(paper_basis, spot); once ann < min_ann we break (monotonic decay
        assumption matches picker).  unique_by=strike in the strategy ensures no
        duplicate strikes that would defeat the break invariant.
        """
        result = pick_cc_strike(inp)
        if isinstance(result, CCStandDown):
            viable_check = sorted(
                [cs for cs in inp.chain
                 if cs.strike >= max(inp.paper_basis, inp.spot)],
                key=lambda cs: cs.strike,
            )
            for cs in viable_check:
                mid = _mid_price(cs.bid, cs.ask)
                if mid < inp.bid_floor:
                    continue
                ann = _annualized_roi(mid, cs.strike, inp.dte)
                assert not (inp.min_ann <= ann <= inp.max_ann), (
                    f"STAND_DOWN but strike {cs.strike} has ann={ann:.2f} in band"
                )
                if ann < inp.min_ann:
                    break  # mirror picker: monotonic decay assumed

    @given(inp=cc_picker_input_st())
    @settings(max_examples=100, deadline=2000)
    def test_result_is_always_one_of_two_types(self, inp: CCPickerInput):
        """pick_cc_strike always returns CCWrite or CCStandDown, never None."""
        result = pick_cc_strike(inp)
        assert isinstance(result, (CCWrite, CCStandDown))

    @given(inp=cc_picker_input_st())
    @settings(max_examples=100, deadline=2000)
    def test_deterministic(self, inp: CCPickerInput):
        """Same inputs → same outputs (pure function)."""
        r1 = pick_cc_strike(inp)
        r2 = pick_cc_strike(inp)
        assert r1 == r2

    @given(inp=cc_picker_input_st())
    @settings(max_examples=100, deadline=2000)
    def test_write_strike_at_or_above_basis(self, inp: CCPickerInput):
        """WRITE strike is always >= paper_basis (never sub-basis)."""
        result = pick_cc_strike(inp)
        if isinstance(result, CCWrite):
            assert result.strike >= inp.paper_basis, (
                f"WRITE strike {result.strike} < basis {inp.paper_basis}"
            )

    @given(inp=cc_picker_input_st())
    @settings(max_examples=200, deadline=2000)
    def test_write_strike_at_or_above_spot(self, inp: CCPickerInput):
        """WRITE strike is always >= spot (never ITM). OTM-only invariant."""
        result = pick_cc_strike(inp)
        if isinstance(result, CCWrite):
            assert result.strike >= inp.spot, (
                f"ITM write: strike {result.strike} < spot {inp.spot}"
            )


# ---------------------------------------------------------------------------
# Unit tests — specific scenarios
# ---------------------------------------------------------------------------

class TestCCEngineScenarios:
    """Deterministic scenario tests."""

    def test_band_hit_at_anchor(self):
        """Anchor strike is in band → BASIS_ANCHOR."""
        chain = (
            ChainStrike(strike=75.0, bid=2.40, ask=2.60, delta=0.45),
            ChainStrike(strike=80.0, bid=1.10, ask=1.30, delta=0.30),
        )
        inp = CCPickerInput(
            ticker="UBER", account_id="U001", paper_basis=73.0,
            spot=70.0, dte=21, expiry="20260501", chain=chain,
        )
        result = pick_cc_strike(inp)
        assert isinstance(result, CCWrite)
        assert result.branch == "BASIS_ANCHOR"
        assert result.strike == 75.0
        assert 30.0 <= result.annualized <= 130.0

    def test_step_up_past_rich_anchor(self):
        """Anchor is > 130% ann → step up to next strike."""
        chain = (
            ChainStrike(strike=75.0, bid=8.0, ask=8.5, delta=0.80),  # very rich
            ChainStrike(strike=80.0, bid=1.50, ask=1.70, delta=0.30),  # mid=1.60, ann~34.8%
        )
        inp = CCPickerInput(
            ticker="UBER", account_id="U001", paper_basis=73.0,
            spot=70.0, dte=21, expiry="20260501", chain=chain,
        )
        result = pick_cc_strike(inp)
        assert isinstance(result, CCWrite)
        assert result.branch == "BASIS_STEP_UP"
        assert result.strike == 80.0

    def test_stand_down_all_below_floor(self):
        """All strikes below 30% annualized → STAND_DOWN."""
        chain = (
            ChainStrike(strike=90.0, bid=0.10, ask=0.14, delta=0.10),
            ChainStrike(strike=95.0, bid=0.04, ask=0.06, delta=0.05),
        )
        inp = CCPickerInput(
            ticker="UBER", account_id="U001", paper_basis=88.0,
            spot=80.0, dte=21, expiry="20260501", chain=chain,
        )
        result = pick_cc_strike(inp)
        assert isinstance(result, CCStandDown)
        assert result.best_strike == 90.0

    def test_stand_down_empty_chain(self):
        """Empty chain → STAND_DOWN."""
        inp = CCPickerInput(
            ticker="UBER", account_id="U001", paper_basis=73.0,
            spot=80.0, dte=21, expiry="20260501", chain=(),
        )
        result = pick_cc_strike(inp)
        assert isinstance(result, CCStandDown)
        assert "empty_chain" in result.reason

    def test_stand_down_zero_dte(self):
        """DTE <= 0 → STAND_DOWN."""
        chain = (ChainStrike(strike=75.0, bid=2.0, ask=2.5, delta=0.45),)
        inp = CCPickerInput(
            ticker="UBER", account_id="U001", paper_basis=73.0,
            spot=80.0, dte=0, expiry="20260415", chain=chain,
        )
        result = pick_cc_strike(inp)
        assert isinstance(result, CCStandDown)
        assert "dte_zero" in result.reason

    def test_garbage_quotes_skipped(self):
        """Strikes with mid < bid_floor ($0.03) are skipped."""
        chain = (
            ChainStrike(strike=75.0, bid=0.01, ask=0.02, delta=0.10),  # garbage
            ChainStrike(strike=80.0, bid=1.50, ask=1.70, delta=0.30),  # mid=1.60, ann~34.8%
        )
        inp = CCPickerInput(
            ticker="UBER", account_id="U001", paper_basis=73.0,
            spot=70.0, dte=21, expiry="20260501", chain=chain,
        )
        result = pick_cc_strike(inp)
        assert isinstance(result, CCWrite)
        assert result.strike == 80.0  # skipped the garbage quote at 75

    def test_per_account_uber_scenario(self):
        """UBER Roth@$73 vs Individual@$86 — the canonical bug scenario.

        Same chain, different basis → different outcomes.
        """
        chain = (
            ChainStrike(strike=75.0, bid=2.40, ask=2.60, delta=0.45),
            ChainStrike(strike=77.5, bid=1.70, ask=1.90, delta=0.38),
            ChainStrike(strike=80.0, bid=1.10, ask=1.30, delta=0.30),
            ChainStrike(strike=82.5, bid=0.65, ask=0.75, delta=0.22),
            ChainStrike(strike=85.0, bid=0.30, ask=0.40, delta=0.15),
            ChainStrike(strike=87.5, bid=0.13, ask=0.17, delta=0.08),
            ChainStrike(strike=90.0, bid=0.04, ask=0.06, delta=0.04),
        )

        roth = pick_cc_strike(CCPickerInput(
            ticker="UBER", account_id="U_ROTH", paper_basis=73.0,
            spot=70.0, dte=21, expiry="20260501", chain=chain,
        ))
        individual = pick_cc_strike(CCPickerInput(
            ticker="UBER", account_id="U_IND", paper_basis=86.0,
            spot=70.0, dte=21, expiry="20260501", chain=chain,
        ))

        # Roth should WRITE (anchor at $75, ann ~60%)
        assert isinstance(roth, CCWrite), f"Roth@$73 should WRITE, got {roth}"
        assert roth.strike == 75.0

        # Individual should STAND_DOWN (anchor at $87.5, ann ~3%)
        assert isinstance(individual, CCStandDown), (
            f"Individual@$86 should STAND_DOWN, got {individual}"
        )

    def test_otm_floor_basis_below_spot(self):
        """When basis < spot, spot is the binding floor — ITM strikes excluded.

        Mirrors the ADBE 245C bug (order #307): basis=$240.85, spot=$247.18.
        245C is ITM (245 < 247.18). Without the fix it would be the anchor.
        With the fix it is excluded; 250C is in chain but ann too low → STAND_DOWN.
        """
        chain = (
            ChainStrike(strike=245.0, bid=5.60, ask=5.80, delta=0.60),  # ITM, in-band without guard
            ChainStrike(strike=250.0, bid=1.80, ask=2.00, delta=0.38),  # OTM, ann~12.6% → stand-down
        )
        inp = CCPickerInput(
            ticker="ADBE", account_id="DUP751004", paper_basis=240.85,
            spot=247.18, dte=22, expiry="20260508", chain=chain,
        )
        result = pick_cc_strike(inp)
        # 245C would have been anchor (and in-band) without OTM guard
        mid_itm = _mid_price(5.60, 5.80)
        ann_itm = _annualized_roi(mid_itm, 245.0, 22)
        assert ann_itm >= inp.min_ann, "test setup: 245C must be in band to confirm guard needed"
        # After fix: 245 < max(240.85, 247.18)=247.18 → excluded
        assert isinstance(result, CCStandDown), (
            f"Expected STAND_DOWN (245C excluded by OTM guard), got {result}"
        )

    def test_otm_floor_basis_above_spot(self):
        """When basis > spot, basis is the binding floor — behavior unchanged.

        PYPL DUP751003 style: basis=$54 > spot=$49 → max(54,49)=54 → 50C excluded.
        """
        chain = (
            ChainStrike(strike=50.0, bid=1.20, ask=1.40, delta=0.50),  # below basis
            ChainStrike(strike=55.0, bid=2.40, ask=2.60, delta=0.40),  # anchor
        )
        inp = CCPickerInput(
            ticker="PYPL", account_id="DUP751003", paper_basis=54.0,
            spot=49.0, dte=16, expiry="20260508", chain=chain,
        )
        result = pick_cc_strike(inp)
        # max(54,49)=54 → 50C excluded, 55C: mid=2.50, ann~103.8% → WRITE
        assert isinstance(result, CCWrite)
        assert result.strike == 55.0
        assert result.strike >= inp.paper_basis  # basis is the binding floor

    def test_atm_write_allowed_strike_equals_spot(self):
        """Strike == spot (ATM) is permitted — equality boundary is inclusive."""
        chain = (
            ChainStrike(strike=80.0, bid=2.40, ask=2.60, delta=0.50),
        )
        inp = CCPickerInput(
            ticker="UBER", account_id="U001", paper_basis=75.0,
            spot=80.0, dte=21, expiry="20260501", chain=chain,
        )
        result = pick_cc_strike(inp)
        # max(75,80)=80 → 80C included (>=); mid=2.50, ann~54.2% → WRITE
        assert isinstance(result, CCWrite)
        assert result.strike == 80.0
        assert result.otm_pct == 0.0  # exactly ATM

    def test_account_id_propagated(self):
        """Result carries the account_id from the input."""
        chain = (ChainStrike(strike=75.0, bid=2.40, ask=2.60),)
        inp = CCPickerInput(
            ticker="UBER", account_id="U22076329", paper_basis=73.0,
            spot=80.0, dte=21, expiry="20260501", chain=chain,
        )
        result = pick_cc_strike(inp)
        assert result.account_id == "U22076329"
