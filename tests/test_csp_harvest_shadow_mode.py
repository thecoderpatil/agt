"""
tests/test_csp_harvest_shadow_mode.py

Shadow-mode contract tests for scan_csp_harvest_candidates after ADR-008
MR 3 ctx opt-in. Mirrors tests/test_csp_allocator_shadow_mode.py pattern.

7 sprint_a tests:
  1. test_scan_requires_ctx_kwarg
  2. test_scan_live_sink_byte_identical_staging
  3. test_scan_collector_sink_captures_shadow_orders
  4. test_scan_shadow_mode_writes_nothing_to_prod_db
  5. test_scan_empty_positions_returns_empty_staged
  6. test_scan_meta_keys_roundtrip
  7. test_shadow_scan_harvest_engine_wired
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agt_equities.csp_harvest import scan_csp_harvest_candidates
from agt_equities.runtime import RunContext, RunMode
from agt_equities.sinks import (
    CollectorOrderSink,
    NullDecisionSink,
    SQLiteOrderSink,
)

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_fake_pos(ticker="AAPL", strike=150.0, expiry="20260515", qty=1,
                   avg_cost=100.0, account="U21971297"):
    contract = SimpleNamespace(
        symbol=ticker, secType="OPT", right="P",
        strike=strike, lastTradeDateOrContractMonth=expiry,
    )
    return SimpleNamespace(contract=contract, position=-qty,
                           avgCost=avg_cost, account=account)


class _FakeIB:
    def __init__(self, positions=(), md_map=None, *, raise_positions=False):
        self._pos = list(positions)
        self._md = md_map or {}
        self._raise = raise_positions

    async def reqPositionsAsync(self):
        if self._raise:
            raise RuntimeError("simulated disconnect")
        return list(self._pos)

    def reqMarketDataType(self, t): pass
    async def qualifyContractsAsync(self, c): return [c]
    def reqMktData(self, c, *a, **kw):
        return self._md.get(c.symbol.upper(), SimpleNamespace(ask=None))
    def cancelMktData(self, c): pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast(_): return None
    monkeypatch.setattr("agt_equities.csp_harvest.asyncio.sleep", _fast)


@pytest.fixture(autouse=True)
def _mock_days_held(monkeypatch):
    monkeypatch.setattr("agt_equities.csp_harvest._lookup_days_held",
                        lambda *a, **kw: 1)


@pytest.fixture
def collector_ctx():
    return RunContext(
        mode=RunMode.SHADOW,
        run_id="shadow-test",
        order_sink=CollectorOrderSink(),
        decision_sink=NullDecisionSink(),
    )


# ---------------------------------------------------------------------------
# Test 1 — ctx is required keyword-only
# ---------------------------------------------------------------------------

def test_scan_requires_ctx_kwarg():
    """Calling without ctx raises TypeError — staging_callback is gone."""
    ib = _FakeIB()
    with pytest.raises(TypeError, match="ctx"):
        asyncio.run(scan_csp_harvest_candidates(ib))


# ---------------------------------------------------------------------------
# Test 2 — SQLiteOrderSink live path: staging_fn called with ticket list
# ---------------------------------------------------------------------------

def test_scan_live_sink_byte_identical_staging():
    """SQLiteOrderSink(staging_fn=...) receives exactly [ticket] on a qualifying put."""
    staged: list[list[dict]] = []

    def _capture(tickets):
        staged.append(list(tickets))

    ctx = RunContext(
        mode=RunMode.LIVE,
        run_id="live-test",
        order_sink=SQLiteOrderSink(staging_fn=_capture),
        decision_sink=NullDecisionSink(),
    )
    pos = _make_fake_pos(ticker="AAPL", avg_cost=100.0)
    ib = _FakeIB([pos], {"AAPL": SimpleNamespace(ask=0.15)})  # 85% profit

    result = asyncio.run(scan_csp_harvest_candidates(ib, ctx=ctx))

    assert len(result["staged"]) == 1
    assert len(staged) == 1
    assert staged[0][0]["ticker"] == "AAPL"
    assert staged[0][0]["mode"] == "CSP_HARVEST"


# ---------------------------------------------------------------------------
# Test 3 — CollectorOrderSink captures ShadowOrder
# ---------------------------------------------------------------------------

def test_scan_collector_sink_captures_shadow_orders(collector_ctx):
    """CollectorOrderSink accumulates a ShadowOrder for each staged ticket."""
    pos = _make_fake_pos(ticker="MSFT", avg_cost=200.0)
    ib = _FakeIB([pos], {"MSFT": SimpleNamespace(ask=0.30)})  # 85% profit

    result = asyncio.run(scan_csp_harvest_candidates(ib, ctx=collector_ctx))

    assert len(result["staged"]) == 1
    orders = collector_ctx.order_sink.peek()
    assert len(orders) == 1
    so = orders[0]
    assert so.engine == "csp_harvest"
    assert so.run_id == collector_ctx.run_id
    assert so.ticker == "MSFT"


# ---------------------------------------------------------------------------
# Test 4 — shadow mode writes nothing to prod DB
# ---------------------------------------------------------------------------

def test_scan_shadow_mode_writes_nothing_to_prod_db(collector_ctx, tmp_path):
    """Collector sink does not touch pending_orders in any SQLite file."""
    db_path = tmp_path / "shadow_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE pending_orders (id INTEGER PRIMARY KEY, payload TEXT)")
    conn.commit()
    conn.close()

    pos = _make_fake_pos(ticker="NVDA", avg_cost=100.0)
    ib = _FakeIB([pos], {"NVDA": SimpleNamespace(ask=0.10)})  # 90% profit

    asyncio.run(scan_csp_harvest_candidates(ib, ctx=collector_ctx))

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM pending_orders").fetchone()[0]
    conn.close()
    assert count == 0, "Shadow scan must not write to any DB"


# ---------------------------------------------------------------------------
# Test 5 — empty positions returns empty staged
# ---------------------------------------------------------------------------

def test_scan_empty_positions_returns_empty_staged(collector_ctx):
    """No positions -> staged=[], skipped=[], errors=[]."""
    ib = _FakeIB([], {})
    result = asyncio.run(scan_csp_harvest_candidates(ib, ctx=collector_ctx))
    assert result["staged"] == []
    assert result["skipped"] == []
    assert result["errors"] == []
    assert collector_ctx.order_sink.peek() == []


# ---------------------------------------------------------------------------
# Test 6 — meta keys roundtrip via CollectorOrderSink
# ---------------------------------------------------------------------------

def test_scan_meta_keys_roundtrip(collector_ctx):
    """All A3 meta keys present in the captured ShadowOrder.meta."""
    pos = _make_fake_pos(ticker="GOOGL", strike=160.0, avg_cost=160.0)
    ib = _FakeIB([pos], {"GOOGL": SimpleNamespace(ask=0.25)})  # ~84% profit

    asyncio.run(scan_csp_harvest_candidates(ib, ctx=collector_ctx))

    orders = collector_ctx.order_sink.peek()
    assert len(orders) == 1
    meta = orders[0].meta
    required_keys = (
        "account_id", "household", "ticker", "strike",
        "expiry", "quantity", "limit_price", "days_held", "v2_rationale",
    )
    for k in required_keys:
        assert k in meta, f"meta missing A3 key: {k}"


# ---------------------------------------------------------------------------
# Test 7 — shadow_scan --engine harvest is wired
# ---------------------------------------------------------------------------

def test_shadow_scan_harvest_engine_wired():
    """scripts/shadow_scan.py run_engines_stub routes 'harvest' to _run_harvest_engine."""
    from scripts.shadow_scan import run_engines_stub

    ctx = RunContext(
        mode=RunMode.SHADOW,
        run_id="wiring-test",
        order_sink=CollectorOrderSink(),
        decision_sink=NullDecisionSink(),
    )

    import io, sys
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        run_engines_stub(ctx, "harvest")

    output = buf.getvalue()
    assert "harvest" in output
    assert "shadow_scan" in output
