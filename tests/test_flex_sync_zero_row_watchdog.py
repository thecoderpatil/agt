"""
tests/test_flex_sync_zero_row_watchdog.py

Sprint 6 Mega-MR 3 — Flex-sync zero-row suspicion watchdog tests.

The watchdog closes Investigation D Section F3's coverage gap: the
Sprint 4 MR !217 freshness watchdog only detects "sync didn't run"
(>6h stale). The zero-row watchdog detects "sync ran cleanly but
returned 0 rows on a weekday we'd expect rows from, and engines saw
activity anyway". Today's (2026-04-23) sync 17 exhibited this pattern.

Five scenarios covered:
  (a) All-zero window + engine activity -> ALERT
  (b) All-zero window + prior-history above threshold + no activity -> ALERT
  (c) All-zero window + prior-history quiet + no activity -> SKIP
  (d) Any non-zero row in the window -> FRESH (no alert)
  (e) Insufficient history (<5 syncs) -> SKIP (grace period)
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


def _seed_db(db_path: Path) -> None:
    """Create the tables the watchdog reads from."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            "CREATE TABLE master_log_sync ("
            "sync_id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, "
            "from_date TEXT, to_date TEXT, status TEXT, rows_received INTEGER, "
            "rows_inserted INTEGER, rows_updated INTEGER, "
            "error_message TEXT, flex_query_id TEXT, reference_code TEXT, "
            "sections_processed INTEGER)"
        )
        conn.execute(
            "CREATE TABLE pending_orders ("
            "id INTEGER PRIMARY KEY, created_at TEXT, status TEXT, "
            "payload TEXT, ib_order_id INTEGER, ib_perm_id INTEGER, "
            "status_history TEXT, fill_price REAL, fill_qty INTEGER, "
            "fill_commission REAL, fill_time TEXT, last_ib_status TEXT)"
        )
        conn.execute(
            "CREATE TABLE csp_allocator_latest ("
            "id INTEGER PRIMARY KEY, run_id TEXT, trade_date TEXT, "
            "staged_json TEXT, rejected_json TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE incidents ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "invariant_id TEXT NOT NULL, "
            "severity TEXT NOT NULL, "
            "fingerprint TEXT NOT NULL, "
            "description TEXT NOT NULL, "
            "evidence_json TEXT, "
            "first_seen_utc TEXT NOT NULL, "
            "last_seen_utc TEXT NOT NULL, "
            "occurrences INTEGER NOT NULL DEFAULT 1, "
            "state TEXT NOT NULL DEFAULT 'open', "
            "fault_source TEXT, "
            "acknowledged_at_utc TEXT, "
            "acknowledged_by TEXT, "
            "resolution_notes TEXT)"
        )
        conn.execute(
            "CREATE TABLE cross_daemon_alerts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created_ts REAL NOT NULL, "
            "kind TEXT NOT NULL, "
            "severity TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "sent_ts REAL, "
            "attempts INTEGER NOT NULL DEFAULT 0, "
            "last_error TEXT)"
        )
        conn.commit()


def _insert_sync(db_path: Path, sync_id: int, started_at: str, rows_received: int, status: str = "success") -> None:
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            "INSERT INTO master_log_sync "
            "(sync_id, started_at, status, rows_received, rows_inserted, "
            "rows_updated) VALUES (?, ?, ?, ?, 0, 0)",
            (sync_id, started_at, status, rows_received),
        )
        conn.commit()


def _insert_pending_order(db_path: Path, created_at: str) -> None:
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            "INSERT INTO pending_orders (created_at, status, payload) "
            "VALUES (?, 'pending', '{}')",
            (created_at,),
        )
        conn.commit()


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db = tmp_path / "test_zero_row.db"
    _seed_db(db)
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    # Belt-and-braces: also override the module attribute in case
    # agt_equities.db is already imported.
    try:
        from agt_equities import db as _agt_db
        monkeypatch.setattr(_agt_db, "DB_PATH", db, raising=False)
    except ImportError:
        pass
    return db


def test_zero_row_insufficient_history_returns_skip(seeded_db):
    """With fewer than 5 syncs, the watchdog must not alert (grace period)."""
    from agt_equities.flex_sync_watchdog import check_zero_row_suspicion
    _insert_sync(seeded_db, 1, "2026-04-20T21:00:00", 0)
    _insert_sync(seeded_db, 2, "2026-04-21T21:00:00", 0)

    result = check_zero_row_suspicion(db_path=seeded_db)
    assert result["status"] == "insufficient_history", result


