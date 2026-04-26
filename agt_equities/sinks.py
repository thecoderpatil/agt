"""Order and Decision sinks - composition-root DI seams for Shadow Scan.

See ``docs/adr/ADR-008_SHADOW_SCAN.md`` for the full architecture.

``SQLiteOrderSink`` / ``SQLiteDecisionSink`` wrap the production write
paths so live callers stage orders + write cycle/dynamic-exit logs as
they always have. ``CollectorOrderSink`` / ``CollectorDecisionSink``
capture the same calls in memory so ``scripts/shadow_scan.py`` can drain
them without touching any table.

MR 1 scope: protocols + classes + ``ShadowOrder`` / ``ShadowDecision``
dataclasses + thread-safe drain. NO engine is rewired to ctx yet - that
happens in MR 2 (CSP allocator), MR 3 (harvest), MR 4 (roll), MR 5 (CC
split).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Shadow-side artifacts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowOrder:
    """An order that would have been staged but was routed to memory.

    Shape matches the live ``pending_orders.payload`` closely enough that
    Cowork review can compare tickets field-by-field against live output.
    """

    engine: str                       # 'cc_engine' | 'csp_allocator' | 'csp_harvest' | 'roll_engine'
    run_id: str                       # ctx.run_id from RunContext
    ticker: str
    right: str                        # 'P' | 'C'
    strike: float
    qty: int
    limit: float | None
    decided_at: str                   # ISO8601 UTC
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ShadowDecision:
    """A mutating write that would have landed in ``cc_cycle_log`` or
    ``bucket3_dynamic_exit_log`` but was routed to memory instead.

    ``kind`` distinguishes the source table so Cowork can render them
    separately in the digest.
    """

    kind: str                         # 'cc_cycle' | 'dynamic_exit'
    run_id: str
    payload: dict[str, Any]
    decided_at: str                   # ISO8601 UTC


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Order sinks
# ---------------------------------------------------------------------------

class SQLiteOrderSink:
    """Live order staging sink - delegates to the production staging fn.

    Constructor takes a callable so the composition root wires in
    ``append_pending_tickets`` without this module importing
    ``telegram_bot`` / ``order_state`` (which would drag the full bot
    stack into every process that wants an ``OrderSink``).

    MR 1 does not rewrite any call site. This sink exists as the contract
    engines adopt starting in MR 2 (CSP allocator).
    """

    def __init__(
        self,
        staging_fn: Callable[..., Any],
        supersede_fn: Callable[..., Any] | None = None,
    ) -> None:
        self._staging_fn = staging_fn
        self._supersede_fn = supersede_fn

    def stage(
        self,
        tickets: list[dict],
        *,
        engine: str,
        run_id: str,
        meta: dict | None = None,
    ) -> None:
        """Forward tickets to the production staging function.

        Phase B Foundation: enrich each ticket with engine/run_id and the
        broker_mode_at_staging / spot_at_staging / premium_at_staging /
        gate_verdicts fields supplied via meta, plus a staged_at_utc
        timestamp. setdefault preserves any caller-supplied values.
        """
        if not tickets:
            return
        meta_ = dict(meta or {})
        enriched: list[dict] = []
        for t in tickets:
            e = dict(t)
            e.setdefault("engine", engine)
            e.setdefault("run_id", run_id)
            if "broker_mode" in meta_:
                e.setdefault("broker_mode_at_staging", meta_["broker_mode"])
            if "spot_at_staging" in meta_:
                e.setdefault("spot_at_staging", meta_["spot_at_staging"])
            if "premium_at_staging" in meta_:
                e.setdefault("premium_at_staging", meta_["premium_at_staging"])
            if "gate_verdicts" in meta_:
                e.setdefault("gate_verdicts", meta_["gate_verdicts"])
            e.setdefault("staged_at_utc", _utc_now_iso())
            enriched.append(e)
        self._staging_fn(enriched)


class CollectorOrderSink:
    """Shadow order sink - appends to an in-memory list, thread-safe.

    Thread-safety is required because Shadow Scan may fan out engine
    calls across ``asyncio`` tasks and worker threads (e.g. ``ib_async``
    callbacks). All public methods acquire the internal lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._orders: list[ShadowOrder] = []

    def stage(
        self,
        tickets: list[dict],
        *,
        engine: str,
        run_id: str,
        meta: dict | None = None,
    ) -> None:
        if not tickets:
            return
        meta_ = dict(meta or {})
        batch: list[ShadowOrder] = []
        for t in tickets:
            try:
                so = ShadowOrder(
                    engine=engine,
                    run_id=run_id,
                    ticker=str(t.get("ticker", "")),
                    right=str(t.get("right", "")),
                    strike=float(t.get("strike", 0.0)),
                    qty=int(t.get("qty", 0)),
                    limit=(
                        float(t["limit"]) if t.get("limit") is not None else None
                    ),
                    decided_at=_utc_now_iso(),
                    meta={
                        **meta_,
                        **{
                            k: v
                            for k, v in t.items()
                            if k
                            not in ("ticker", "right", "strike", "qty", "limit")
                        },
                    },
                )
            except (TypeError, ValueError) as exc:
                # Shadow mode is observational - never raise out of a sink.
                # Record the parse failure as meta so Cowork can triage.
                so = ShadowOrder(
                    engine=engine,
                    run_id=run_id,
                    ticker="",
                    right="",
                    strike=0.0,
                    qty=0,
                    limit=None,
                    decided_at=_utc_now_iso(),
                    meta={
                        "shadow_parse_error": str(exc),
                        "raw": {k: str(v) for k, v in (t or {}).items()},
                    },
                )
            batch.append(so)
        with self._lock:
            self._orders.extend(batch)

    def drain(self) -> list[ShadowOrder]:
        """Atomically return collected orders and reset.

        Returns a new list each call; callers may safely mutate.
        """
        with self._lock:
            out = list(self._orders)
            self._orders.clear()
        return out

    def peek(self) -> list[ShadowOrder]:
        """Return a snapshot without draining. Tests + digests may peek."""
        with self._lock:
            return list(self._orders)

    def __len__(self) -> int:
        with self._lock:
            return len(self._orders)


