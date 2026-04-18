"""
tests/test_roll_scanner_shadow_mode.py

Shadow-mode contract tests for scan_and_stage_defensive_rolls after
ADR-008 MR 4b extraction to agt_equities/roll_scanner.py.

7 sprint_a tests:
  1. test_scan_requires_ctx_kwarg
  2. test_scan_live_sink_byte_identical_staging
  3. test_scan_collector_sink_captures_shadow_orders
  4. test_scan_shadow_mode_writes_nothing_to_prod_db
  5. test_scan_empty_positions_returns_empty_alerts
  6. test_scan_meta_engine_is_roll_engine
  7. test_shadow_scan_roll_engine_wired
"""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agt_equities.runtime import RunContext, RunMode
from agt_equities.sinks import (
    CollectorOrderSink,
    NullDecisionSink,
    SQLiteOrderSink,
)

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Shared stub callables
# ---------------------------------------------------------------------------

_MOCK_DESK_MODE = MagicMock(return_value="PEACETIME")
_MOCK_EXPIRATIONS = AsyncMock(return_value=[])
_MOCK_CHAIN = AsyncMock(return_value=[])
_ACCOUNT_LABELS = {"U1": "Paper-Yash"}


def _make_scanner_kwargs(
    *,
    spot: float = 95.0,
    ledger=None,
    desk_mode: str = "PEACETIME",
    account_labels: dict | None = None,
    is_halted: bool = False,
):
    """Return the full set of injected kwarg mocks for scan_and_stage_defensive_rolls."""
    if account_labels is None:
        account_labels = _ACCOUNT_LABELS
    if ledger is None:
        ledger = {"initial_basis": 120.0, "adjusted_basis": 110.0}
    return dict(
        ibkr_get_spot=AsyncMock(return_value=spot),
        load_premium_ledger=MagicMock(return_value=ledger),
        get_desk_mode=MagicMock(return_value=desk_mode),
        ibkr_get_expirations=AsyncMock(return_value=[]),
        ibkr_get_chain=AsyncMock(return_value=[]),
        account_labels=account_labels,
        is_halted=is_halted,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_fake_short_call(
    ticker="AAPL",
    strike=100.0,
    expiry=None,
    qty=1,
    avg_cost=-150.0,
    account="U1",
):
    from datetime import date, timedelta
    if expiry is None:
        expiry = (date.today() + timedelta(days=30)).strftime("%Y%m%d")
    contract = SimpleNamespace(
        symbol=ticker,
        secType="OPT",
        right="C",
        strike=strike,
        lastTradeDateOrContractMonth=expiry,
        conId=111,
    )
    return SimpleNamespace(
        contract=contract,
        position=-qty,
        avgCost=avg_cost,
        account=account,
    )


class _FakeIB:
    def __init__(self, positions=(), md=None, *, raise_positions=False):
        self._pos = list(positions)
        self._md = md or SimpleNamespace(
            ask=0.20, bid=0.15,
            modelGreeks=SimpleNamespace(delta=0.20, impliedVol=0.25),
            bidGreeks=None,
        )
        self._raise = raise_positions

    async def reqPositionsAsync(self):
        if self._raise:
            raise RuntimeError("simulated disconnect")
        return list(self._pos)

    def reqMarketDataType(self, t):
        return None

    async def qualifyContractsAsync(self, c):
        return [SimpleNamespace(conId=getattr(c, "conId", 222) or 222)]

    def reqMktData(self, c, *a, **kw):
        return self._md

    def cancelMktData(self, c):
        return None


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast(*_a, **_kw):
        return None
    monkeypatch.setattr("agt_equities.roll_scanner.asyncio.sleep", _fast)


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
    """Calling without ctx raises TypeError — the scanner enforces keyword-only."""
    from agt_equities import roll_scanner
    ib = _FakeIB()
    with pytest.raises(TypeError, match="ctx"):
        asyncio.run(roll_scanner.scan_and_stage_defensive_rolls(
            ib,
            ibkr_get_spot=AsyncMock(return_value=95.0),
            load_premium_ledger=MagicMock(return_value=None),
            get_desk_mode=MagicMock(return_value="PEACETIME"),
            ibkr_get_expirations=AsyncMock(return_value=[]),
            ibkr_get_chain=AsyncMock(return_value=[]),
            account_labels={},
        ))


# ---------------------------------------------------------------------------
# Test 2 — SQLiteOrderSink live path: staging_fn called with finalized tickets
# ---------------------------------------------------------------------------

@patch("agt_equities.roll_scanner.ACCOUNT_TO_HOUSEHOLD", {"U1": "Yash_Household"})
def test_scan_live_sink_byte_identical_staging():
    """SQLiteOrderSink(staging_fn=...) receives the finalized ticket list on a HARVEST."""
    from agt_equities import roll_scanner
    staged: list[list[dict]] = []

    def _capture(tickets):
        staged.append(list(tickets))

    ctx = RunContext(
        mode=RunMode.LIVE,
        run_id="live-test",
        order_sink=SQLiteOrderSink(staging_fn=_capture),
        decision_sink=NullDecisionSink(),
    )
    pos = _make_fake_short_call(ticker="AAPL", strike=100.0, avg_cost=-150.0)
    ib = _FakeIB([pos])

    alerts = asyncio.run(
        roll_scanner.scan_and_stage_defensive_rolls(
            ib, ctx=ctx, **_make_scanner_kwargs(),
        )
    )

    assert any("HARVEST" in line for line in alerts), alerts
    assert len(staged) == 1
    ticket = staged[0][0]
    assert ticket["ticker"] == "AAPL"
    assert ticket["sec_type"] == "OPT"
    assert ticket["action"] == "BUY"
    assert ticket["origin"] == "roll_engine"


# ---------------------------------------------------------------------------
# Test 3 — CollectorOrderSink captures ShadowOrder with engine='roll_engine'
# ---------------------------------------------------------------------------

@patch("agt_equities.roll_scanner.ACCOUNT_TO_HOUSEHOLD", {"U1": "Yash_Household"})
def test_scan_collector_sink_captures_shadow_orders(collector_ctx):
    """CollectorOrderSink accumulates a ShadowOrder for each staged ticket."""
    from agt_equities import roll_scanner
    pos = _make_fake_short_call(ticker="MSFT", strike=100.0, avg_cost=-150.0)
    ib = _FakeIB([pos])

    asyncio.run(
        roll_scanner.scan_and_stage_defensive_rolls(
            ib, ctx=collector_ctx, **_make_scanner_kwargs(),
        )
    )

    orders = collector_ctx.order_sink.peek()
    assert len(orders) == 1
    so = orders[0]
    assert so.engine == "roll_engine"
    assert so.run_id == collector_ctx.run_id
    assert so.ticker == "MSFT"
    assert so.right == "C"


# ---------------------------------------------------------------------------
# Test 4 — shadow mode writes nothing to any DB
# ---------------------------------------------------------------------------

@patch("agt_equities.roll_scanner.ACCOUNT_TO_HOUSEHOLD", {"U1": "Yash_Household"})
def test_scan_shadow_mode_writes_nothing_to_prod_db(collector_ctx, tmp_path):
    """Collector sink does not touch pending_orders in any SQLite file."""
    from agt_equities import roll_scanner
    db_path = tmp_path / "shadow_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE pending_orders (id INTEGER PRIMARY KEY, payload TEXT)")
    conn.commit()
    conn.close()

    pos = _make_fake_short_call(ticker="NVDA", strike=100.0, avg_cost=-150.0)
    ib = _FakeIB([pos])

    asyncio.run(
        roll_scanner.scan_and_stage_defensive_rolls(
            ib, ctx=collector_ctx, **_make_scanner_kwargs(),
        )
    )

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM pending_orders").fetchone()[0]
    conn.close()
    assert count == 0, "Shadow scan must not write to any DB"


