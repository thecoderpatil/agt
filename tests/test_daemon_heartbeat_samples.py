"""health.write_heartbeat double-writes daemon_heartbeat + samples."""
from __future__ import annotations

import sqlite3
import time

import pytest

pytestmark = pytest.mark.sprint_a


def _seed(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE daemon_heartbeat (
                daemon_name TEXT PRIMARY KEY,
                last_beat_utc TEXT NOT NULL,
                pid INTEGER NOT NULL,
                client_id INTEGER,
                notes TEXT
            );
            CREATE TABLE daemon_heartbeat_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                daemon_name TEXT NOT NULL,
                beat_utc TEXT NOT NULL,
                pid INTEGER NOT NULL,
                client_id INTEGER,
                notes TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_double_write_appends_to_samples(tmp_path, monkeypatch):
    db = tmp_path / "agt.db"
    _seed(db)
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    from agt_equities import db as agt_db
    monkeypatch.setattr(agt_db, "DB_PATH", str(db))
    from agt_equities import health as h
    h.write_heartbeat("agt_scheduler", pid=111, client_id=2, db_path=str(db))
    time.sleep(0.01)
    h.write_heartbeat("agt_scheduler", pid=111, client_id=2, db_path=str(db))
    h.write_heartbeat("agt_bot", pid=222, client_id=3, db_path=str(db))

    conn = sqlite3.connect(str(db))
    try:
        upsert_count = conn.execute("SELECT COUNT(*) FROM daemon_heartbeat").fetchone()[0]
        sample_count = conn.execute("SELECT COUNT(*) FROM daemon_heartbeat_samples").fetchone()[0]
        sample_daemons = sorted({r[0] for r in conn.execute(
            "SELECT daemon_name FROM daemon_heartbeat_samples")})
    finally:
        conn.close()
    assert upsert_count == 2  # one row per daemon
    assert sample_count == 3  # three append-only writes
    assert sample_daemons == ["agt_bot", "agt_scheduler"]


def test_missing_samples_table_is_tolerated(tmp_path, monkeypatch):
    db = tmp_path / "agt.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE daemon_heartbeat (daemon_name TEXT PRIMARY KEY, last_beat_utc TEXT, "
        "pid INTEGER, client_id INTEGER, notes TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    from agt_equities import db as agt_db
    monkeypatch.setattr(agt_db, "DB_PATH", str(db))
    from agt_equities import health as h
    # Must NOT raise -- samples table doesn't exist, write should warn-and-continue.
    h.write_heartbeat("agt_scheduler", pid=111, client_id=2, db_path=str(db))
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT COUNT(*) FROM daemon_heartbeat").fetchone()[0]
    finally:
        conn.close()
    assert rows == 1
