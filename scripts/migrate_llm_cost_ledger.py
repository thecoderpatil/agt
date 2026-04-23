"""Idempotent migration: create llm_cost_ledger + idx_csp_pending_status_sent.

Per ADR-CSP_TELEGRAM_DIGEST_v1. Creates the ledger table the digest
LLM call writes to + the index on csp_pending_approval the ADR
specifies. Pure CREATE IF NOT EXISTS — safe to re-run.

Usage:
    python scripts/migrate_llm_cost_ledger.py [--db PATH]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_cost_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT    NOT NULL,
    run_id          TEXT    NOT NULL,
    call_site       TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    input_tokens    INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL,
    cost_usd        REAL    NOT NULL,
    status          TEXT    NOT NULL,
    error_class     TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_cost_timestamp
    ON llm_cost_ledger(timestamp_utc);

CREATE INDEX IF NOT EXISTS idx_llm_cost_site
    ON llm_cost_ledger(call_site, timestamp_utc);

CREATE INDEX IF NOT EXISTS idx_csp_pending_status_sent
    ON csp_pending_approval(status, sent_at_utc);
"""


def migrate(db_path: str) -> None:
    with closing(sqlite3.connect(db_path, timeout=10.0)) as conn:
        # csp_pending_approval may not exist on a fresh DB (it lives in
        # the prod DB schema, not our migrations). Create it minimally
        # so the index DDL succeeds idempotently. The full ADR schema
        # is enforced elsewhere.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS csp_pending_approval ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " run_id TEXT NOT NULL,"
            " household_id TEXT NOT NULL,"
            " candidates_json TEXT NOT NULL,"
            " sent_at_utc TEXT NOT NULL,"
            " timeout_at_utc TEXT NOT NULL,"
            " telegram_message_id INTEGER,"
            " status TEXT NOT NULL,"
            " approved_indices_json TEXT,"
            " resolved_at_utc TEXT,"
            " resolved_by TEXT"
            ")"
        )
        conn.executescript(SCHEMA_SQL)
        conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="C:/AGT_Runtime/state/agt_desk.db",
        help="SQLite DB path",
    )
    args = parser.parse_args(argv)
    migrate(args.db)
    print(f"llm_cost_ledger migrated against {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
