"""Sprint 6 Mega-MR 4A — ADR-011 §10.1 decision_outcomes table migration.

Creates `decision_outcomes` table + two indexes. Idempotent; safe to
re-run.

Usage:
    python scripts/migrate_decision_outcomes.py --db-path <path>
"""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from agt_equities.db import get_db_connection

DDL = """
CREATE TABLE IF NOT EXISTS decision_outcomes (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id                  TEXT NOT NULL UNIQUE,
    engine                       TEXT NOT NULL
        CHECK(engine IN ('exit','roll','harvest','entry')),
    canary_step                  TEXT NOT NULL
        CHECK(canary_step IN ('paper','canary_1','canary_2','canary_3','live')),
    fire_timestamp_utc           TEXT NOT NULL,
    account_id                   TEXT NOT NULL,
    household                    TEXT NOT NULL,
    gate_snapshot_json           TEXT NOT NULL,
    inputs_json                  TEXT NOT NULL,
    fill_outcome_json            TEXT,
    reconciliation_delta_json    TEXT,
    created_at                   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                   TEXT
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_decision_outcomes_engine_timestamp "
    "ON decision_outcomes(engine, fire_timestamp_utc);",
    "CREATE INDEX IF NOT EXISTS idx_decision_outcomes_canary_step "
    "ON decision_outcomes(canary_step);",
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
    print("decision_outcomes schema migration complete")