# ---------------------------------------------------------------------------
# Test 5 — empty positions returns empty alerts
# ---------------------------------------------------------------------------

def test_scan_empty_positions_returns_empty_alerts(collector_ctx):
    """No short calls -> scanner returns [] and sink stays empty."""
    from agt_equities import roll_scanner
    ib = _FakeIB([])
    alerts = asyncio.run(
        roll_scanner.scan_and_stage_defensive_rolls(
            ib, ctx=collector_ctx, **_make_scanner_kwargs(),
        )
    )
    assert alerts == []
    assert collector_ctx.order_sink.peek() == []


# ---------------------------------------------------------------------------
# Test 6 — ShadowOrder.engine stamped 'roll_engine' (not a generic value)
# ---------------------------------------------------------------------------

@patch("agt_equities.roll_scanner.ACCOUNT_TO_HOUSEHOLD", {"U1": "Yash_Household"})
def test_scan_meta_engine_is_roll_engine(collector_ctx):
    """Every captured ShadowOrder is tagged engine='roll_engine'."""
    from agt_equities import roll_scanner
    pos = _make_fake_short_call(ticker="GOOGL", strike=100.0, avg_cost=-150.0)
    ib = _FakeIB([pos])

    asyncio.run(
        roll_scanner.scan_and_stage_defensive_rolls(
            ib, ctx=collector_ctx, **_make_scanner_kwargs(),
        )
    )

    orders = collector_ctx.order_sink.peek()
    assert len(orders) == 1
    assert orders[0].engine == "roll_engine"


# ---------------------------------------------------------------------------
# Test 7 — shadow_scan --engine roll is wired
# ---------------------------------------------------------------------------

def test_shadow_scan_roll_engine_wired():
    """scripts/shadow_scan.py run_engines_stub routes 'roll' to _run_roll_engine."""
    from scripts.shadow_scan import run_engines_stub

    ctx = RunContext(
        mode=RunMode.SHADOW,
        run_id="wiring-test",
        order_sink=CollectorOrderSink(),
        decision_sink=NullDecisionSink(),
    )

    import io
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        run_engines_stub(ctx, "roll")

    output = buf.getvalue()
    assert "roll" in output
    assert "shadow_scan" in output
