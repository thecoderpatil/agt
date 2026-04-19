"""Unit tests for F.6 nightly integration smoke checks.

Exercises ``agt_equities.smoke.run_nightly_smoke_checks`` against ephemeral
SQLite databases built in ``tmp_path``.  No bot stack required.

Coverage: fresh OK, stale heartbeat, missing service row,
missing daemon_heartbeat / pending_orders / decisions tables.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

from agt_equities.smoke import (  # noqa: E402
    _EXPECTED_DAEMONS,
    _HEARTBEAT_MAX_AGE_S,
    run_nightly_smoke_checks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(
    tmp_path: Path,
    *,
    beat_offset_s: int = 30,
    include_heartbeat: bool = True,
    include_pending_orders: bool = True,
    include_decisions: bool = True,
    omit_service: str | None = None,
) -> Path:
    """Return path to a minimal SQLite DB.

    beat_offset_s > _HEARTBEAT_MAX_AGE_S triggers stale detection.
    include_* flags control which tables are created.
    omit_service deletes that service's heartbeat row after insert.
    """
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))

    if include_heartbeat:
        conn.execute("""
            CREATE TABLE daemon_heartbeat (
                daemon_name   TEXT PRIMARY KEY,
                last_beat_utc TEXT NOT NULL,
                pid           INTEGER NOT NULL
            )
        """)
    if include_pending_orders:
        conn.execute("""
            CREATE TABLE pending_orders (
                id         INTEGER PRIMARY KEY,
                payload    JSON NOT NULL,
                status     TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        """)
    if include_decisions:
        conn.execute("""
            CREATE TABLE decisions (
                decision_id TEXT PRIMARY KEY,
                engine      TEXT NOT NULL,
                decided_at  TEXT NOT NULL
            )
        """)

    if include_heartbeat:
        now_utc = datetime.now(timezone.utc)
        beat_utc = (now_utc - timedelta(seconds=beat_offset_s)).isoformat()
        for svc in _EXPECTED_DAEMONS:
            conn.execute(
                "INSERT INTO daemon_heartbeat VALUES (?, ?, ?)",
                (svc, beat_utc, 12345),
            )
        if omit_service:
            conn.execute(
                "DELETE FROM daemon_heartbeat WHERE daemon_name = ?",
                (omit_service,),
            )

    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_all_ok_returns_empty(tmp_path):
    db = _make_db(tmp_path, beat_offset_s=30)
    failures = run_nightly_smoke_checks(str(db))
    assert failures == [], failures


def test_stale_heartbeat_detected(tmp_path):
    stale_s = _HEARTBEAT_MAX_AGE_S + 120
    db = _make_db(tmp_path, beat_offset_s=stale_s)
    failures = run_nightly_smoke_checks(str(db))
    assert any("stale" in f for f in failures), failures


def test_missing_service_row_detected(tmp_path):
    db = _make_db(tmp_path, beat_offset_s=30, omit_service=_EXPECTED_DAEMONS[0])
    failures = run_nightly_smoke_checks(str(db))
    assert any("no row" in f for f in failures), failures


def test_missing_heartbeat_table_detected(tmp_path):
    db = _make_db(tmp_path, beat_offset_s=30, include_heartbeat=False)
    failures = run_nightly_smoke_checks(str(db))
    assert any("daemon_heartbeat" in f for f in failures), failures


def test_missing_pending_orders_table_detected(tmp_path):
    db = _make_db(tmp_path, beat_offset_s=30, include_pending_orders=False)
    failures = run_nightly_smoke_checks(str(db))
    assert any("pending_orders" in f for f in failures), failures


def test_missing_decisions_table_detected(tmp_path):
    db = _make_db(tmp_path, beat_offset_s=30, include_decisions=False)
    failures = run_nightly_smoke_checks(str(db))
    assert any("decisions" in f for f in failures), failures
