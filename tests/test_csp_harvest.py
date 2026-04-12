"""
tests/test_csp_harvest.py

Unit tests for agt_equities.csp_harvest — the CSP profit-take
harvester dispatched in M2 (2026-04-11).

Structure (10 tests per the M2 dispatch):
  1-6. _should_harvest_csp threshold predicate tests
  7-10. scan_csp_harvest_candidates async scanner tests with FakeIB

The pure threshold predicate is tested directly. The async scanner
is driven by a FakeIB duck-typed against the ib_async interface
exercised in csp_harvest.scan_csp_harvest_candidates.

ISOLATION: this test file imports only stdlib + pytest + the
csp_harvest public API. It does NOT touch telegram_bot or any
screener module.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agt_equities.csp_harvest import (
    CSP_HARVEST_THRESHOLD_LAST_DAY,
    CSP_HARVEST_THRESHOLD_NEXT_DAY,
    _should_harvest_csp,
    scan_csp_harvest_candidates,
)


# ---------------------------------------------------------------------------
# 1-6. _should_harvest_csp threshold tests
# ---------------------------------------------------------------------------

def test_should_harvest_next_day_80_pct_passes():
    """dte >= 1 and profit_pct >= 0.80 → harvest."""
    # initial credit $1.00, ask $0.20 → 80% profit captured, 2 dte
    ok, reason = _should_harvest_csp(1.00, 0.20, dte=2)
    assert ok is True
    assert "next_day_80" in reason


def test_should_harvest_next_day_below_80_pct_fails():
    """dte >= 1 and profit_pct just under 0.80 → NO harvest."""
    # initial credit $1.00, ask $0.21 → 79% profit, 2 dte
    ok, reason = _should_harvest_csp(1.00, 0.21, dte=2)
    assert ok is False
    assert "below_threshold" in reason


def test_should_harvest_last_day_90_pct_passes():
    """dte <= 1 and profit_pct >= 0.90 → harvest (0DTE crunch)."""
    # initial $1.00, ask $0.10 → 90% profit, 0 dte
    ok, reason = _should_harvest_csp(1.00, 0.10, dte=0)
    assert ok is True
    # dte=0 cannot match next-day rule (needs dte>=1), so must be last-day
    assert "last_day_90" in reason


def test_should_harvest_last_day_85_pct_fails():
    """dte <= 1 but profit only 85% → NO harvest (needs 90% on last day)."""
    ok, reason = _should_harvest_csp(1.00, 0.15, dte=0)
    assert ok is False
    assert "below_threshold" in reason


def test_should_harvest_zero_credit_rejects():
    """Zero / negative initial_credit is rejected outright — no div by 0."""
    ok, reason = _should_harvest_csp(0.0, 0.10, dte=5)
    assert ok is False
    assert reason == "zero_credit"
    ok2, reason2 = _should_harvest_csp(-0.50, 0.10, dte=5)
    assert ok2 is False
    assert reason2 == "zero_credit"


def test_should_harvest_nan_ask_rejects():
    """NaN ask is rejected (guards the IBKR OPRA-missing path
    documented in C6.2 ib_chains)."""
    ok, reason = _should_harvest_csp(1.00, float("nan"), dte=3)
    assert ok is False
    assert reason == "nan_or_inf_input"
    # None also rejected
    ok2, reason2 = _should_harvest_csp(1.00, None, dte=3)
    assert ok2 is False
    assert reason2 == "missing_input"


# Sanity: thresholds match the dispatch spec
def test_threshold_constants_match_dispatch_spec():
    assert CSP_HARVEST_THRESHOLD_NEXT_DAY == 0.80
    assert CSP_HARVEST_THRESHOLD_LAST_DAY == 0.90


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
        avgCost=avg_cost,        # per-contract IBKR convention (× 100 = credit)
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


# ---------------------------------------------------------------------------
# 7-10. scan_csp_harvest_candidates scanner tests
# ---------------------------------------------------------------------------

def test_scanner_stages_passing_put():
    """A short put with 80%+ profit and dte>=2 stages a BTC ticket."""
    pos = _make_fake_put_position(
        ticker="AAPL", strike=150.0,
        expiry="20260420",  # far in future relative to 2026-04-11 (9 dte)
        qty=1, avg_cost=100.0,  # $1.00 credit per contract
    )
    md = {"AAPL": SimpleNamespace(ask=0.15)}  # 85% profit
    ib = FakeIB([pos], md)

    out = asyncio.run(scan_csp_harvest_candidates(ib))

    assert len(out["staged"]) == 1
    t = out["staged"][0]
    assert t["ticker"] == "AAPL"
    assert t["action"] == "BUY"
    assert t["right"] == "P"
    assert t["mode"] == "CSP_HARVEST"
    assert t["strike"] == 150.0
    assert t["quantity"] == 1
    assert t["limit_price"] == 0.15
    assert "next_day_80" in t["v2_rationale"]
    assert out["skipped"] == []
    assert out["errors"] == []


def test_scanner_skips_below_threshold():
    """A short put with only 50% profit is skipped, not staged."""
    pos = _make_fake_put_position(
        ticker="MSFT", strike=400.0,
        expiry="20260515", qty=2, avg_cost=200.0,  # $2.00 credit
    )
    md = {"MSFT": SimpleNamespace(ask=1.00)}  # 50% profit
    ib = FakeIB([pos], md)

    out = asyncio.run(scan_csp_harvest_candidates(ib))

    assert out["staged"] == []
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["ticker"] == "MSFT"
    assert "below_threshold" in out["skipped"][0]["reason"]


def test_scanner_invokes_staging_callback_with_ticket():
    """When staging_callback is provided, the scanner hands the
    ticket list to it. This is the injection seam that lets
    telegram_bot.cmd_csp_harvest wire in append_pending_tickets
    without csp_harvest importing telegram_bot."""
    pos = _make_fake_put_position(
        ticker="NVDA", strike=800.0,
        expiry="20260420", qty=1, avg_cost=150.0,  # $1.50 credit
    )
    md = {"NVDA": SimpleNamespace(ask=0.20)}  # ~87% profit
    ib = FakeIB([pos], md)

    captured: list[list[dict]] = []

    def sink(tickets):
        captured.append(list(tickets))

    out = asyncio.run(scan_csp_harvest_candidates(ib, staging_callback=sink))

    assert len(out["staged"]) == 1
    assert len(captured) == 1
    assert len(captured[0]) == 1
    assert captured[0][0]["ticker"] == "NVDA"
    assert captured[0][0]["mode"] == "CSP_HARVEST"


def test_scanner_handles_reqpositions_failure_gracefully():
    """If reqPositionsAsync raises, the scanner returns a structured
    error dict rather than propagating the exception. This is the
    watchdog-safety contract: a flaky IBKR connection must not bring
    down the scheduled 3:30 PM sweep."""
    ib = FakeIB([], {}, raise_on_positions=True)

    out = asyncio.run(scan_csp_harvest_candidates(ib))

    assert out["staged"] == []
    assert len(out["errors"]) == 1
    assert out["errors"][0]["scope"] == "reqPositionsAsync"
    assert "simulated IBKR disconnect" in out["errors"][0]["error"]
    assert any("Failed to fetch positions" in a for a in out["alerts"])
