#!/usr/bin/env python3
"""
rotate_session_log.py — Weekly rotation of autonomous_session_log.

Keeps the last 7 days in the main table. Older rows get archived to
autonomous_session_log_archive. Prevents unbounded context growth when
tasks read recent history.

Run weekly (e.g., Sunday night) or at the start of the Opus architect review.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

os.chdir(Path(__file__).resolve().parent.parent)

DB_PATH = (
    os.environ.get("AGT_DB_PATH")
    or str(Path(__file__).resolve().parent.parent / "agt_desk.db")
)


def rotate(keep_days: int = 7) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.row_factory = sqlite3.Row

    # Ensure archive table exists (mirrors main table)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autonomous_session_log_archive (
            id INTEGER PRIMARY KEY,
            task_name TEXT NOT NULL,
            run_at TEXT NOT NULL,
            summary TEXT,
            positions_snapshot JSON,
            orders_snapshot JSON,
            actions_taken JSON,
            errors JSON,
            metrics JSON,
            notes TEXT,
            archived_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    cutoff = f"-{keep_days} days"

    # Count rows to archive
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM autonomous_session_log "
        "WHERE run_at < datetime('now', ?)", (cutoff,)
    ).fetchone()
    archive_count = row["cnt"]

    if archive_count == 0:
        conn.close()
        return {"archived": 0, "remaining": 0, "message": "Nothing to rotate"}

    # Copy old rows to archive
    conn.execute(
        "INSERT INTO autonomous_session_log_archive "
        "(id, task_name, run_at, summary, positions_snapshot, orders_snapshot, "
        "actions_taken, errors, metrics, notes) "
        "SELECT id, task_name, run_at, summary, positions_snapshot, orders_snapshot, "
        "actions_taken, errors, metrics, notes "
        "FROM autonomous_session_log "
        "WHERE run_at < datetime('now', ?)", (cutoff,)
    )

    # Delete archived rows from main table
    conn.execute(
        "DELETE FROM autonomous_session_log WHERE run_at < datetime('now', ?)",
        (cutoff,)
    )

    remaining = conn.execute(
        "SELECT COUNT(*) as cnt FROM autonomous_session_log"
    ).fetchone()["cnt"]

    conn.commit()
    conn.close()

    return {
        "archived": archive_count,
        "remaining": remaining,
        "cutoff_days": keep_days,
        "timestamp": datetime.now().isoformat(),
    }


if __name__ == "__main__":
    from agt_equities.boot import assert_boot_contract
    assert_boot_contract()
    result = rotate()
    print(json.dumps(result, indent=2))
    print(f"\nRotated {result['archived']} rows, {result['remaining']} remaining")
