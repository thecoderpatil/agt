"""paper_baseline.py — ADR-011 §2 promotion gate DB adapter.

Provides read-only queries against the `decisions` and `incidents` tables
to evaluate promotion gate status for each engine.  Call
``evaluate_all(engine, db_path=...)`` to get a list of GateResult objects.

G1 / G3 / G4 are stubbed as insufficient_data pending schema additions
(see module docstring on each stub for required pre-requisites).

Never writes to the DB.  Accepts ``db_path`` keyword so tests can pass
an in-memory or tmp database without monkeypatching module globals.
"""
from __future__ import annotations

import dataclasses
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Literal

from agt_equities.db import get_ro_connection

# ---------------------------------------------------------------------------
# Thresholds — mirror config/promotion_gates.yaml.
# Changes require an ADR-011 amendment.
# ---------------------------------------------------------------------------
_G2_WINDOW_DAYS: int = 14
_G5_WINDOW_DAYS: int = 14
_G5_MIN_SETTLED: int = 30
_G5_MIN_OVERRIDES: int = 5
_G5_ALPHA_T: float = 1.645          # one-sided z-score approx for alpha=0.05
_NO_GATE_ENGINES: frozenset[str] = frozenset({"cc_exit", "roll_engine", "csp_harvest"})

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
GateStatus = Literal["green", "red", "insufficient_data"]


@dataclasses.dataclass(frozen=True)
class GateResult:
    gate_id: str          # "G1" ... "G5"
    status: GateStatus
    value: float | None   # observed metric (None if not computable)
    threshold: float | None
    message: str


# ---------------------------------------------------------------------------
# G1 stub -- shadow-vs-live divergence
# ---------------------------------------------------------------------------
def evaluate_g1(engine: str) -> GateResult:
    """G1: not implemented.

    Requires cross-joining shadow_scan JSON output (ADR-008) with a
    hypothetical live-priced fill surface.  Cannot be evaluated from SQLite
    alone.  Implement as a follow-on once shadow_scan persists its
    per-ticket bps divergence.
    """
    return GateResult(
        gate_id="G1",
        status="insufficient_data",
        value=None,
        threshold=None,
        message="G1 not implemented: requires shadow_scan JSON bps integration (ADR-008)",
    )


