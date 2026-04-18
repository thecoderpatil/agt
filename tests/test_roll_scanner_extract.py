"""
tests/test_roll_scanner_extract.py

Extraction contract tests (Stream A2, MR 4b):
  1. Import smoke: roll_scanner is importable and scan_and_stage_defensive_rolls is callable.
  2. Standalone isolation: scan_and_stage_defensive_rolls runs with all 5 deps stubbed.
  3. Identity check: _scan_and_stage_defensive_rolls is NOT present in telegram_bot.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Test 1 — Import smoke
# ---------------------------------------------------------------------------

def test_roll_scanner_import_smoke():
    """roll_scanner is importable and exposes the public scanner function."""
    from agt_equities import roll_scanner
    assert callable(roll_scanner.scan_and_stage_defensive_rolls)


# ---------------------------------------------------------------------------
# Test 2 — Standalone isolation: all 5 callables injected
# ---------------------------------------------------------------------------

def test_roll_scanner_standalone_isolation():
    """scan_and_stage_defensive_rolls runs with all deps as MagicMock stubs."""
    from agt_equities import roll_scanner
    from agt_equities.runtime import RunContext, RunMode
    from agt_equities.sinks import CollectorOrderSink, NullDecisionSink

    class _FakeIB:
        async def reqPositionsAsync(self):
            return []
        def reqMarketDataType(self, t): return None
        async def qualifyContractsAsync(self, c): return []
        def reqMktData(self, c, *a, **kw): return SimpleNamespace()
        def cancelMktData(self, c): return None

    ctx = RunContext(
        mode=RunMode.SHADOW,
        run_id="extract-iso-test",
        order_sink=CollectorOrderSink(),
        decision_sink=NullDecisionSink(),
    )

    result = asyncio.run(
        roll_scanner.scan_and_stage_defensive_rolls(
            _FakeIB(),
            ctx=ctx,
            ibkr_get_spot=AsyncMock(return_value=100.0),
            load_premium_ledger=MagicMock(return_value=None),
            get_desk_mode=MagicMock(return_value="PEACETIME"),
            ibkr_get_expirations=AsyncMock(return_value=[]),
            ibkr_get_chain=AsyncMock(return_value=[]),
            account_labels={},
            is_halted=False,
        )
    )
    # Empty positions → empty alerts
    assert result == []


# ---------------------------------------------------------------------------
# Test 3 — Identity check: old name is gone from telegram_bot
# ---------------------------------------------------------------------------

def test_telegram_bot_scan_name_removed():
    """_scan_and_stage_defensive_rolls must NOT be importable from telegram_bot."""
    import telegram_bot
    assert not hasattr(telegram_bot, "_scan_and_stage_defensive_rolls"), (
        "_scan_and_stage_defensive_rolls must be removed from telegram_bot "
        "after extraction to roll_scanner"
    )
