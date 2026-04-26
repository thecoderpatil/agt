"""Sprint 12 P0 — DEFECT-1 fix: write_heartbeat() tx_immediate regression tests.

Verifies that write_heartbeat() uses tx_immediate for all DB writes, eliminating
silent heartbeat failures under write-lock contention (DEFERRED → tx_immediate).
See: reports/heartbeat_stale_dual_daemon_20260426.md §5 DEFECT-1.
"""
from __future__ import annotations

import inspect
import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


def _seed(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE daemon_heartbeat (
                daemon_name TEXT PRIMARY KEY,
                last_beat_utc TEXT NOT NULL,
                pid INTEGER,
                client_id INTEGER,
                notes TEXT
            );
            CREATE TABLE daemon_heartbeat_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                daemon_name TEXT NOT NULL,
                beat_utc TEXT NOT NULL,
                pid INTEGER,
                client_id INTEGER,
                notes TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Source inspection
# ---------------------------------------------------------------------------

def test_tx_immediate_present_in_write_heartbeat_source():
    """write_heartbeat() must call tx_immediate — source inspection guard."""
    from agt_equities.health import write_heartbeat
    source = inspect.getsource(write_heartbeat)
    assert "tx_immediate" in source, "write_heartbeat must use tx_immediate context manager"


def test_conn_commit_not_called_in_write_heartbeat():
    """write_heartbeat() must NOT call conn.commit() — tx_immediate owns commit."""
    from agt_equities.health import write_heartbeat
    source = inspect.getsource(write_heartbeat)
    assert "conn.commit()" not in source, "write_heartbeat must not call conn.commit(); tx_immediate handles it"


# ---------------------------------------------------------------------------
# Functional: correct write behaviour
# ---------------------------------------------------------------------------

def test_write_heartbeat_upserts_row(tmp_path):
    """write_heartbeat creates the daemon_heartbeat row on first call."""
    db = tmp_path / "hb.db"
    _seed(db)
    from agt_equities.health import write_heartbeat
    write_heartbeat("agt_bot", pid=100, db_path=db)
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT daemon_name, pid FROM daemon_heartbeat WHERE daemon_name='agt_bot'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == 100


def test_write_heartbeat_idempotent_upsert(tmp_path):
    """Repeated calls upsert — one row, pid updated to latest."""
    db = tmp_path / "hb.db"
    _seed(db)
    from agt_equities.health import write_heartbeat
    write_heartbeat("agt_bot", pid=1, db_path=db)
    write_heartbeat("agt_bot", pid=2, db_path=db)
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT pid FROM daemon_heartbeat WHERE daemon_name='agt_bot'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == 2


def test_write_heartbeat_also_appends_samples(tmp_path):
    """Each write_heartbeat call appends a row to daemon_heartbeat_samples."""
    db = tmp_path / "hb.db"
    _seed(db)
    from agt_equities.health import write_heartbeat
    write_heartbeat("agt_bot", pid=1, db_path=db)
    write_heartbeat("agt_bot", pid=1, db_path=db)
    conn = sqlite3.connect(str(db))
    count = conn.execute(
        "SELECT COUNT(*) FROM daemon_heartbeat_samples WHERE daemon_name='agt_bot'"
    ).fetchone()[0]
    conn.close()
    assert count == 2


def test_write_heartbeat_missing_samples_table_tolerated(tmp_path):
    """If daemon_heartbeat_samples is absent, write_heartbeat still updates daemon_heartbeat."""
    db = tmp_path / "hb.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE daemon_heartbeat (daemon_name TEXT PRIMARY KEY, "
        "last_beat_utc TEXT NOT NULL, pid INTEGER, client_id INTEGER, notes TEXT)"
    )
    conn.commit()
    conn.close()
    from agt_equities.health import write_heartbeat
    write_heartbeat("agt_bot", pid=42, db_path=db)
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT pid FROM daemon_heartbeat WHERE daemon_name='agt_bot'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 42


# ---------------------------------------------------------------------------
# Concurrency: lock-contention failure is logged, not silently dropped
# ---------------------------------------------------------------------------

def test_write_heartbeat_lock_contention_logs_error(tmp_path, caplog, monkeypatch):
    """Under reserved-lock contention, write_heartbeat logs exception (not silent).

    With the old DEFERRED pattern the OperationalError was swallowed silently,
    leaving daemon_heartbeat stale with no trace in logs. With tx_immediate the
    failure propagates to the outer except and calls logger.exception().
    """
    db = tmp_path / "hb.db"
    _seed(db)

    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_reserved():
        c = sqlite3.connect(str(db), timeout=10.0)
        c.execute("PRAGMA busy_timeout = 10000;")
        c.execute("BEGIN IMMEDIATE;")
        lock_acquired.set()
        release_lock.wait(timeout=5.0)
        try:
            c.execute("ROLLBACK;")
        except Exception:
            pass
        c.close()

    t = threading.Thread(target=hold_reserved, daemon=True)
    t.start()
    assert lock_acquired.wait(timeout=3.0), "lock holder thread did not start"

    try:
        import agt_equities.db as _db_mod
        monkeypatch.setattr(_db_mod, "_BUSY_TIMEOUT_MS", 50)  # 50ms → fast timeout
        from agt_equities.health import write_heartbeat
        with caplog.at_level(logging.ERROR, logger="agt_equities.health"):
            write_heartbeat("agt_bot", pid=777, db_path=db)
    finally:
        release_lock.set()
        t.join(timeout=5.0)

    # daemon_heartbeat must NOT have pid=777 (tx failed atomically under contention)
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT pid FROM daemon_heartbeat WHERE daemon_name='agt_bot'").fetchone()
    conn.close()
    assert row is None or row[0] != 777, "pid=777 must not be written when tx fails under contention"

    # Failure must be logged at ERROR level — not silently dropped
    logged_msgs = [r.message for r in caplog.records]
    assert any("write_heartbeat" in m for m in logged_msgs), (
        f"Expected write_heartbeat error in logs; got: {logged_msgs}"
    )
