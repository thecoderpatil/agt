"""
tests/test_known_trading_day_partial_fill.py

Sprint 9 Item 4 — _is_known_trading_day widens Evidence A to accept
status='partially_filled' in addition to 'filled'.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


def _seed_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload TEXT,
            status TEXT NOT NULL,
            fill_time TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS daemon_heartbeat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            daemon_name TEXT NOT NULL,
            last_beat_utc TEXT NOT NULL
        );
    """)
    conn.commit()


@pytest.fixture
def db_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    p = tmp_path / "test_known_td.db"
    monkeypatch.setenv("AGT_DB_PATH", str(p))
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _seed_schema(conn)
    return conn


def test_partially_filled_counts_as_evidence_a(db_conn: sqlite3.Connection) -> None:
    """A partially_filled row with fill_time on D classifies D as a known trading day."""
    from agt_equities.flex_sync import _is_known_trading_day

    db_conn.execute(
        "INSERT INTO pending_orders (payload, status, fill_time) VALUES (?, ?, ?)",
        ('{"account_id":"U22076329"}', "partially_filled", "2026-04-24T18:33:00"),
    )
    db_conn.commit()

    assert _is_known_trading_day("20260424", conn=db_conn) is True


def test_partially_filled_wrong_date_not_evidence_a(db_conn: sqlite3.Connection) -> None:
    """A partially_filled row on a different date is not Evidence A for D."""
    from agt_equities.flex_sync import _is_known_trading_day

    db_conn.execute(
        "INSERT INTO pending_orders (payload, status, fill_time) VALUES (?, ?, ?)",
        ('{"account_id":"U22076329"}', "partially_filled", "2026-04-23T18:33:00"),
    )
    db_conn.commit()

    # Evidence A: no match on 2026-04-24. Evidence B: no RTH heartbeat.
    assert _is_known_trading_day("20260424", conn=db_conn) is False
