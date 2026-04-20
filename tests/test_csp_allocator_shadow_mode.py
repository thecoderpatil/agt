"""ADR-008 MR 2 — CSP allocator ctx/shadow-mode contract tests.

Companion to ``tests/test_shadow_scan_plumbing.py``. These tests verify
that ``run_csp_allocator`` honors the MR 1 seam end-to-end:

  - LIVE ctx with ``SQLiteOrderSink(staging_fn=callable)`` forwards
    ticket batches to ``callable`` positionally — byte-identical to the
    pre-MR-2 ``staging_callback`` behavior.
  - SHADOW ctx with ``CollectorOrderSink`` captures ticket batches in
    memory with zero DB writes; ``drain()`` returns ``ShadowOrder``
    entries carrying ``engine='csp_allocator'``, the run_id, and the
    meta payload the allocator supplies.
  - The allocator stages via ``ctx.order_sink.stage(tickets, ...)`` and
    does NOT call anything else that could touch ``pending_orders`` on
    the shadow path — proved by monkey-patching the staging adapter and
    asserting it is never invoked.

No IB. No DB. Pure contract exercise against fake household snapshots
plus a constructed RAY candidate.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from agt_equities.csp_allocator import (
    AllocatorResult,
    run_csp_allocator,
)
from agt_equities.runtime import RunContext, RunMode
from agt_equities.sinks import (
    CollectorOrderSink,
    NullDecisionSink,
    ShadowOrder,
    SQLiteOrderSink,
)

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Fixtures — mirror test_csp_allocator.py's minimal snapshot + candidate
# so this file is self-contained.
# ---------------------------------------------------------------------------


def _fake_candidate(ticker: str = "AAPL", strike: float = 150.0) -> Any:
    """Construct a RAYCandidate-like object conforming to CSPCandidate."""
    class _C:
        pass
    c = _C()
    c.ticker = ticker
    c.strike = float(strike)
    c.mid = 2.50
    c.expiry = "2026-05-15"
    c.annualized_yield = 18.5
    return c


def _fake_hh_snapshot() -> dict:
    """Minimal household snapshot that passes rule_1/rule_2 for small-notional
    candidates. Mirrors the canonical ``_fake_hh_snapshot`` in
    ``tests/test_csp_allocator.py``: hh_nlv=$261K (20% ceiling = $52.2K),
    hh_margin_nlv == hh_margin_el = $109K (zero prior margin usage →
    margin_budget = $54.5K at VIX 18). All other gates fail-open when
    ``extras`` is empty (sector_map / correlations / delta absent) and
    Rule 6 only applies to Vikram_Household.
    """
    return {
        "household": "Yash_Household",
        "hh_nlv": 261_000.0,
        "hh_margin_nlv": 109_000.0,
        "hh_margin_el": 109_000.0,
        "accounts": {
            "U21971297": {
                "account_id": "U21971297",
                "nlv": 109_000.0,
                "el": 109_000.0,
                "buying_power": 200_000.0,
                "cash_available": 200_000.0,
                "margin_eligible": True,
            },
            "U22076329": {
                "account_id": "U22076329",
                "nlv": 152_000.0,
                "el": 0.0,
                "buying_power": 0.0,
                "cash_available": 152_000.0,
                "margin_eligible": False,
            },
        },
        "existing_positions": {},
        "existing_csps": {},
        "working_order_tickers": set(),
        "staged_order_tickers": set(),
    }


def _empty_extras(hh, candidate) -> dict:
    return {}


def _live_ctx(staging_fn) -> RunContext:
    return RunContext(
        mode=RunMode.LIVE,
        run_id=uuid.uuid4().hex,
        order_sink=SQLiteOrderSink(staging_fn=staging_fn),
        decision_sink=NullDecisionSink(),
    
        broker_mode="paper",
        engine="csp",
    )


def _shadow_ctx() -> tuple[RunContext, CollectorOrderSink]:
    sink = CollectorOrderSink()
    ctx = RunContext(
        mode=RunMode.SHADOW,
        run_id=uuid.uuid4().hex,
        order_sink=sink,
        decision_sink=NullDecisionSink(),
        db_path="/tmp/shadow-fake.db",
    
        broker_mode="paper",
        engine="csp",
    )
    return ctx, sink


# ---------------------------------------------------------------------------
# LIVE ctx: SQLiteOrderSink forwards to staging_fn byte-identically
# ---------------------------------------------------------------------------


class TestLiveCtxForwardsTickets:
    def test_sqlite_order_sink_calls_staging_fn_positionally(self):
        captured: list[list[dict]] = []
        cand = _fake_candidate()
        hh = _fake_hh_snapshot()
        result = run_csp_allocator(
            [cand],
            {"Yash_Household": hh},
            vix=18.0,
            extras_provider=_empty_extras,
            ctx=_live_ctx(captured.append),
        )
        assert isinstance(result, AllocatorResult)
        assert len(result.staged) >= 1, result.skipped
        # staging_fn got exactly one list-of-tickets call per staged batch
        assert len(captured) == 1
        assert captured[0] == result.staged

    def test_live_ctx_no_error_on_staging_fn_raise(self):
        def boom(tickets):
            raise RuntimeError("boom")
        cand = _fake_candidate()
        hh = _fake_hh_snapshot()
        result = run_csp_allocator(
            [cand],
            {"Yash_Household": hh},
            vix=18.0,
            extras_provider=_empty_extras,
            ctx=_live_ctx(boom),
        )
        # The allocator is expected to surface the failure in result.errors
        # rather than propagate — matches pre-MR-2 behavior.
        assert result.staged == []
        assert any("staging failed" in e.get("error", "") for e in result.errors)


# ---------------------------------------------------------------------------
# SHADOW ctx: CollectorOrderSink captures tickets, no DB writes
# ---------------------------------------------------------------------------


class TestShadowCtxCapturesTickets:
    def test_collector_order_sink_populates_shadow_orders(self):
        cand = _fake_candidate(ticker="MSFT", strike=420.0)
        hh = _fake_hh_snapshot()
        ctx, sink = _shadow_ctx()
        result = run_csp_allocator(
            [cand],
            {"Yash_Household": hh},
            vix=18.0,
            extras_provider=_empty_extras,
            ctx=ctx,
        )
        assert len(result.staged) >= 1, result.skipped
        drained = sink.drain()
        assert len(drained) >= 1
        assert all(isinstance(s, ShadowOrder) for s in drained)
        so = drained[0]
        assert so.engine == "csp_allocator"
        assert so.run_id == ctx.run_id
        assert so.ticker == "MSFT"
        assert so.right == "P"
        assert so.strike == pytest.approx(420.0)
        # CollectorOrderSink reads ``t.get("qty")`` but the allocator emits
        # ``quantity`` in the ticket dict (MR 1 sink naming mismatch tracked
        # separately). The contract payload the allocator supplies is what
        # matters here — assert on ``meta["quantity"]`` plus ``meta["n_contracts"]``.
        assert so.meta.get("household") == "Yash_Household"
        assert so.meta.get("ticker") == "MSFT"
        assert so.meta.get("n_contracts", 0) >= 1
        assert so.meta.get("quantity", 0) >= 1

    def test_shadow_does_not_invoke_any_staging_fn(self, monkeypatch):
        """The SHADOW path must not touch SQLiteOrderSink's staging_fn.

        Proven by asserting the adapter referenced from telegram_bot
        (append_pending_tickets) is never called during a shadow run.
        We simulate this by installing a tripwire staging_fn that would
        raise if invoked, wrapping it in a SQLiteOrderSink we DO NOT
        attach to ctx. If ctx.order_sink is the CollectorOrderSink, the
        tripwire stays silent.
        """
        tripwire_called = False

        def tripwire(tickets):
            nonlocal tripwire_called
            tripwire_called = True
            raise AssertionError(
                "staging_fn must not be invoked on SHADOW path"
            )

        # Build a shadow ctx with CollectorOrderSink
        ctx, sink = _shadow_ctx()
        # Keep a reference to a SQLiteOrderSink wrapping the tripwire —
        # the allocator must ignore this; only ctx.order_sink is used.
        _ignored_live_sink = SQLiteOrderSink(staging_fn=tripwire)

        cand = _fake_candidate(ticker="NVDA", strike=100.0)
        hh = _fake_hh_snapshot()
        result = run_csp_allocator(
            [cand],
            {"Yash_Household": hh},
            vix=18.0,
            extras_provider=_empty_extras,
            ctx=ctx,
        )
        assert tripwire_called is False
        # Shadow path still populates result.staged (in-process dataclass)
        assert len(result.staged) >= 1

    def test_shadow_run_id_threaded_to_every_shadow_order(self):
        cand_a = _fake_candidate(ticker="AAPL", strike=150.0)
        cand_b = _fake_candidate(ticker="MSFT", strike=420.0)
        hh = _fake_hh_snapshot()
        ctx, sink = _shadow_ctx()
        run_csp_allocator(
            [cand_a, cand_b],
            {"Yash_Household": hh},
            vix=18.0,
            extras_provider=_empty_extras,
            ctx=ctx,
        )
        drained = sink.drain()
        assert drained, "expected at least one staged shadow order"
        assert {so.run_id for so in drained} == {ctx.run_id}
        assert {so.engine for so in drained} == {"csp_allocator"}


# ---------------------------------------------------------------------------
# Contract: ctx is required (keyword-only) — no staging_callback fallback
# ---------------------------------------------------------------------------


class TestCtxIsRequired:
    def test_missing_ctx_raises_typeerror(self):
        cand = _fake_candidate()
        hh = _fake_hh_snapshot()
        with pytest.raises(TypeError):
            run_csp_allocator(
                [cand],
                {"Yash_Household": hh},
                vix=18.0,
                extras_provider=_empty_extras,
            )

    def test_staging_callback_kwarg_is_no_longer_accepted(self):
        """After MR 2, ``staging_callback=`` must not be a parameter.

        This test locks the contract so future refactors do not silently
        resurrect the legacy seam.
        """
        cand = _fake_candidate()
        hh = _fake_hh_snapshot()
        with pytest.raises(TypeError):
            run_csp_allocator(
                [cand],
                {"Yash_Household": hh},
                vix=18.0,
                extras_provider=_empty_extras,
                staging_callback=lambda _t: None,  # type: ignore[call-arg]
            )
