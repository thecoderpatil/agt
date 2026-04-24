"""Sprint 8 Mega-MR 3 — decision_outcomes schema tightening (DR B6).

Adds three forensic-correlation columns to decision_outcomes:
  - config_hash             : engine config fingerprint at decision time
  - triggering_rule_id      : invariant_id or policy name that caused decision
  - kill_switch_invocation_ref : link to kill_switch_events.id if active

Idempotent. Safe to re-run. Pattern: _safe_add_column wraps ALTER TABLE to
swallow SQLite's "duplicate column name" error, since unlike MySQL, SQLite
raises on second add.

Usage:
    python scripts/migrate_decision_outcomes_tightening.py --db-path <path>
"""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from agt_equities.db import get_db_connection


NEW_COLUMNS = [
    ("config_hash", "TEXT"),
    ("triggering_rule_id", "TEXT"),
    ("kill_switch_invocation_ref", "TEXT"),
]


BACKFILL_STMTS = [
    "UPDATE decision_outcomes SET config_hash = 'pre_migration_unknown' "
    "WHERE config_hash IS NULL",
    "UPDATE decision_outcomes SET triggering_rule_id = 'pre_migration_unknown' "
    "WHERE triggering_rule_id IS NULL",
    # kill_switch_invocation_ref: NULL allowed — most decisions lack kill-switch context.
]


NEW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_decision_outcomes_triggering_rule "
    "ON decision_outcomes(triggering_rule_id)",
    "CREATE INDEX IF NOT EXISTS idx_decision_outcomes_kill_switch "
    "ON decision_outcomes(kill_switch_invocation_ref) "
    "WHERE kill_switch_invocation_ref IS NOT NULL",
]


def _safe_add_column(
    conn: sqlite3.Connection, table: str, column_name: str, column_type: str
) -> bool:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_type}")
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return False
        raise


def run(db_path: str | Path | None = None) -> dict:
    added = []
    skipped = []
    with closing(get_db_connection(db_path=db_path)) as conn:
        for col_name, col_type in NEW_COLUMNS:
            if _safe_add_column(conn, "decision_outcomes", col_name, col_type):
                added.append(col_name)
            else:
                skipped.append(col_name)
        for stmt in BACKFILL_STMTS:
            conn.execute(stmt)
        for stmt in NEW_INDEXES:
            conn.execute(stmt)
        conn.commit()
    return {"added": added, "skipped_existing": skipped}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=None)
    args = ap.parse_args()
    result = run(db_path=args.db_path)
    print(f"decision_outcomes tightening: added={result['added']} "
          f"skipped_existing={result['skipped_existing']}")