# ---------------------------------------------------------------------------
# Decision sinks
# ---------------------------------------------------------------------------

class SQLiteDecisionSink:
    """Live decision sink - wraps ``cc_cycle_log`` and
    ``bucket3_dynamic_exit_log`` writers.

    Constructor takes callables for the same reason ``SQLiteOrderSink``
    does: keep this module free of bot-stack imports.
    """

    def __init__(
        self,
        record_cc_cycle_fn: Callable[..., Any],
        record_dynamic_exit_fn: Callable[..., Any],
    ) -> None:
        self._record_cc_cycle_fn = record_cc_cycle_fn
        self._record_dynamic_exit_fn = record_dynamic_exit_fn

    def record_cc_cycle(self, entries: list[dict], *, run_id: str) -> None:
        if not entries:
            return
        self._record_cc_cycle_fn(entries)

    def record_dynamic_exit(self, entries: list[dict], *, run_id: str) -> None:
        if not entries:
            return
        self._record_dynamic_exit_fn(entries)


class CollectorDecisionSink:
    """Shadow decision sink - appends to an in-memory list, thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._decisions: list[ShadowDecision] = []

    def record_cc_cycle(self, entries: list[dict], *, run_id: str) -> None:
        if not entries:
            return
        batch = [
            ShadowDecision(
                kind="cc_cycle",
                run_id=run_id,
                payload=dict(e),
                decided_at=_utc_now_iso(),
            )
            for e in entries
        ]
        with self._lock:
            self._decisions.extend(batch)

    def record_dynamic_exit(self, entries: list[dict], *, run_id: str) -> None:
        if not entries:
            return
        batch = [
            ShadowDecision(
                kind="dynamic_exit",
                run_id=run_id,
                payload=dict(e),
                decided_at=_utc_now_iso(),
            )
            for e in entries
        ]
        with self._lock:
            self._decisions.extend(batch)

    def drain(self) -> list[ShadowDecision]:
        with self._lock:
            out = list(self._decisions)
            self._decisions.clear()
        return out

    def peek(self) -> list[ShadowDecision]:
        with self._lock:
            return list(self._decisions)

    def __len__(self) -> int:
        with self._lock:
            return len(self._decisions)


class NullDecisionSink:
    """No-op decision sink for tests and LIVE paths that do not persist."""

    def record_cc_cycle(self, entries: list[dict], *, run_id: str) -> None:
        return None

    def record_dynamic_exit(self, entries: list[dict], *, run_id: str) -> None:
        return None


__all__ = [
    "ShadowOrder",
    "ShadowDecision",
    "SQLiteOrderSink",
    "CollectorOrderSink",
    "SQLiteDecisionSink",
    "CollectorDecisionSink",
    "NullDecisionSink",
]
