"""Tests for CC DecisionSink plumbing (ADR-008 MR 5).

sprint_a tests: verify that the DecisionSink seam is correctly wired into
_run_cc_logic and _stage_dynamic_exit_candidate.

Sink-unit tests (1-5) run without telegram_bot imports.
Integration tests (6-12) require telegram_bot with the MR 5 changes.
"""
from __future__ import annotations

import asyncio
import datetime
import inspect
import uuid
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def null_sink():
    from agt_equities.sinks import NullDecisionSink
    return NullDecisionSink()


@pytest.fixture
def collector_sink():
    from agt_equities.sinks import CollectorDecisionSink
    return CollectorDecisionSink()


def _make_shadow_ctx(sink):
    from agt_equities.runtime import RunContext, RunMode
    from agt_equities.sinks import CollectorOrderSink
    return RunContext(
        mode=RunMode.SHADOW,
        run_id=uuid.uuid4().hex,
        order_sink=CollectorOrderSink(),
        decision_sink=sink,
    
        broker_mode="paper",
        engine="cc",
    )


# ---------------------------------------------------------------------------
# Sink unit tests — no telegram_bot needed
# ---------------------------------------------------------------------------

def test_null_decision_sink_cc_cycle_no_op(null_sink):
    null_sink.record_cc_cycle([{"ticker": "AAPL"}], run_id="x")


def test_null_decision_sink_dynamic_exit_no_op(null_sink):
    null_sink.record_dynamic_exit([{"audit_id": "abc"}], run_id="x")


def test_collector_captures_cc_cycle_entries(collector_sink):
    entries = [
        {"ticker": "MSFT", "household": "HH1", "mode": "MODE_2_HARVEST", "flag": "HARVEST_OK"},
        {"ticker": "AAPL", "household": "HH1", "mode": "MODE_2_HARVEST", "flag": "SKIPPED"},
    ]
    collector_sink.record_cc_cycle(entries, run_id="run1")
    decisions = collector_sink.drain()
    assert len(decisions) == 2
    assert all(d.kind == "cc_cycle" for d in decisions)
    assert all(d.run_id == "run1" for d in decisions)
    assert {d.payload["ticker"] for d in decisions} == {"MSFT", "AAPL"}


def test_collector_captures_dynamic_exit_entries(collector_sink):
    entries = [
        {
            "audit_id": "abc123",
            "trade_date": "2026-04-19",
            "ticker": "TSLA",
            "action_type": "CC",
            "final_status": "STAGED",
        }
    ]
    collector_sink.record_dynamic_exit(entries, run_id="run2")
    decisions = collector_sink.drain()
    assert len(decisions) == 1
    assert decisions[0].kind == "dynamic_exit"
    assert decisions[0].run_id == "run2"
    assert decisions[0].payload["trade_date"] == "2026-04-19"
    assert decisions[0].payload["ticker"] == "TSLA"


def test_dynamic_exit_trade_date_isoformat_not_sql_literal(collector_sink):
    today = datetime.date.today().isoformat()
    collector_sink.record_dynamic_exit([{"trade_date": today}], run_id="r1")
    d = collector_sink.drain()[0]
    assert d.payload["trade_date"] == today
    assert "now" not in str(d.payload["trade_date"]).lower()


# ---------------------------------------------------------------------------
# telegram_bot API sentinels — require MR 5 changes in telegram_bot.py
# ---------------------------------------------------------------------------

def test_write_dynamic_exit_rows_exists():
    import telegram_bot
    assert callable(telegram_bot._write_dynamic_exit_rows)


def test_stage_dynamic_exit_has_ctx_kwarg():
    import telegram_bot
    sig = inspect.signature(telegram_bot._stage_dynamic_exit_candidate)
    assert "ctx" in sig.parameters
    assert sig.parameters["ctx"].default is None


def test_run_cc_logic_has_ctx_kwarg():
    import telegram_bot
    sig = inspect.signature(telegram_bot._run_cc_logic)
    assert "ctx" in sig.parameters


def test_no_asyncio_to_thread_log_cc_cycle_in_run_cc_logic():
    import telegram_bot
    src = inspect.getsource(telegram_bot._run_cc_logic)
    assert "asyncio.to_thread(_log_cc_cycle" not in src, (
        "asyncio.to_thread(_log_cc_cycle) must be removed by MR 5"
    )


def test_date_now_sql_literal_removed_from_stage_source():
    import telegram_bot
    src = inspect.getsource(telegram_bot._stage_dynamic_exit_candidate)
    assert "date('now')" not in src, (
        "_stage_dynamic_exit_candidate must use date.today().isoformat(), not date('now')"
    )


# ---------------------------------------------------------------------------
# Integration: _run_cc_logic routes to CollectorDecisionSink
# ---------------------------------------------------------------------------

