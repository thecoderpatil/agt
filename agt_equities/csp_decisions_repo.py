"""CSP decision audit trail — one row per candidate per scan run.

Captures gate-by-gate verdicts + evidence snapshots regardless of final
outcome (staged or rejected). Enables post-hoc triage of why a candidate
was or wasn't selected, without re-running the scan.

Schema:
    run_id              TEXT NOT NULL      -- links decisions within a single scan
    household_id        TEXT NOT NULL
    ticker              TEXT NOT NULL
    decided_at_utc      TEXT NOT NULL      -- ISO8601 UTC
    final_outcome       TEXT NOT NULL      -- 'staged' | 'rejected_by_<gate_name>'
    gate_verdicts       TEXT NOT NULL      -- JSON: list of {gate, ok, reason}
    evidence_snapshot   TEXT NOT NULL      -- JSON: input snapshot at decision time
    n_requested         INTEGER            -- candidates requested by allocator
    n_sized             INTEGER            -- contracts sized (null if rejected)
    PRIMARY KEY (run_id, household_id, ticker)

Invariants:
- All public functions accept *, db_path: str | Path | None = None
- Writes are idempotent on (run_id, household_id, ticker)
- No long-lived connection; each call opens + closes
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agt_equities.db import get_db_connection, get_ro_connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS csp_decisions (
    run_id            TEXT    NOT NULL,
    household_id      TEXT    NOT NULL,
    ticker            TEXT    NOT NULL,
    decided_at_utc    TEXT    NOT NULL,
    final_outcome     TEXT    NOT NULL,
    gate_verdicts     TEXT    NOT NULL,
    evidence_snapshot TEXT    NOT NULL,
    n_requested       INTEGER,
    n_sized           INTEGER,
    PRIMARY KEY (run_id, household_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_csp_decisions_ticker
    ON csp_decisions(ticker, decided_at_utc DESC);

CREATE INDEX IF NOT EXISTS idx_csp_decisions_run
    ON csp_decisions(run_id);
"""


def ensure_schema(*, db_path: str | Path | None = None) -> None:
    """Idempotent schema create. Safe to call on every process start."""
    try:
        with get_db_connection(db_path) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()
    except sqlite3.Error as exc:
        raise RuntimeError(f"csp_decisions schema ensure failed: {exc}") from exc


def record_decision(
    *,
    run_id: str,
    household_id: str,
    ticker: str,
    final_outcome: str,
    gate_verdicts: list[dict[str, Any]],
    evidence_snapshot: dict[str, Any],
    n_requested: int | None = None,
    n_sized: int | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Record one candidate's decision. Idempotent on (run_id, household_id, ticker).

    Args:
        final_outcome: 'staged' or 'rejected_by_<gate_name>'.
        gate_verdicts: ordered list of {gate: str, ok: bool, reason: str | None}.
        evidence_snapshot: input dict at decision time (ticker fundamentals,
            delta, IVR, sector, etc.) — used for triage.
    """
    if not run_id or not household_id or not ticker:
        raise ValueError("run_id, household_id, ticker are required")
    if not final_outcome:
        raise ValueError("final_outcome is required")

    now = datetime.now(timezone.utc).isoformat()
    verdicts_json = json.dumps(gate_verdicts, default=str, sort_keys=True)
    evidence_json = json.dumps(evidence_snapshot, default=str, sort_keys=True)

    try:
        with get_db_connection(db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO csp_decisions (
                    run_id, household_id, ticker, decided_at_utc,
                    final_outcome, gate_verdicts, evidence_snapshot,
                    n_requested, n_sized
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, household_id, ticker, now,
                    final_outcome, verdicts_json, evidence_json,
                    n_requested, n_sized,
                ),
            )
            conn.commit()
    except sqlite3.Error as exc:
        # Fail-open: audit trail failure must NOT block trading decisions.
        # Log-only; allocator continues.
        import logging
        logging.getLogger(__name__).warning(
            "csp_decisions.record_decision failed for %s/%s/%s: %s",
            run_id, household_id, ticker, exc,
        )


def list_by_ticker(
    ticker: str,
    *,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return most recent decisions for a ticker, newest first."""
    try:
        with get_ro_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM csp_decisions
                WHERE ticker = ?
                ORDER BY decided_at_utc DESC
                LIMIT ?
                """,
                (ticker, limit),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
    except sqlite3.Error as exc:
        raise RuntimeError(f"csp_decisions.list_by_ticker failed: {exc}") from exc


def list_by_run(
    run_id: str,
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return all decisions from a single scan run."""
    try:
        with get_ro_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM csp_decisions
                WHERE run_id = ?
                ORDER BY ticker ASC
                """,
                (run_id,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
    except sqlite3.Error as exc:
        raise RuntimeError(f"csp_decisions.list_by_run failed: {exc}") from exc


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    # Re-hydrate JSON fields for caller convenience
    d["gate_verdicts"] = json.loads(d["gate_verdicts"])
    d["evidence_snapshot"] = json.loads(d["evidence_snapshot"])
    return d
