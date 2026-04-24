"""ADR-018 Phase 1 — flex_sync_retry_attempts table migration.

Tracks automated retry attempts for flex_sync runs that returned zero
rows on a confirmed trading day. The 4-attempt escalation path
(original + 3 retries at +2h/+4h/+6h) persists here so reboots survive
the schedule.

Idempotent. Safe to re-run.

Usage:
    python scripts/migrate_flex_sync_retry_attempts.py --db-path <path>
"""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from agt_equities.db import get_db_connection

DDL = """
CREATE TABLE IF NOT EXISTS flex_sync_retry_attempts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    original_sync_id    INTEGER NOT NULL,
    coverage_date       TEXT NOT NULL,
    attempt_n           INTEGER NOT NULL,
    scheduled_at_utc    TEXT NOT NULL,
    attempted_at_utc    TEXT,
    result              TEXT,
    resolved_at_utc     TEXT,
    rows_recovered      INTEGER
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_flex_retry_date "
    "ON flex_sync_retry_attempts(coverage_date)",
    "CREATE INDEX IF NOT EXISTS idx_flex_retry_sync "
    "ON flex_sync_retry_attempts(original_sync_id)",
    "CREATE INDEX IF NOT EXISTS idx_flex_retry_pending "
    "ON flex_sync_retry_attempts(scheduled_at_utc) "
    "WHERE attempted_at_utc IS NULL",
]


def run(db_path: str | Path | None = None) -> None:
    with closing(get_db_connection(db_path=db_path)) as conn:
        conn.executescript(DDL)
        for stmt in INDEXES:
            conn.execute(stmt)
        conn.commit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=None)
    args = ap.parse_args()
    run(db_path=args.db_path)
    print("flex_sync_retry_attempts schema migration complete")
