"""Sprint 6 Mega-MR 4A — ADR-011 §10.1 decision_outcomes repo.

Three stage lifecycle per row:

  1. `record_fire`:  engine decided + staged an action. Writes
     gate_snapshot_json + inputs_json at the moment of fire. At this
     point `fill_outcome_json` and `reconciliation_delta_json` are both
     NULL (outcome hasn't happened yet).

  2. `update_fill_outcome`: IB fill callback lands OR paper-auto-exec
     simulated fill lands. Writes fill_outcome_json + updated_at.
     `reconciliation_delta_json` still NULL (we don't yet have the
     post-Flex ground truth).

  3. `update_reconciliation`: T+1 flex sync lands; we compare our
     expected outcome vs what IBKR actually booked. Delta written to
     reconciliation_delta_json.

Reads: `get_fires_by_engine` supplies the Sprint 6+ learning loop a
chronological slice per engine. Retries happen by the caller;
decision_outcomes is append-only + updates only on id-by-id path.

All writes use `tx_immediate` per the db.py WAL discipline.
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agt_equities.db import get_db_connection, get_ro_connection, tx_immediate


_VALID_ENGINES = {"exit", "roll", "harvest", "entry"}
_VALID_CANARY_STEPS = {"paper", "canary_1", "canary_2", "canary_3", "live"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, sort_keys=True)


def record_fire(
    *,
    decision_id: str,
    engine: str,
    canary_step: str,
    fire_timestamp_utc: str | None = None,
    account_id: str,
    household: str,
    gate_snapshot: dict[str, Any],
    inputs: dict[str, Any],
    db_path: str | Path | None = None,
) -> None:
    """Record a new engine-fire row. Idempotent on `decision_id`
    (UNIQUE constraint; INSERT OR IGNORE)."""
    if engine not in _VALID_ENGINES:
        raise ValueError(f"engine must be one of {_VALID_ENGINES}, got {engine!r}")
    if canary_step not in _VALID_CANARY_STEPS:
        raise ValueError(
            f"canary_step must be one of {_VALID_CANARY_STEPS}, got {canary_step!r}"
        )
    ts = fire_timestamp_utc or _utc_now_iso()
    with closing(get_db_connection(db_path=db_path)) as conn:
        with tx_immediate(conn):
            conn.execute(
                """
                INSERT OR IGNORE INTO decision_outcomes (
                    decision_id, engine, canary_step, fire_timestamp_utc,
                    account_id, household, gate_snapshot_json, inputs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id, engine, canary_step, ts,
                    account_id, household,
                    _dumps(gate_snapshot), _dumps(inputs),
                ),
            )


def update_fill_outcome(
    *,
    decision_id: str,
    fill_outcome: dict[str, Any],
    db_path: str | Path | None = None,
) -> bool:
    """Attach fill outcome JSON + bump updated_at. Returns True if a row
    was updated, False if decision_id missing."""
    with closing(get_db_connection(db_path=db_path)) as conn:
        with tx_immediate(conn):
            cur = conn.execute(
                """
                UPDATE decision_outcomes
                SET fill_outcome_json = ?, updated_at = ?
                WHERE decision_id = ?
                """,
                (_dumps(fill_outcome), _utc_now_iso(), decision_id),
            )
            return cur.rowcount > 0


def update_reconciliation(
    *,
    decision_id: str,
    reconciliation_delta: dict[str, Any],
    db_path: str | Path | None = None,
) -> bool:
    """Attach reconciliation delta JSON + bump updated_at. Returns True
    if a row was updated."""
    with closing(get_db_connection(db_path=db_path)) as conn:
        with tx_immediate(conn):
            cur = conn.execute(
                """
                UPDATE decision_outcomes
                SET reconciliation_delta_json = ?, updated_at = ?
                WHERE decision_id = ?
                """,
                (_dumps(reconciliation_delta), _utc_now_iso(), decision_id),
            )
            return cur.rowcount > 0


def get_fires_by_engine(
    *,
    engine: str,
    since_utc: str | None = None,
    limit: int = 1000,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return most-recent-first engine-fire rows for a given engine.

    Optional `since_utc` (ISO 8601) lower-bounds fire_timestamp_utc.
    """
    if engine not in _VALID_ENGINES:
        raise ValueError(f"engine must be one of {_VALID_ENGINES}, got {engine!r}")
    with get_ro_connection(db_path=db_path) as conn:
        conn.row_factory = _row_to_dict
        if since_utc is not None:
            rows = conn.execute(
                """
                SELECT * FROM decision_outcomes
                WHERE engine = ? AND fire_timestamp_utc >= ?
                ORDER BY fire_timestamp_utc DESC LIMIT ?
                """,
                (engine, since_utc, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM decision_outcomes
                WHERE engine = ?
                ORDER BY fire_timestamp_utc DESC LIMIT ?
                """,
                (engine, int(limit)),
            ).fetchall()
    return rows


def _row_to_dict(cursor, row):
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}
