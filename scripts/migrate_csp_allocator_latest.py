"""Migration: csp_allocator_latest singleton table.

Sprint 4 MR A (2026-04-24). Per ADR-CSP_TELEGRAM_DIGEST_v1 §5 step 2 wiring.

The scheduler's csp_digest_send job (09:37 ET weekdays) reads the most recent
AllocatorResult from this table, formats it via csp_digest.formatter, fetches
LLM commentary, and sends a Telegram digest. The allocator fail-softly writes
here at end of every run via csp_allocator.persist_latest_result.

Idempotent. Safe to rerun; CREATE TABLE IF NOT EXISTS.

Usage:
    python scripts/migrate_csp_allocator_latest.py
    python scripts/migrate_csp_allocator_latest.py --db-path /custom/path.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from repo root without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agt_equities.db import get_db_connection, tx_immediate  # noqa: E402


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS csp_allocator_latest (
    id          INTEGER PRIMARY KEY CHECK(id = 1),
    run_id      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    staged_json TEXT NOT NULL,
    rejected_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def migrate(db_path: str | None = None) -> None:
    with get_db_connection(db_path=db_path) as conn:
        with tx_immediate(conn):
            conn.execute(SCHEMA_SQL)
    print(f"csp_allocator_latest migration OK (db_path={db_path or '<default>'})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-path", type=str, default=None)
    args = ap.parse_args()
    migrate(args.db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
