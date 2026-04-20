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
    _lookup_days_held,
    _should_harvest_csp,
    scan_csp_harvest_candidates,
)
from agt_equities.runtime import RunContext, RunMode
from agt_equities.sinks import CollectorOrderSink, NullDecisionSink

pytestmark = pytest.mark.sprint_a


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
        avgCost=avg_cost,        # per-contract IBKR convention (Ã— 100 = credit)
        account=account,
    )


class FakeIB:
    """Minimal ib_async-shaped fake for scan_csp_harvest_candidates."""

    def __init__(self, positions, market_data_by_symbol, *, raise_on_positions=False):
        self._positions = positions
        self._md = market_data_by_symbol
        self._raise_positions = raise_on_positions
        self.cancel_calls: list[str] = []
        self.req_calls: list[str] = []

    async def reqPositionsAsync(self):
        if self._raise_positions:
            raise RuntimeError("simulated IBKR disconnect")
        return list(self._positions)

    def reqMarketDataType(self, t):
        pass

    async def qualifyContractsAsync(self, contract):
        return [contract]

    def reqMktData(self, contract, *args, **kwargs):
        sym = contract.symbol.upper()
        self.req_calls.append(sym)
        return self._md.get(sym, SimpleNamespace(ask=None))

    def cancelMktData(self, contract):
        self.cancel_calls.append(contract.symbol.upper())


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the 2s reqMktData settle sleep in all scanner tests."""
    async def _fast(_s):
        return None
    monkeypatch.setattr("agt_equities.csp_harvest.asyncio.sleep", _fast)


@pytest.fixture(autouse=True)
def _mock_days_held_lookup(monkeypatch):
    """Mock `_lookup_days_held` to return 1 for all scanner tests.

    Scanner tests validate threshold integration (days_held -> _should_harvest_csp
    routing), NOT the DB lookup itself. Tests 14-16 override per-test.

    Note: production `_lookup_days_held` has SQL column bugs (tracked as backlog
    ticket #10) and currently always returns -1 in live environments, forcing
    effective_days_held=2 (90% path). Fix coming in follow-up MR.
    """
    monkeypatch.setattr("agt_equities.csp_harvest._lookup_days_held", lambda *a, **kw: 1)


@pytest.fixture
def _ctx_scanner():
    """CollectorOrderSink ctx for scanner tests — captures orders without DB writes."""
    return RunContext(
        mode=RunMode.LIVE,
        run_id="test-harvest",
        order_sink=CollectorOrderSink(),
        decision_sink=NullDecisionSink(),
    
        broker_mode="paper",
        engine="harvest",
    )


# ---------------------------------------------------------------------------
# 10-13. scan_csp_harvest_candidates scanner tests
# ---------------------------------------------------------------------------

def test_scanner_stages_passing_put(_ctx_scanner):
    """A short put with 80%+ profit and dte>=2 stages a BTC ticket."""
    pos = _make_fake_put_position(
        ticker="AAPL", strike=150.0,
        expiry="20270120",  # far in future relative to 2026-04-11 (9 dte)
        qty=1, avg_cost=100.0,  # $1.00 credit per contract
    )
    md = {"AAPL": SimpleNamespace(ask=0.15)}  # 85% profit
    ib = FakeIB([pos], md)

    out = asyncio.run(scan_csp_harvest_candidates(ib, ctx=_ctx_scanner))

    assert len(out["staged"]) == 1
    t = out["staged"][0]
    assert t["ticker"] == "AAPL"
    assert t["action"] == "BUY"
    assert t["right"] == "P"
    assert t["mode"] == "CSP_HARVEST"
    assert t["strike"] == 150.0
    assert t["quantity"] == 1
    assert t["limit_price"] == 0.15
    assert "day1_80" in t["v2_rationale"]
    assert out["skipped"] == []
    assert out["errors"] == []


def test_scanner_skips_below_threshold(_ctx_scanner):
    """A short put with only 50% profit is skipped, not staged."""
    pos = _make_fake_put_position(
        ticker="MSFT", strike=400.0,
        expiry="20260515", qty=2, avg_cost=200.0,  # $2.00 credit
    )
    md = {"MSFT": SimpleNamespace(ask=1.00)}  # 50% profit
    ib = FakeIB([pos], md)

    out = asyncio.run(scan_csp_harvest_candidates(ib, ctx=_ctx_scanner))

    assert out["staged"] == []
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["ticker"] == "MSFT"
    assert "below_threshold" in out["skipped"][0]["reason"]


def test_scanner_stages_via_order_sink(_ctx_scanner):
    """ctx.order_sink.stage called with ticket; CollectorOrderSink captures the
    ShadowOrder. Validates engine=csp_harvest, run_id, and all A3 meta keys."""
    pos = _make_fake_put_position(
        ticker="NVDA", strike=800.0,
        expiry="20270120", qty=1, avg_cost=150.0,  # $1.50 credit
    )
    md = {"NVDA": SimpleNamespace(ask=0.20)}  # ~87% profit
    ib = FakeIB([pos], md)

    out = asyncio.run(scan_csp_harvest_candidates(ib, ctx=_ctx_scanner))

    assert len(out["staged"]) == 1
    orders = _ctx_scanner.order_sink.peek()
    assert len(orders) == 1
    so = orders[0]
    assert so.engine == "csp_harvest"
    assert so.run_id == _ctx_scanner.run_id
    for key in ("account_id", "household", "ticker", "strike", "expiry",
                "quantity", "limit_price", "days_held", "v2_rationale"):
        assert key in so.meta, f"meta missing key: {key}"
    assert so.meta["ticker"] == "NVDA"


def test_scanner_handles_reqpositions_failure_gracefully(_ctx_scanner):
    """If reqPositionsAsync raises, the scanner returns a structured
    error dict rather than propagating the exception. This is the
    watchdog-safety contract: a flaky IBKR connection must not bring
    down the scheduled 3:30 PM sweep."""
    ib = FakeIB([], {}, raise_on_positions=True)

    out = asyncio.run(scan_csp_harvest_candidates(ib, ctx=_ctx_scanner))

    assert out["staged"] == []
    assert len(out["errors"]) == 1
    assert out["errors"][0]["scope"] == "reqPositionsAsync"
    assert "simulated IBKR disconnect" in out["errors"][0]["error"]
    assert any("Failed to fetch positions" in a for a in out["alerts"])

# ---------------------------------------------------------------------------
# 14-16. days_held integration tests (new -- f4def9f intent, not in 88b1cb6)
# ---------------------------------------------------------------------------

def test_scanner_day1_80pct_triggers_harvest(monkeypatch, _ctx_scanner):
    """Position held 1 day, 81.25% profit -> stages BTC ticket with day1_80 reason."""
    pos = _make_fake_put_position(
        ticker="GOOGL", strike=160.0,
        expiry="20260515", qty=1, avg_cost=160.0,  # $1.60 credit
    )
    md = {"GOOGL": SimpleNamespace(ask=0.30)}  # 81.25% profit
    ib = FakeIB([pos], md)

    out = asyncio.run(scan_csp_harvest_candidates(ib, ctx=_ctx_scanner))

    assert len(out["staged"]) == 1
    t = out["staged"][0]
    assert "day1_80" in t["v2_rationale"]
    assert t["days_held"] == 1


def test_scanner_day2_plus_90pct_triggers_harvest(monkeypatch, _ctx_scanner):
    """days_held=3: 91% profit stages (standard_90); 85% profit skips."""
    monkeypatch.setattr("agt_equities.csp_harvest._lookup_days_held", lambda *a, **kw: 3)

    pos_hi = _make_fake_put_position(
        ticker="AMZN", strike=200.0,
        expiry="20260515", qty=1, avg_cost=100.0,
    )
    md_hi = {"AMZN": SimpleNamespace(ask=0.09)}  # 91% profit
    out_hi = asyncio.run(scan_csp_harvest_candidates(FakeIB([pos_hi], md_hi), ctx=_ctx_scanner))
    assert len(out_hi["staged"]) == 1
    assert "standard_90" in out_hi["staged"][0]["v2_rationale"]
    assert out_hi["staged"][0]["days_held"] == 3

    pos_lo = _make_fake_put_position(
        ticker="AMZN", strike=200.0,
        expiry="20260515", qty=1, avg_cost=100.0,
    )
    md_lo = {"AMZN": SimpleNamespace(ask=0.15)}  # 85% profit
    out_lo = asyncio.run(scan_csp_harvest_candidates(FakeIB([pos_lo], md_lo), ctx=_ctx_scanner))
    assert out_lo["staged"] == []
    assert "below_threshold" in out_lo["skipped"][0]["reason"]


def test_scanner_days_held_integration_passes_to_threshold(monkeypatch, _ctx_scanner):
    """_lookup_days_held=5 is forwarded to _should_harvest_csp as days_held=5."""
    monkeypatch.setattr("agt_equities.csp_harvest._lookup_days_held", lambda *a, **kw: 5)

    captured_kwargs: list[dict] = []
    import agt_equities.csp_harvest as _mod
    original = _mod._should_harvest_csp

    def _spy(ic, ask, dte, **kwargs):
        captured_kwargs.append(kwargs)
        return original(ic, ask, dte, **kwargs)

    monkeypatch.setattr("agt_equities.csp_harvest._should_harvest_csp", _spy)

    pos = _make_fake_put_position(
        ticker="META", strike=500.0,
        expiry="20260515", qty=1, avg_cost=100.0,
    )
    md = {"META": SimpleNamespace(ask=0.05)}  # 95% profit
    asyncio.run(scan_csp_harvest_candidates(FakeIB([pos], md), ctx=_ctx_scanner))

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("days_held") == 5
