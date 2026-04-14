"""Unit tests for ``agt_equities.health`` (Decoupling Sprint A Unit A2).

Heartbeat write/read, stale detection, orphan sweep against an isolated
file-backed SQLite DB. The tripwire fixture in ``tests/conftest.py``
guarantees we never touch the production DB.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agt_equities import health
from agt_equities.db import get_db_connection
from agt_equities.schema import register_operational_tables


# ---------------------------------------------------------------------------
# Isolated test-DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Per-test SQLite file with the operational schema applied."""
    db = tmp_path / "agt_test_health.db"
    conn = get_db_connection(db_path=db)
    try:
        register_operational_tables(conn)
        conn.commit()
    finally:
        conn.close()
    return db


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_schema_creates_daemon_heartbeat(tmp_db: Path):
    conn = sqlite3.connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='daemon_heartbeat'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


def test_schema_creates_orphan_sweep_log(tmp_db: Path):
    conn = sqlite3.connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='orphan_sweep_log'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Heartbeat write/read
# ---------------------------------------------------------------------------

def test_write_heartbeat_inserts_row(tmp_db: Path):
    health.write_heartbeat("agt_scheduler", pid=12345, client_id=2, db_path=tmp_db)
    hb = health.get_heartbeat("agt_scheduler", db_path=tmp_db)
    assert hb is not None
    assert hb["daemon_name"] == "agt_scheduler"
    assert hb["pid"] == 12345
    assert hb["client_id"] == 2
    assert hb["last_beat_utc"]


def test_write_heartbeat_upserts_on_repeat(tmp_db: Path):
    health.write_heartbeat("agt_bot", pid=1, client_id=1, notes="boot", db_path=tmp_db)
    health.write_heartbeat("agt_bot", pid=2, client_id=1, notes="ok", db_path=tmp_db)
    hb = health.get_heartbeat("agt_bot", db_path=tmp_db)
    assert hb is not None
    assert hb["pid"] == 2
    assert hb["notes"] == "ok"

    # Confirm exactly one row, not two.
    conn = sqlite3.connect(tmp_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM daemon_heartbeat WHERE daemon_name='agt_bot'"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_get_heartbeat_returns_none_when_absent(tmp_db: Path):
    assert health.get_heartbeat("nonexistent_daemon", db_path=tmp_db) is None


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------

def test_heartbeat_age_seconds_basic(tmp_db: Path):
    fixed_now = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
    # Insert a stale heartbeat manually.
    conn = sqlite3.connect(tmp_db)
    try:
        five_min_ago = (fixed_now - timedelta(minutes=5)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO daemon_heartbeat (daemon_name, last_beat_utc, pid) VALUES (?, ?, ?)",
            ("stale_d", five_min_ago, 99),
        )
        conn.commit()
    finally:
        conn.close()
    age = health.heartbeat_age_seconds("stale_d", now=fixed_now, db_path=tmp_db)
    assert age is not None
    assert 299 <= age <= 301  # 5 minutes ± rounding


def test_heartbeat_age_seconds_returns_none_when_missing(tmp_db: Path):
    assert health.heartbeat_age_seconds("ghost", db_path=tmp_db) is None


def test_is_daemon_stale_missing_is_stale(tmp_db: Path):
    assert health.is_daemon_stale("never_started", db_path=tmp_db) is True


def test_is_daemon_stale_fresh(tmp_db: Path):
    health.write_heartbeat("fresh_d", db_path=tmp_db)
    assert health.is_daemon_stale("fresh_d", db_path=tmp_db) is False


def test_is_daemon_stale_old_beats_ttl(tmp_db: Path):
    fixed_now = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
    conn = sqlite3.connect(tmp_db)
    try:
        old = (fixed_now - timedelta(seconds=200)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO daemon_heartbeat (daemon_name, last_beat_utc, pid) VALUES (?, ?, ?)",
            ("oldie", old, 1),
        )
        conn.commit()
    finally:
        conn.close()
    # Default TTL = 90s; 200s old → stale.
    assert health.is_daemon_stale("oldie", now=fixed_now, db_path=tmp_db) is True
    # Override TTL above the age → fresh.
    assert health.is_daemon_stale("oldie", ttl_s=300, now=fixed_now, db_path=tmp_db) is False


# ---------------------------------------------------------------------------
# Orphan sweep
# ---------------------------------------------------------------------------

def _seed_pending_orders(db: Path, rows: list[tuple[str, str]]) -> None:
    """rows = [(payload_json, created_at_iso), ...] all status='staged'."""
    conn = sqlite3.connect(db)
    try:
        for payload, created_at in rows:
            conn.execute(
                "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, 'staged', ?)",
                (payload, created_at),
            )
        conn.commit()
    finally:
        conn.close()


def test_orphan_sweep_zero_when_empty(tmp_db: Path):
    swept = health.sweep_orphan_staged_orders(db_path=tmp_db)
    assert swept == 0
    # Audit row still appended.
    conn = sqlite3.connect(tmp_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM orphan_sweep_log").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_orphan_sweep_skips_fresh_rows(tmp_db: Path):
    fixed_now = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
    fresh_iso = (fixed_now - timedelta(hours=1)).isoformat(timespec="seconds")
    _seed_pending_orders(tmp_db, [("{}", fresh_iso)])
    swept = health.sweep_orphan_staged_orders(
        ttl_hours=24.0, now=fixed_now, db_path=tmp_db
    )
    assert swept == 0
    # Row remains staged.
    conn = sqlite3.connect(tmp_db)
    try:
        status = conn.execute("SELECT status FROM pending_orders").fetchone()[0]
        assert status == "staged"
    finally:
        conn.close()


def test_orphan_sweep_supersedes_old_rows(tmp_db: Path):
    fixed_now = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
    old_iso = (fixed_now - timedelta(hours=48)).isoformat(timespec="seconds")
    fresh_iso = (fixed_now - timedelta(hours=1)).isoformat(timespec="seconds")
    _seed_pending_orders(tmp_db, [
        ("{\"old\":1}", old_iso),
        ("{\"old\":2}", old_iso),
        ("{\"fresh\":1}", fresh_iso),
    ])
    swept = health.sweep_orphan_staged_orders(
        ttl_hours=24.0, now=fixed_now, db_path=tmp_db
    )
    assert swept == 2
    conn = sqlite3.connect(tmp_db)
    try:
        statuses = sorted(
            r[0] for r in conn.execute("SELECT status FROM pending_orders").fetchall()
        )
        assert statuses == ["staged", "superseded", "superseded"]
        log = conn.execute(
            "SELECT swept_count, ttl_hours FROM orphan_sweep_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert log[0] == 2
        assert log[1] == 24.0
    finally:
        conn.close()


def test_orphan_sweep_does_not_touch_non_staged(tmp_db: Path):
    """superseded / processing rows must not be re-touched even if old."""
    fixed_now = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
    old_iso = (fixed_now - timedelta(hours=48)).isoformat(timespec="seconds")
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, 'processing', ?)",
            ("{}", old_iso),
        )
        conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, 'superseded', ?)",
            ("{}", old_iso),
        )
        conn.commit()
    finally:
        conn.close()
    swept = health.sweep_orphan_staged_orders(
        ttl_hours=24.0, now=fixed_now, db_path=tmp_db
    )
    assert swept == 0
