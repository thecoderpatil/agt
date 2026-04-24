"""Sprint 6 Mega-MR 5 — ADR-011 §4 pre-gateway risk layer (SKELETON).

This module sits between any engine's `order_sink.stage(...)` call and
the IB Gateway's `placeOrder` invocation. It evaluates four trip
conditions on every order:

  K1 — session drawdown > 5.0% of pre-open NAV
  K2 — >= 3 consecutive broker rejections in any rolling 60s window
  K3 — signal-to-ack latency p95 > 500ms over trailing 20 orders
  K4 — correlation drift vs paper baseline < 0.95 over trailing 50

Any single trip:
  1. Halts the offending engine via `engine_state.halt_engine(...)`.
  2. Cancels the engine's open working orders (best-effort).
  3. Writes a Tier-0 incident.
  4. Sends a Telegram alert.
  5. Does NOT roll back already-filled orders.

This MR ships the **SKELETON** — the evaluator functions raise
NotImplementedError. Sprint 7 or later fills the bodies once we have
live telemetry surfaces to read from (order-fill timestamps for K3,
session NAV snapshot for K1, etc). The dataclass TripResult + the
public signature of `evaluate_order` are stable contracts; future
implementations must not break them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Engine = Literal["exit", "roll", "harvest", "entry"]


@dataclass(frozen=True)
class TripResult:
    """Outcome of a pre-gateway evaluation pass.

    Attributes:
        tripped: True if ANY of K1-K4 flagged the order.
        k1_session_drawdown_tripped: per-check flag.
        k2_consecutive_rejections_tripped: per-check flag.
        k3_latency_tripped: per-check flag.
        k4_correlation_drift_tripped: per-check flag.
        reason: free-form human-readable explanation for the first trip.
        evidence: structured payload (numeric thresholds + actuals) for
            the incident writer + Telegram alert. Keys map 1:1 to the
            K1-K4 evaluator implementations in Sprint 7+.
    """

    tripped: bool
    k1_session_drawdown_tripped: bool
    k2_consecutive_rejections_tripped: bool
    k3_latency_tripped: bool
    k4_correlation_drift_tripped: bool
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def evaluate_k1_session_drawdown(
    *,
    engine: Engine,
    session_nav: float,
    pre_open_nav: float,
    threshold_pct: float,
) -> TripResult:
    """K1 — session drawdown guard.

    Skeleton raises NotImplementedError. Real implementation Sprint 7+:
    compares `session_nav` to `pre_open_nav` and flags the trip when
    drawdown exceeds `threshold_pct`. Evidence payload carries both
    NAVs + computed drawdown pct.
    """
    raise NotImplementedError(
        "Sprint 6 Mega-MR 5 skeleton: K1 evaluator body lands in Sprint 7. "
        "Signature + TripResult contract are stable."
    )


def evaluate_k2_consecutive_rejections(
    *,
    engine: Engine,
    recent_rejections: list[dict[str, Any]],
    threshold_count: int,
    window_seconds: int,
) -> TripResult:
    """K2 — consecutive broker rejections guard.

    Skeleton raises NotImplementedError. Real implementation counts
    rejections within the trailing `window_seconds` and flags when
    count >= `threshold_count`. Evidence payload carries the rejection
    tickets themselves.
    """
    raise NotImplementedError(
        "Sprint 6 Mega-MR 5 skeleton: K2 evaluator body lands in Sprint 7."
    )


def evaluate_k3_latency(
    *,
    engine: Engine,
    recent_latencies_ms: list[float],
    threshold_p95_ms: float,
) -> TripResult:
    """K3 — signal-to-ack latency p95 guard.

    Skeleton raises NotImplementedError. Real implementation computes
    p95 of `recent_latencies_ms` over trailing N and flags when it
    exceeds `threshold_p95_ms`.
    """
    raise NotImplementedError(
        "Sprint 6 Mega-MR 5 skeleton: K3 evaluator body lands in Sprint 7."
    )


def evaluate_k4_correlation_drift(
    *,
    engine: Engine,
    live_decisions: list[Any],
    paper_decisions: list[Any],
    threshold_correlation: float,
) -> TripResult:
    """K4 — correlation drift vs paper baseline guard.

    Skeleton raises NotImplementedError. Real implementation computes
    Pearson correlation between paired live + paper decision sequences
    over trailing 50 and flags when it drops below threshold.
    """
    raise NotImplementedError(
        "Sprint 6 Mega-MR 5 skeleton: K4 evaluator body lands in Sprint 7."
    )


def evaluate_order(
    *,
    engine: Engine,
    order_payload: dict[str, Any],
) -> TripResult:
    """Aggregate evaluator called from the pre-gateway hook.

    Skeleton raises NotImplementedError. Real implementation:

      1. Pulls the four telemetry snapshots (NAV, recent_rejections,
         recent_latencies, live+paper decision pairs) from the
         appropriate repo modules.
      2. Invokes evaluate_k1..k4 with config-sourced thresholds from
         `config/promotion_gates.yaml`.
      3. If any trips, returns an aggregated TripResult with the first
         reason + merged evidence payload.
      4. If none trips, returns TripResult(tripped=False, ...).

    The calling pre-gateway hook is responsible for acting on the
    TripResult (halt engine, cancel orders, write incident, send
    Telegram alert).
    """
    raise NotImplementedError(
        "Sprint 6 Mega-MR 5 skeleton: evaluate_order body lands in Sprint 7."
    )


__all__ = [
    "Engine",
    "TripResult",
    "evaluate_k1_session_drawdown",
    "evaluate_k2_consecutive_rejections",
    "evaluate_k3_latency",
    "evaluate_k4_correlation_drift",
    "evaluate_order",
]
