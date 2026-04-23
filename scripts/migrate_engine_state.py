"""Sprint 6 Mega-MR 5 — ADR-011 §4 engine_state table migration.

One row per engine (exit/roll/harvest/entry). Columns reflect the
pre-gateway/kill-switch state machine in ADR-011 §4 + §5.

Idempotent: safe to re-run. Initial seed leaves all four engines at
`paper` canary_step with `halted=0` (false).

Usage:
    python scripts/migrate_engine_state.py --db-path <path>
"""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from agt_equities.db import get_db_connection


DDL = """
CREATE TABLE IF NOT EXISTS engine_state (
    engine         TEXT PRIMARY KEY
        CHECK(engine IN ('exit','roll','harvest','entry')),
    canary_step    TEXT NOT NULL DEFAULT 'paper'
        CHECK(canary_step IN ('paper','canary_1','canary_2','canary_3','live','halted')),
    halted         INTEGER NOT NULL DEFAULT 0
        CHECK(halted IN (0, 1)),
    halted_reason  TEXT,
    halted_at_utc  TEXT,
    last_transition_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes          TEXT
);
"""


_SEED = [
    ("exit",    "paper", 0),
    ("roll",    "paper", 0),
    ("harvest", "paper", 0),
    ("entry",   "paper", 0),
]


def run(db_path: str | Path | None = None) -> dict[str, int]:
    stats = {"table_created": 0, "rows_seeded": 0}
    with closing(get_db_connection(db_path=db_path)) as conn:
        # Detect pre-existence for stats reporting.
        existed = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='engine_state'"
        ).fetchone()
        conn.executescript(DDL)
        if not existed:
            stats["table_created"] = 1
        for engine, canary_step, halted in _SEED:
            cur = conn.execute(
                "INSERT OR IGNORE INTO engine_state "
                "(engine, canary_step, halted) VALUES (?, ?, ?)",
                (engine, canary_step, halted),
            )
            stats["rows_seeded"] += cur.rowcount
        conn.commit()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=None)
    args = ap.parse_args()
    stats = run(db_path=args.db_path)
    print(f"engine_state migration: {stats}")
