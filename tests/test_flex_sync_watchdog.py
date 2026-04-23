"""
tests/test_flex_sync_watchdog.py

Sprint 4 MR B — covers agt_equities/flex_sync_watchdog.py per ADR-FLEX_FRESHNESS_v1.
Tests hit the watchdog functions directly against a file-backed sqlite DB
fixture (the tripwire autouse fixture sets AGT_DB_PATH per-test). No touches
to prohibited flex_sync.py.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.sprint_a


def _seed_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS master_log_sync (
            sync_id      TEXT PRIMARY KEY,
            started_at   TEXT NOT NULL,
            status       TEXT NOT NULL,
            rows_received INTEGER
        );
        CREATE TABLE IF NOT EXISTS cross_daemon_alerts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ts   REAL NOT NULL,
            kind         TEXT NOT NULL,
            severity     TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            sent_ts      REAL,
            attempts     INTEGER NOT NULL DEFAULT 0,
            last_error   TEXT
        );
    """)
    conn.commit()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    p = tmp_path / "test_watchdog.db"
    monkeypatch.setenv("AGT_DB_PATH", str(p))
    with sqlite3.connect(str(p)) as conn:
        _seed_schema(conn)
    return str(p)


@pytest.fixture
def sentinel_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect SENTINEL_FILE to a tmp_path so tests don't touch the real
    C:\\AGT_Telegram_Bridge\\state\\ directory."""
    import agt_equities.flex_sync_watchdog as mod
    sentinel_dir = tmp_path / "state"
    sentinel_file = sentinel_dir / "flex_sync_stale.flag"
    monkeypatch.setattr(mod, "SENTINEL_DIR", sentinel_dir)
    monkeypatch.setattr(mod, "SENTINEL_FILE", sentinel_file)
    return sentinel_file


# ---------------------------------------------------------------------------
# query_latest_sync
# ---------------------------------------------------------------------------


def test_query_latest_sync_no_rows(db_path):
    from agt_equities.flex_sync_watchdog import query_latest_sync
    info = query_latest_sync(db_path=db_path)
    assert info["started_at"] is None


def test_query_latest_sync_returns_most_recent_success(db_path):
    from agt_equities.flex_sync_watchdog import query_latest_sync
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO master_log_sync VALUES (?, ?, ?, ?)",
                     ("sync-old", "2026-04-20T16:30:00Z", "success", 100))
        conn.execute("INSERT INTO master_log_sync VALUES (?, ?, ?, ?)",
                     ("sync-new", "2026-04-24T16:30:00Z", "success", 200))
        conn.execute("INSERT INTO master_log_sync VALUES (?, ?, ?, ?)",
                     ("sync-err", "2026-04-24T17:00:00Z", "error", 0))
        conn.commit()
    info = query_latest_sync(db_path=db_path)
    # Latest success is sync-new (error rows are filtered out).
    assert info["sync_id"] == "sync-new"
    assert info["status"] == "success"
    assert info["started_at"] == "2026-04-24T16:30:00Z"


# ---------------------------------------------------------------------------
# run_flex_sync_watchdog
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 4, 24, 22, 0, 0, tzinfo=timezone.utc)  # 18:00 ET


def test_watchdog_fresh_clears_sentinel(db_path, sentinel_tmp):
    from agt_equities.flex_sync_watchdog import run_flex_sync_watchdog

    # Seed sentinel present (from a previous stale run) + fresh sync row
    sentinel_tmp.parent.mkdir(parents=True, exist_ok=True)
    sentinel_tmp.write_text("stale_marker\n")
    with sqlite3.connect(db_path) as conn:
        fresh = (_now() - timedelta(hours=1)).isoformat()
        conn.execute("INSERT INTO master_log_sync VALUES (?, ?, ?, ?)",
                     ("sync-fresh", fresh, "success", 500))
        conn.commit()

    result = run_flex_sync_watchdog(now_utc=_now(), db_path=db_path)
    assert result["status"] == "fresh"
    assert result["age_hours"] < 6
    assert not sentinel_tmp.exists(), "sentinel must be deleted on fresh reading"


def test_watchdog_stale_alerts_and_writes_sentinel(db_path, sentinel_tmp):
    from agt_equities.flex_sync_watchdog import run_flex_sync_watchdog

    # Seed a sync 12 hours old (well past the 6h threshold)
    with sqlite3.connect(db_path) as conn:
        stale = (_now() - timedelta(hours=12)).isoformat()
        conn.execute("INSERT INTO master_log_sync VALUES (?, ?, ?, ?)",
                     ("sync-stale", stale, "success", 500))
        conn.commit()

    result = run_flex_sync_watchdog(now_utc=_now(), db_path=db_path)
    assert result["status"] == "alerted"
    assert result["reason"] == "stale"
    assert result["sentinel_written"] is True
    assert sentinel_tmp.exists()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT kind, severity FROM cross_daemon_alerts WHERE kind = 'FLEX_SYNC_MISSED'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "crit"


def test_watchdog_no_rows_alerts(db_path, sentinel_tmp):
    """No master_log_sync rows at all → crit alert with reason=no_rows."""
    from agt_equities.flex_sync_watchdog import run_flex_sync_watchdog

    result = run_flex_sync_watchdog(now_utc=_now(), db_path=db_path)
    assert result["status"] == "alerted"
    assert result["reason"] == "no_rows"
    assert result["sentinel_written"] is True
    assert sentinel_tmp.exists()


def test_watchdog_custom_threshold(db_path, sentinel_tmp):
    """Threshold override — at 36h, a 12h-old sync is still fresh."""
    from agt_equities.flex_sync_watchdog import run_flex_sync_watchdog

    with sqlite3.connect(db_path) as conn:
        mid = (_now() - timedelta(hours=12)).isoformat()
        conn.execute("INSERT INTO master_log_sync VALUES (?, ?, ?, ?)",
                     ("sync-mid", mid, "success", 500))
        conn.commit()

    result = run_flex_sync_watchdog(
        now_utc=_now(), threshold_hours=36.0, db_path=db_path,
    )
    assert result["status"] == "fresh"


def test_watchdog_idempotency_consecutive_stale_fires_one_alert_per_run(db_path, sentinel_tmp):
    """Two consecutive stale runs enqueue two alerts — the watchdog itself is not
    deduping across runs; the alert consumer or a later ADR handles that."""
    from agt_equities.flex_sync_watchdog import run_flex_sync_watchdog

    with sqlite3.connect(db_path) as conn:
        stale = (_now() - timedelta(hours=12)).isoformat()
        conn.execute("INSERT INTO master_log_sync VALUES (?, ?, ?, ?)",
                     ("sync-stale", stale, "success", 500))
        conn.commit()

    run_flex_sync_watchdog(now_utc=_now(), db_path=db_path)
    run_flex_sync_watchdog(now_utc=_now() + timedelta(hours=1), db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM cross_daemon_alerts WHERE kind = 'FLEX_SYNC_MISSED'"
        ).fetchone()[0]
    # Each cron tick fires its own alert; dedup is the consumer's job
    assert n == 2


# ---------------------------------------------------------------------------
# FLEX_SYNC_MISSED alert rendering
# ---------------------------------------------------------------------------


def test_format_alert_text_flex_sync_missed():
    from agt_equities.alerts import format_alert_text
    text = format_alert_text({
        "kind": "FLEX_SYNC_MISSED",
        "severity": "crit",
        "payload": {
            "age_hours": 12.5,
            "last_sync_utc": "2026-04-24T04:00:00+00:00",
            "threshold_hours": 6.0,
            "sync_id": "sync-abc",
        },
    })
    assert "flex_sync STALE" in text
    assert "12.5" in text
    assert "2026-04-24T04:00:00" in text
    assert "6.0" in text
    assert "/flex_status" in text
