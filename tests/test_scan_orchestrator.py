"""Tests for agt_equities.scan_orchestrator two-phase pipeline.

Covers:
- ScanTrigger enum shape
- build_scan_inputs returns ScanInputs with expected fields
- allocate_candidates on empty tuple returns empty AllocatorResult
- run_csp_scan paper: approval_dispatcher NOT called, all candidates pass
- run_csp_scan live+csp: approval_dispatcher called, only approved indices used
- run_csp_scan live+csp without dispatcher: raises ValueError (fail-closed)
- run_csp_scan empty candidate universe: short-circuits, no allocator call

Tripwire-compliant (uses /__agt_test_tripwire_no_prod_db__/ auto DB path).
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agt_equities.runtime import RunContext, RunMode
from agt_equities.scan_orchestrator import (
    ScanInputs,
    ScanResult,
    ScanTrigger,
    allocate_candidates,
    build_scan_inputs,
    run_csp_scan,
)
from agt_equities.sinks import CollectorOrderSink, NullDecisionSink


pytestmark = pytest.mark.sprint_a


def _make_ctx(broker_mode: str = "paper", engine: str = "csp") -> RunContext:
    return RunContext(
        mode=RunMode.LIVE,
        run_id=uuid.uuid4().hex,
        order_sink=CollectorOrderSink(),
        decision_sink=NullDecisionSink(),
        broker_mode=broker_mode,
        engine=engine,
    )


def _make_candidate(ticker: str, strike: float, idx: int) -> MagicMock:
    c = MagicMock()
    c.ticker = ticker
    c.strike = strike
    c.expiry = "2026-05-15"
    c.mid = 1.25
    c.annualized_yield = 0.18
    c.household_id = f"HH{idx}"
    return c


def test_scan_trigger_enum_shape():
    assert ScanTrigger.ADHOC.value == "adhoc"
    assert ScanTrigger.DAILY_AUTO.value == "daily_auto"
    assert ScanTrigger.DRYRUN.value == "dryrun"
    # Enum is str-based — can be used in string formatting
    assert f"{ScanTrigger.ADHOC}" == "ScanTrigger.ADHOC"


def test_allocate_candidates_empty_returns_empty_result():
    ctx = _make_ctx()
    inputs = ScanInputs(
        trigger=ScanTrigger.DRYRUN,
        candidates=(),
        snapshots={},
        vix=20.0,
        extras_provider=lambda *_a, **_k: {},
        watchlist=[],
        run_id="test-run",
    )
    result = allocate_candidates((), inputs, ctx)
    assert result.staged == []
    assert result.skipped == []
    assert result.errors == []
    assert result.digest_lines == []


def test_run_csp_scan_paper_auto_approves_all(monkeypatch):
    """Paper broker_mode: approval_dispatcher must NOT be called."""
    ctx = _make_ctx(broker_mode="paper", engine="csp")

    candidates = tuple(_make_candidate(f"TKR{i}", 100.0 + i, i) for i in range(3))
    fake_inputs = ScanInputs(
        trigger=ScanTrigger.ADHOC,
        candidates=candidates,
        snapshots={"HH0": {"nlv": 100_000}},
        vix=18.5,
        extras_provider=lambda *_a, **_k: {},
        watchlist=[],
        run_id="test-paper",
    )

    async def fake_build(trigger, ctx, *, ib_conn, margin_stats=None):
        return fake_inputs

    dispatcher = AsyncMock()  # Must not be called

    fake_allocation = MagicMock()
    fake_allocation.total_staged_contracts = 3
    fake_allocation.digest_lines = ["line1"]

    with patch("agt_equities.scan_orchestrator.build_scan_inputs", side_effect=fake_build), \
         patch("agt_equities.scan_orchestrator.allocate_candidates", return_value=fake_allocation) as alloc:
        result = asyncio.run(run_csp_scan(
            ScanTrigger.ADHOC, ctx,
            ib_conn=MagicMock(),
            approval_dispatcher=dispatcher.__call__,
        ))

    dispatcher.assert_not_called()
    assert result.approval_applied is False
    assert result.approved_count == 3
    # allocate_candidates called with the full tuple (auto-approve)
    alloc.assert_called_once()
    called_approved = alloc.call_args.args[0]
    assert called_approved == candidates


def test_run_csp_scan_live_csp_invokes_dispatcher(monkeypatch):
    """Live broker_mode + csp engine: dispatcher called; only approved indices used."""
    ctx = _make_ctx(broker_mode="live", engine="csp")

    candidates = tuple(_make_candidate(f"TKR{i}", 100.0 + i, i) for i in range(4))
    fake_inputs = ScanInputs(
        trigger=ScanTrigger.DAILY_AUTO,
        candidates=candidates,
        snapshots={},
        vix=20.0,
        extras_provider=lambda *_a, **_k: {},
        watchlist=[],
        run_id="test-live",
    )

    async def fake_build(trigger, ctx, *, ib_conn, margin_stats=None):
        return fake_inputs

    async def fake_dispatcher(cands, ctx):
        # Approve indices 0 and 2 (skip 1 and 3) — tests ordering preservation
        return frozenset({0, 2})

    fake_allocation = MagicMock()
    fake_allocation.total_staged_contracts = 2
    fake_allocation.digest_lines = []

    with patch("agt_equities.scan_orchestrator.build_scan_inputs", side_effect=fake_build), \
         patch("agt_equities.scan_orchestrator.allocate_candidates", return_value=fake_allocation) as alloc:
        result = asyncio.run(run_csp_scan(
            ScanTrigger.DAILY_AUTO, ctx,
            ib_conn=MagicMock(),
            approval_dispatcher=fake_dispatcher,
        ))

    assert result.approval_applied is True
    assert result.approved_count == 2
    called_approved = alloc.call_args.args[0]
    # Order preserved — indices 0 and 2 from the original tuple
    assert called_approved == (candidates[0], candidates[2])


def test_run_csp_scan_live_csp_without_dispatcher_raises():
    """Fail-closed: no dispatcher on live-CSP must NOT ship un-approved orders."""
    ctx = _make_ctx(broker_mode="live", engine="csp")

    candidates = tuple(_make_candidate("TKR0", 100.0, 0) for _ in range(1))
    fake_inputs = ScanInputs(
        trigger=ScanTrigger.ADHOC,
        candidates=candidates,
        snapshots={},
        vix=20.0,
        extras_provider=lambda *_a, **_k: {},
        watchlist=[],
        run_id="test-fail-closed",
    )

    async def fake_build(trigger, ctx, *, ib_conn, margin_stats=None):
        return fake_inputs

    with patch("agt_equities.scan_orchestrator.build_scan_inputs", side_effect=fake_build):
        with pytest.raises(ValueError, match="approval_dispatcher"):
            asyncio.run(run_csp_scan(
                ScanTrigger.ADHOC, ctx,
                ib_conn=MagicMock(),
                approval_dispatcher=None,
            ))


def test_run_csp_scan_empty_universe_short_circuits():
    """Empty candidate universe: no allocator call, no approval call."""
    ctx = _make_ctx(broker_mode="live", engine="csp")

    fake_inputs = ScanInputs(
        trigger=ScanTrigger.ADHOC,
        candidates=(),
        snapshots={},
        vix=0.0,
        extras_provider=lambda *_a, **_k: {},
        watchlist=[],
        run_id="test-empty",
    )

    async def fake_build(trigger, ctx, *, ib_conn, margin_stats=None):
        return fake_inputs

    dispatcher = AsyncMock()

    with patch("agt_equities.scan_orchestrator.build_scan_inputs", side_effect=fake_build):
        result = asyncio.run(run_csp_scan(
            ScanTrigger.ADHOC, ctx,
            ib_conn=MagicMock(),
            approval_dispatcher=dispatcher.__call__,
        ))

    dispatcher.assert_not_called()
    assert result.approved_count == 0
    assert result.approval_applied is False
    assert result.allocation.staged == []


def test_run_csp_scan_live_non_csp_engine_skips_approval():
    """Live + engine='cc' (hypothetical): approval NOT required per policy."""
    ctx = _make_ctx(broker_mode="live", engine="cc")

    candidates = tuple(_make_candidate(f"TKR{i}", 100.0, i) for i in range(2))
    fake_inputs = ScanInputs(
        trigger=ScanTrigger.ADHOC,
        candidates=candidates,
        snapshots={},
        vix=20.0,
        extras_provider=lambda *_a, **_k: {},
        watchlist=[],
        run_id="test-non-csp",
    )

    async def fake_build(trigger, ctx, *, ib_conn, margin_stats=None):
        return fake_inputs

    dispatcher = AsyncMock()

    fake_allocation = MagicMock()
    fake_allocation.total_staged_contracts = 2
    fake_allocation.digest_lines = []

    with patch("agt_equities.scan_orchestrator.build_scan_inputs", side_effect=fake_build), \
         patch("agt_equities.scan_orchestrator.allocate_candidates", return_value=fake_allocation):
        result = asyncio.run(run_csp_scan(
            ScanTrigger.ADHOC, ctx,
            ib_conn=MagicMock(),
            approval_dispatcher=dispatcher.__call__,
        ))

    dispatcher.assert_not_called()
    assert result.approval_applied is False
    assert result.approved_count == 2
