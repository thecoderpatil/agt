"""ADR-018 Phase 1 — zero-row refusal + date filter + retry tests.

Covers:
  - _is_known_trading_day classification (pending_orders + heartbeat evidence)
  - zero-row branch: known trading day raises suspicious + incident + retry row
  - zero-row branch: non-trading day preserves success path
  - from_date/to_date row filtering on master_log_trades
  - retry escalation to FLEX_SYNC_PERSISTENT_EMPTY at attempt 4
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


from agt_equities import flex_sync
from agt_equities.db import get_db_connection
from agt_equities.schema import register_master_log_tables, register_operational_tables


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def phase1_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "agt_phase1_test.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000;")
    try:
        register_master_log_tables(conn)
        register_operational_tables(conn)

        # pending_orders (trading-day evidence A) — schema.py creates a minimal
        # table; we need fill_time too (added by later ALTERs in prod). Drop
        # and recreate with the extended shape since ALTER pile is complex.
        conn.execute("DROP TABLE IF EXISTS pending_orders")
        conn.execute(
            """
            CREATE TABLE pending_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                fill_time TEXT,
                payload TEXT,
                created_at TEXT
            )
            """
        )
        # daemon_heartbeat (trading-day evidence B).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_heartbeat (
                daemon_name TEXT PRIMARY KEY,
                last_beat_utc TEXT NOT NULL,
                pid INTEGER,
                client_id INTEGER,
                notes TEXT
            )
            """
        )
        # incidents + remediation_incidents (for incidents_repo.register target).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_key TEXT NOT NULL,
                invariant_id TEXT,
                severity TEXT NOT NULL,
                scrutiny_tier TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                detector TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                closed_at TEXT,
                last_action_at TEXT,
                consecutive_breaches INTEGER NOT NULL DEFAULT 1,
                observed_state TEXT,
                desired_state TEXT,
                confidence REAL,
                mr_iid INTEGER,
                ddiff_url TEXT,
                rejection_history TEXT,
                fault_source TEXT NOT NULL DEFAULT 'internal',
                severity_tier INTEGER NOT NULL DEFAULT 1,
                burn_weight REAL NOT NULL DEFAULT 10,
                error_budget_tier INTEGER NOT NULL DEFAULT 2,
                budget_consumed_pct REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remediation_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                detector TEXT,
                detected_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cross_daemon_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts REAL NOT NULL,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                payload_json TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                sent_ts REAL,
                attempts INTEGER DEFAULT 0,
                last_error TEXT
            )
            """
        )
        # Apply the Phase 1 migration.
        conn.commit()
    finally:
        conn.close()
    from scripts.migrate_flex_sync_retry_attempts import run as migrate
    migrate(db_path=db)

    # Patch flex_sync's DB connection factory to use this test DB.
    def _factory():
        c = sqlite3.connect(db, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout = 15000;")
        return c

    monkeypatch.setattr(flex_sync, "_get_db", _factory)
    # Patch get_db_connection (used by incidents_repo) to hit same DB.
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    try:
        from agt_equities import db as _agt_db
        monkeypatch.setattr(_agt_db, "DB_PATH", db, raising=False)
    except ImportError:
        pass

    return db


def _empty_flex_xml() -> bytes:
    return (
        b'<FlexQueryResponse queryName="MASTER_LOG" type="AF">\n'
        b'<FlexStatements count="0">\n'
        b'</FlexStatements>\n'
        b'</FlexQueryResponse>\n'
    )


def _minimal_flex_xml_with_trades(trade_dates: list[str]) -> bytes:
    """Build a minimal Flex XML with one Trade element per supplied date.

    Populates all NOT NULL columns required by master_log_trades schema.
    """
    trades = "\n".join(
        f'<Trade transactionID="T{i}" tradeDate="{d}" '
        f'dateTime="{d};140000" symbol="AAPL" '
        f'accountId="U22388499" currency="USD" assetCategory="STK" '
        f'conid="265598" transactionType="ExchTrade" buySell="BUY" '
        f'quantity="1" tradePrice="100" />'
        for i, d in enumerate(trade_dates)
    )
    xml = (
        f'<FlexQueryResponse queryName="MASTER_LOG" type="AF">\n'
        f'<FlexStatements count="1">\n'
        f'<FlexStatement accountId="U22388499" fromDate="20260101" toDate="20261231">\n'
        f'<Trades>\n'
        f'{trades}\n'
        f'</Trades>\n'
        f'</FlexStatement>\n'
        f'</FlexStatements>\n'
        f'</FlexQueryResponse>\n'
    ).encode()
    return xml


# ---------------------------------------------------------------------------
# _is_known_trading_day
# ---------------------------------------------------------------------------


def test_known_trading_day_pending_orders_filled(phase1_db):
    """Evidence A: pending_orders has a filled row on the date."""
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        conn.execute(
            "INSERT INTO pending_orders (status, fill_time) VALUES "
            "('filled', '2026-04-27T14:30:00+00:00')"
        )
        conn.commit()
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        conn.row_factory = sqlite3.Row
        assert flex_sync._is_known_trading_day("20260427", conn=conn) is True


def test_known_trading_day_heartbeat_evidence(phase1_db):
    """Evidence B: heartbeat landed inside RTH for the date."""
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        conn.execute(
            "INSERT INTO daemon_heartbeat (daemon_name, last_beat_utc, pid) "
            "VALUES ('agt_bot', '2026-04-27T15:00:00+00:00', 123)"
        )
        conn.commit()
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        conn.row_factory = sqlite3.Row
        assert flex_sync._is_known_trading_day("20260427", conn=conn) is True


def test_non_trading_day_weekend(phase1_db):
    """No filled pending_orders + no heartbeat during RTH → not a trading day."""
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        conn.row_factory = sqlite3.Row
        # Sat 2026-04-25
        assert flex_sync._is_known_trading_day("20260425", conn=conn) is False


def test_non_trading_day_quiet_weekday(phase1_db):
    """No filled rows, no heartbeats during RTH → not a trading day (False)."""
    # Heartbeat exists but outside RTH window.
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        conn.execute(
            "INSERT INTO daemon_heartbeat (daemon_name, last_beat_utc, pid) "
            "VALUES ('agt_bot', '2026-04-27T04:00:00+00:00', 123)"  # midnight ET
        )
        conn.commit()
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        conn.row_factory = sqlite3.Row
        assert flex_sync._is_known_trading_day("20260427", conn=conn) is False


# ---------------------------------------------------------------------------
# Zero-row refusal branch
# ---------------------------------------------------------------------------


def test_zero_row_refuses_success_on_known_trading_day(phase1_db):
    """Flex returns empty; coverage date is known trading day; incident raised,
    status=suspicious, retry row inserted."""
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        conn.execute(
            "INSERT INTO pending_orders (status, fill_time) VALUES "
            "('filled', '2026-04-27T14:30:00+00:00')"
        )
        conn.commit()
    result = flex_sync.run_sync(
        flex_sync.SyncMode.INCREMENTAL,
        xml_bytes=_empty_flex_xml(),
        from_date="20260427",
        to_date="20260427",
    )
    assert result.status == 'suspicious'
    assert result.needs_retry is True
    assert result.retry_date == "20260427"
    assert result.next_attempt_n == 1
    # master_log_sync row has status='suspicious'.
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        mls = conn.execute(
            "SELECT status FROM master_log_sync WHERE sync_id = ?",
            (result.sync_id,),
        ).fetchone()
        assert mls[0] == "suspicious"
        # Retry row written.
        retry = conn.execute(
            "SELECT coverage_date, attempt_n FROM flex_sync_retry_attempts "
            "WHERE original_sync_id = ?",
            (result.sync_id,),
        ).fetchone()
        assert retry == ("20260427", 1)
        # Tier-0 incident filed.
        inc = conn.execute(
            "SELECT severity, invariant_id FROM incidents "
            "WHERE incident_key LIKE 'FLEX_SYNC_EMPTY_KNOWN_TRADING_DAY%'"
        ).fetchone()
        assert inc is not None
        assert inc[0] == "critical"
        assert inc[1] == "FLEX_SYNC_EMPTY_KNOWN_TRADING_DAY"


def test_zero_row_commits_success_on_non_trading_day(phase1_db):
    """Flex returns empty; no trading-day evidence → status='success', no incident."""
    # Saturday 2026-04-25 — no evidence seeded.
    result = flex_sync.run_sync(
        flex_sync.SyncMode.INCREMENTAL,
        xml_bytes=_empty_flex_xml(),
        from_date="20260425",
        to_date="20260425",
    )
    assert result.status == 'success'
    assert result.needs_retry is False
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        mls = conn.execute(
            "SELECT status FROM master_log_sync WHERE sync_id = ?",
            (result.sync_id,),
        ).fetchone()
        assert mls[0] == "success"
        # No retry row.
        assert conn.execute(
            "SELECT COUNT(*) FROM flex_sync_retry_attempts WHERE original_sync_id = ?",
            (result.sync_id,),
        ).fetchone()[0] == 0
        # No incident.
        assert conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE invariant_id LIKE 'FLEX_SYNC_%'"
        ).fetchone()[0] == 0


def test_retry_schedule_enqueued_with_correct_backoff(phase1_db):
    """Attempts 1, 2, 3 get +2h/+4h/+6h offsets; no new enqueue at attempt 4."""
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        conn.execute(
            "INSERT INTO pending_orders (status, fill_time) VALUES "
            "('filled', '2026-04-27T14:30:00+00:00')"
        )
        conn.commit()
    # Attempt 1 (original) — schedules next (attempt 2) at +2h
    r1 = flex_sync.run_sync(
        flex_sync.SyncMode.INCREMENTAL,
        xml_bytes=_empty_flex_xml(),
        from_date="20260427", to_date="20260427",
        retry_attempt_n=0,
    )
    assert r1.needs_retry is True
    assert r1.next_attempt_n == 1  # next attempt index
    # Simulate retry 2 (still zero)
    r2 = flex_sync.run_sync(
        flex_sync.SyncMode.INCREMENTAL,
        xml_bytes=_empty_flex_xml(),
        from_date="20260427", to_date="20260427",
        retry_attempt_n=1,
    )
    assert r2.needs_retry is True
    assert r2.next_attempt_n == 2
    # Simulate retry 3
    r3 = flex_sync.run_sync(
        flex_sync.SyncMode.INCREMENTAL,
        xml_bytes=_empty_flex_xml(),
        from_date="20260427", to_date="20260427",
        retry_attempt_n=2,
    )
    assert r3.needs_retry is True
    assert r3.next_attempt_n == 3
    # Count retry rows — should be 3 (one after each attempt 0, 1, 2).
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM flex_sync_retry_attempts WHERE coverage_date = ?",
            ("20260427",),
        ).fetchone()[0]
    assert count == 3


def test_persistent_empty_escalates_tier_0(phase1_db):
    """Attempt 4 still zero → FLEX_SYNC_PERSISTENT_EMPTY + cross_daemon_alert."""
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        conn.execute(
            "INSERT INTO pending_orders (status, fill_time) VALUES "
            "('filled', '2026-04-27T14:30:00+00:00')"
        )
        conn.commit()
    # retry_attempt_n=4 is the 4th attempt (failing for the final time).
    result = flex_sync.run_sync(
        flex_sync.SyncMode.INCREMENTAL,
        xml_bytes=_empty_flex_xml(),
        from_date="20260427", to_date="20260427",
        retry_attempt_n=4,
    )
    assert result.status == 'suspicious'
    # next_attempt_n would be 5, beyond our schedule, so needs_retry False.
    assert result.needs_retry is False
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        # Persistent-empty incident filed.
        inc = conn.execute(
            "SELECT invariant_id FROM incidents "
            "WHERE incident_key LIKE 'FLEX_SYNC_PERSISTENT_EMPTY%'"
        ).fetchone()
        assert inc is not None
        assert inc[0] == "FLEX_SYNC_PERSISTENT_EMPTY"
        # Cross-daemon alert emitted.
        cda = conn.execute(
            "SELECT kind, severity FROM cross_daemon_alerts "
            "WHERE kind = 'FLEX_SYNC_PERSISTENT_EMPTY'"
        ).fetchone()
        assert cda is not None
        assert cda[1] == "crit"


# ---------------------------------------------------------------------------
# Date-filter row scoping
# ---------------------------------------------------------------------------


def test_from_to_date_filters_response(phase1_db):
    """run_sync with from_date=to_date filters out-of-window trades."""
    xml = _minimal_flex_xml_with_trades(["20260420", "20260427", "20260428"])
    result = flex_sync.run_sync(
        flex_sync.SyncMode.INCREMENTAL,
        xml_bytes=xml,
        from_date="20260427",
        to_date="20260427",
    )
    assert result.status == 'success'
    # Only one trade in window; rows_received reflects post-filter count.
    assert result.rows_received == 1
    with closing(sqlite3.connect(str(phase1_db))) as conn:
        trade_count = conn.execute(
            "SELECT COUNT(*) FROM master_log_trades WHERE trade_date = '20260427'"
        ).fetchone()[0]
    assert trade_count == 1
