"""
scripts/migrate_heartbeat_samples_archive.py

Creates daemon_heartbeat_samples_archive table for the 30-day rolling
retention policy implemented by the heartbeat_archive scheduler job.
Idempotent: CREATE TABLE IF NOT EXISTS.
"""
import sys
from contextlib import closing

from agt_equities.db import get_db_connection, tx_immediate


def run(db_path=None):
    with closing(get_db_connection(db_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='daemon_heartbeat_samples_archive'"
        ).fetchone()
        if exists:
            print("SKIP: daemon_heartbeat_samples_archive already exists")
            return
        with tx_immediate(conn):
            conn.execute("""
                CREATE TABLE daemon_heartbeat_samples_archive (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    daemon_name TEXT NOT NULL,
                    beat_utc    TEXT NOT NULL,
                    pid         INTEGER NOT NULL,
                    client_id   INTEGER,
                    notes       TEXT,
                    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
        print("DONE: daemon_heartbeat_samples_archive created")


if __name__ == "__main__":
    run()