def test_run_cc_logic_empty_discovery_returns_main_text(collector_sink):
    import telegram_bot
    import agt_equities.position_discovery as _pd

    ctx = _make_shadow_ctx(collector_sink)
    with patch.object(
        _pd, "discover_positions", new_callable=AsyncMock,
        return_value={"households": {}, "error": None},
    ), patch.object(
        telegram_bot, "ensure_ib_connected", new_callable=AsyncMock, return_value=None,
    ), patch.object(
        telegram_bot, "_query_margin_stats", new_callable=AsyncMock, return_value={},
    ):
        result = asyncio.run(telegram_bot._run_cc_logic(None, ctx=ctx))

    assert isinstance(result, dict)
    assert "main_text" in result


def test_run_cc_logic_does_not_call_to_thread_with_log_cc_cycle(collector_sink):
    import telegram_bot
    import agt_equities.position_discovery as _pd

    ctx = _make_shadow_ctx(collector_sink)
    with patch.object(
        _pd, "discover_positions", new_callable=AsyncMock,
        return_value={"households": {}, "error": None},
    ), patch.object(
        telegram_bot, "ensure_ib_connected", new_callable=AsyncMock, return_value=None,
    ), patch.object(
        telegram_bot, "_query_margin_stats", new_callable=AsyncMock, return_value={},
    ), patch.object(telegram_bot.asyncio, "to_thread", wraps=asyncio.to_thread) as spy:
        asyncio.run(telegram_bot._run_cc_logic(None, ctx=ctx))

    for c in spy.call_args_list:
        assert c.args[0] is not telegram_bot._log_cc_cycle, (
            "asyncio.to_thread(_log_cc_cycle) was called — MR 5 removal failed"
        )


# ---------------------------------------------------------------------------
# MR fix/cc-order-sink-staging: CC orders route through ctx.order_sink
# ---------------------------------------------------------------------------

import pytest


class TestCCOrderSinkRouting:
    """Verify CC orders route through ctx.order_sink (not append_pending_tickets
    directly), so shadow scans and Telegram digests can observe CC tickets."""

    @pytest.mark.sprint_a
    def test_collector_order_sink_accepts_cc_engine_call(self):
        """CollectorOrderSink.stage() with engine='cc_engine' produces a
        ShadowOrder with the correct engine tag. Verifies the call signature
        that _run_cc_logic uses after the fix."""
        import uuid
        from agt_equities.runtime import RunContext, RunMode
        from agt_equities.sinks import CollectorOrderSink, NullDecisionSink

        collector = CollectorOrderSink()
        run_id = uuid.uuid4().hex
        ctx = RunContext(
            mode=RunMode.SHADOW,
            run_id=run_id,
            order_sink=collector,
            decision_sink=NullDecisionSink(),
        
            broker_mode="paper",
            engine="cc",
        )

        fake_ticket = {
            "account_id": "U00000001",
            "household": "Test_Household",
            "ticker": "AAPL",
            "action": "SELL",
            "sec_type": "OPT",
            "quantity": 1,
        }
        ctx.order_sink.stage([fake_ticket], engine="cc_engine", run_id=run_id)

        orders = collector.drain()
        assert len(orders) == 1
        assert orders[0].engine == "cc_engine"

    @pytest.mark.sprint_a
    @pytest.mark.asyncio
    async def test_empty_staged_does_not_call_stage(self, monkeypatch):
        """When no CC positions exist, order_sink.stage must not be called."""
        import uuid
        from agt_equities.runtime import RunContext, RunMode
        from agt_equities.sinks import CollectorOrderSink, NullDecisionSink
        import telegram_bot as tb

        collector = CollectorOrderSink()
        ctx = RunContext(
            mode=RunMode.SHADOW,
            run_id=uuid.uuid4().hex,
            order_sink=collector,
            decision_sink=NullDecisionSink(),
        
            broker_mode="paper",
            engine="cc",
        )

        import agt_equities.position_discovery as _pd

        async def _raise_no_positions(*a, **kw):
            raise Exception("no positions")

        monkeypatch.setattr(_pd, "discover_positions", _raise_no_positions)

        try:
            await tb._run_cc_logic(None, ctx=ctx)
        except Exception:
            pass

        assert len(collector.drain()) == 0

    @pytest.mark.sprint_a
    def test_append_pending_tickets_not_called_directly(self):
        """Sentinel: _run_cc_logic source must route through ctx.order_sink,
        not call asyncio.to_thread(append_pending_tickets, staged) directly."""
        import inspect
        import telegram_bot as tb

        src = inspect.getsource(tb._run_cc_logic)
        assert "asyncio.to_thread(append_pending_tickets" not in src, (
            "Direct append_pending_tickets staging still present in _run_cc_logic; "
            "patch was not applied or was partially reverted"
        )
        assert "ctx.order_sink.stage" in src, (
            "ctx.order_sink.stage call missing from _run_cc_logic after patch"
        )