def test_zero_row_nonzero_in_window_returns_fresh(seeded_db):
    """Any non-zero row in the window means the pipeline is healthy."""
    from agt_equities.flex_sync_watchdog import check_zero_row_suspicion
    # 7 syncs, last 5 mostly zero but one with rows
    _insert_sync(seeded_db, 1, "2026-04-14T21:00:00", 100)
    _insert_sync(seeded_db, 2, "2026-04-15T21:00:00", 80)
    _insert_sync(seeded_db, 3, "2026-04-16T21:00:00", 0)
    _insert_sync(seeded_db, 4, "2026-04-17T21:00:00", 0)
    _insert_sync(seeded_db, 5, "2026-04-20T21:00:00", 90)  # non-zero in window
    _insert_sync(seeded_db, 6, "2026-04-21T21:00:00", 0)
    _insert_sync(seeded_db, 7, "2026-04-22T21:00:00", 0)

    result = check_zero_row_suspicion(db_path=seeded_db)
    assert result["status"] == "fresh", result


def test_zero_row_all_zero_plus_prior_activity_alerts(seeded_db):
    """All-zero window + prior mean > threshold (no engine activity) = alert."""
    from agt_equities.flex_sync_watchdog import check_zero_row_suspicion
    # Prior 7 days with avg ~100 rows; recent 5 all zero; NO pending_orders
    _insert_sync(seeded_db, 1, "2026-04-13T21:00:00", 100)
    _insert_sync(seeded_db, 2, "2026-04-14T21:00:00", 120)
    _insert_sync(seeded_db, 3, "2026-04-15T21:00:00", 80)
    _insert_sync(seeded_db, 4, "2026-04-16T21:00:00", 90)
    _insert_sync(seeded_db, 5, "2026-04-17T21:00:00", 110)
    _insert_sync(seeded_db, 6, "2026-04-20T21:00:00", 0)
    _insert_sync(seeded_db, 7, "2026-04-21T21:00:00", 0)
    _insert_sync(seeded_db, 8, "2026-04-22T21:00:00", 0)
    _insert_sync(seeded_db, 9, "2026-04-23T21:00:00", 0)
    _insert_sync(seeded_db, 10, "2026-04-24T21:00:00", 0)

    result = check_zero_row_suspicion(db_path=seeded_db)
    assert result["status"] == "alerted", result
    assert any("prior_mean" in r for r in result["reasons"]), result


def test_zero_row_all_zero_plus_engine_activity_alerts(seeded_db):
    """All-zero window + pending_orders during window = alert.

    This is the 'engines traded but flex missed' signal from dispatch.
    """
    from agt_equities.flex_sync_watchdog import check_zero_row_suspicion
    # Prior history quiet; recent window all-zero; BUT pending_orders present
    _insert_sync(seeded_db, 1, "2026-04-13T21:00:00", 0)
    _insert_sync(seeded_db, 2, "2026-04-14T21:00:00", 1)
    _insert_sync(seeded_db, 3, "2026-04-15T21:00:00", 0)
    _insert_sync(seeded_db, 4, "2026-04-16T21:00:00", 2)
    _insert_sync(seeded_db, 5, "2026-04-17T21:00:00", 0)
    _insert_sync(seeded_db, 6, "2026-04-20T21:00:00", 0)
    _insert_sync(seeded_db, 7, "2026-04-21T21:00:00", 0)
    _insert_sync(seeded_db, 8, "2026-04-22T21:00:00", 0)
    _insert_sync(seeded_db, 9, "2026-04-23T21:00:00", 0)
    _insert_sync(seeded_db, 10, "2026-04-24T21:00:00", 0)
    _insert_pending_order(seeded_db, "2026-04-22T14:30:00")  # during window

    result = check_zero_row_suspicion(db_path=seeded_db)
    assert result["status"] == "alerted", result
    assert any("engine_activity" in r for r in result["reasons"]), result


def test_zero_row_all_zero_but_quiet_prior_and_no_activity_skips(seeded_db):
    """All-zero window with truly quiet prior + no activity -> benign skip.

    This is the 'nothing happening anywhere' case; don't flap.
    """
    from agt_equities.flex_sync_watchdog import check_zero_row_suspicion
    # All syncs all zero, no engine activity
    for i, dt in enumerate([
        "2026-04-13T21:00:00", "2026-04-14T21:00:00", "2026-04-15T21:00:00",
        "2026-04-16T21:00:00", "2026-04-17T21:00:00", "2026-04-20T21:00:00",
        "2026-04-21T21:00:00", "2026-04-22T21:00:00", "2026-04-23T21:00:00",
        "2026-04-24T21:00:00",
    ], start=1):
        _insert_sync(seeded_db, i, dt, 0)

    result = check_zero_row_suspicion(db_path=seeded_db)
    assert result["status"] == "all_zero_benign", result