# ---------------------------------------------------------------------------
# G2 -- zero Tier-0/Tier-1 internal trips in trailing window
# ---------------------------------------------------------------------------
def evaluate_g2(
    engine: str,
    *,
    window_days: int = _G2_WINDOW_DAYS,
    db_path: str | None = None,
) -> GateResult:
    """G2: zero Tier-0 or Tier-1 *internal* invariant trips over the last
    ``window_days`` calendar days.

    Source: ``incidents`` table -- severity_tier <= 1, fault_source = 'internal',
    detected_at >= cutoff.  Vendor/broker incidents (fault_source != 'internal')
    do NOT count toward this gate.

    A single trip resets the gate to red regardless of subsequent clean days.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    sql = """
        SELECT COUNT(*) AS trip_count
        FROM incidents
        WHERE severity_tier <= 1
          AND fault_source = 'internal'
          AND detected_at >= ?
    """
    try:
        with closing(get_ro_connection(db_path)) as conn:
            row = conn.execute(sql, (cutoff,)).fetchone()
        trip_count = int(row[0]) if row else 0
    except Exception as exc:
        return GateResult(
            gate_id="G2",
            status="insufficient_data",
            value=None,
            threshold=0.0,
            message=f"G2 query failed: {exc}",
        )
    status: GateStatus = "green" if trip_count == 0 else "red"
    return GateResult(
        gate_id="G2",
        status=status,
        value=float(trip_count),
        threshold=0.0,
        message=(
            f"{trip_count} Tier-0/1 internal trip(s) in last {window_days} "
            f"calendar days"
        ),
    )


# ---------------------------------------------------------------------------
# G3 stub -- staged-decision sample size
# ---------------------------------------------------------------------------
def evaluate_g3(engine: str) -> GateResult:
    """G3: not implemented.

    ADR-011 §2 requires filtering pending_orders by engine.  pending_orders
    has no top-level `engine` column -- SQLiteOrderSink.stage() drops the
    engine kwarg before forwarding to append_pending_tickets.  Implement
    after adding an `engine TEXT` column to pending_orders and injecting it
    in SQLiteOrderSink.stage().
    """
    return GateResult(
        gate_id="G3",
        status="insufficient_data",
        value=None,
        threshold=60.0,
        message="G3 not implemented: requires engine column on pending_orders",
    )


# ---------------------------------------------------------------------------
# G4 stub -- broker rejection rate
# ---------------------------------------------------------------------------
def evaluate_g4(engine: str) -> GateResult:
    """G4: not implemented.  Same schema gap as G3."""
    return GateResult(
        gate_id="G4",
        status="insufficient_data",
        value=None,
        threshold=0.001,
        message="G4 not implemented: requires engine column on pending_orders",
    )


# ---------------------------------------------------------------------------
# G5 -- operator override counterfactual P&L variance
# ---------------------------------------------------------------------------
def evaluate_g5(
    engine: str,
    *,
    window_days: int = _G5_WINDOW_DAYS,
    min_settled: int = _G5_MIN_SETTLED,
    min_overrides: int = _G5_MIN_OVERRIDES,
    alpha_t: float = _G5_ALPHA_T,
    db_path: str | None = None,
) -> GateResult:
    """G5: operator override counterfactual P&L does not statistically beat
    the engine's own decisions (one-sided t-test, alpha=0.05).

    N/A for engines with no operator approval gate (cc_exit, roll_engine,
    csp_harvest) -- returns green unconditionally.

    Source: ``decisions`` table -- operator_action, realized_pnl,
    counterfactual_pnl.  Returns insufficient_data if fewer than
    ``min_settled`` decisions with both P&L fields populated exist.

    Statistical note: uses a normal approximation (z-score) for the
    one-sided t-test critical value.  Valid when n >= 30 (enforced by
    ``min_settled``).  scipy is not a project dependency; no import used.
    """
    if engine in _NO_GATE_ENGINES:
        return GateResult(
            gate_id="G5",
            status="green",
            value=None,
            threshold=None,
            message=f"G5 N/A for engine={engine} (no operator override gate)",
        )

    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    sql = """
        SELECT realized_pnl, counterfactual_pnl, operator_action
        FROM decisions
        WHERE engine = ?
          AND decision_timestamp >= ?
          AND realized_pnl IS NOT NULL
          AND counterfactual_pnl IS NOT NULL
    """
    try:
        with closing(get_ro_connection(db_path)) as conn:
            rows = conn.execute(sql, (engine, cutoff)).fetchall()
    except Exception as exc:
        return GateResult(
            gate_id="G5",
            status="insufficient_data",
            value=None,
            threshold=alpha_t,
            message=f"G5 query failed: {exc}",
        )

    if len(rows) < min_settled:
        return GateResult(
            gate_id="G5",
            status="insufficient_data",
            value=float(len(rows)),
            threshold=float(min_settled),
            message=(
                f"Only {len(rows)}/{min_settled} settled decisions for "
                f"engine={engine} in last {window_days} days"
            ),
        )

    overrides = [(float(r[0]), float(r[1])) for r in rows if r[2] == "rejected"]
    if len(overrides) < min_overrides:
        return GateResult(
            gate_id="G5",
            status="green",
            value=float(len(overrides)),
            threshold=None,
            message=(
                f"Only {len(overrides)} overrides -- t-test inconclusive; "
                f"gate green by default"
            ),
        )

    # diff = realized_pnl (override outcome) - counterfactual_pnl (engine outcome).
    # Positive mean_diff means operator beat engine.
    diffs = [a - c for a, c in overrides]
    n = len(diffs)
    mean_diff = sum(diffs) / n
    variance = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)

    if variance <= 0.0:
        # All diffs identical — positive mean = operator always beats engine (infinite t-stat → red)
        zero_status: GateStatus = "red" if mean_diff > 0 else "green"
        return GateResult(
            gate_id="G5",
            status=zero_status,
            value=float("inf") if mean_diff > 0 else 0.0,
            threshold=alpha_t,
            message=(
                f"Zero variance; mean_diff={mean_diff:.4f} "
                f"({'FAIL -- operator beats engine' if zero_status == 'red' else 'PASS'})"
            ),
        )

    t_stat = mean_diff / math.sqrt(variance / n)
    status: GateStatus = "red" if t_stat > alpha_t else "green"
    return GateResult(
        gate_id="G5",
        status=status,
        value=round(t_stat, 4),
        threshold=alpha_t,
        message=(
            f"t={t_stat:.3f} ({'FAIL -- operator beats engine' if status == 'red' else 'PASS'}); "
            f"n_overrides={n}; mean_diff={mean_diff:.4f}"
        ),
    )


# ---------------------------------------------------------------------------
# Aggregate evaluator
# ---------------------------------------------------------------------------
def evaluate_all(
    engine: str,
    *,
    window_days: int = 14,
    db_path: str | None = None,
) -> list[GateResult]:
    """Return GateResult for all 5 gates in ADR-011 §2 order (G1-G5).

    G1 / G3 / G4 return insufficient_data until their prerequisite schema
    changes ship.  G2 and G5 are fully evaluated.
    """
    return [
        evaluate_g1(engine),
        evaluate_g2(engine, window_days=window_days, db_path=db_path),
        evaluate_g3(engine),
        evaluate_g4(engine),
        evaluate_g5(engine, window_days=window_days, db_path=db_path),
    ]
