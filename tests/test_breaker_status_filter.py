"""Regression tests for circuit-breaker status filtering.

Bug observed 2026-04-16: check_daily_order_limit and check_daily_notional
counted all pending_orders rows for today, including 'superseded' rows
(prior versions of an order after a price modification). That produced
a false halt when the /report surface showed 35 orders / $2.5M notional
while actual IB-event count was 4 and real committed capital was $204K.

Fix:
  - daily_orders: WHERE ... AND status != 'superseded'
  - daily_notional: WHERE ... AND status IN ('filled','processing','partially_filled')
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.sprint_a


def _make_db(tmp_path: Path) -> Path:
    """Build a minimal pending_orders table with mixed statuses for today."""
    db = tmp_path / "breaker_test.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload JSON NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            ib_order_id INTEGER,
            ib_perm_id INTEGER,
            client_id TEXT DEFAULT 'AGT'
        );
        CREATE TABLE autonomous_session_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            errors TEXT
        );
        CREATE TABLE v_available_nlv AS SELECT 'acct' AS account_id, 1.0 AS nlv WHERE 1=0;
        CREATE TABLE el_snapshots (
            account_id TEXT, nlv REAL, timestamp TEXT
        );
        CREATE TABLE flex_sync_log (sync_type TEXT, completed_at TEXT);
        CREATE TABLE directives (
            is_active INTEGER, created_at TEXT, expires_at TEXT
        );
        """
    )
    now = datetime.now(timezone.utc).isoformat()

    # 31 superseded ($50K strike * 1 qty * 100 each = $50K each = $1.55M phantom)
    for _ in range(31):
        conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, ?, ?)",
            (json.dumps({"strike": 500.0, "quantity": 1}), "superseded", now),
        )
    # 3 filled ($100 strike, 1 qty -> $10K each -> $30K real)
    for _ in range(3):
        conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, ?, ?)",
            (json.dumps({"strike": 100.0, "quantity": 1}), "filled", now),
        )
    # 1 processing ($200 strike, 1 qty -> $20K real)
    conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, ?, ?)",
        (json.dumps({"strike": 200.0, "quantity": 1}), "processing", now),
    )
    # 2 cancelled and 1 failed — should NOT count toward notional, SHOULD count toward orders
    for _ in range(2):
        conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, ?, ?)",
            (json.dumps({"strike": 300.0, "quantity": 1}), "cancelled", now),
        )
    conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, ?, ?)",
        (json.dumps({"strike": 400.0, "quantity": 1}), "failed", now),
    )
    # Insert a fresh directive so check_directive_freshness passes
    conn.execute(
        "INSERT INTO directives (is_active, created_at, expires_at) VALUES (1, ?, ?)",
        (now, (datetime.now(timezone.utc).replace(year=2099)).isoformat()),
    )
    conn.commit()
    conn.close()
    return db


def test_daily_orders_excludes_superseded(tmp_path, monkeypatch):
    """35 rows total, 31 superseded, 4 real IB events + 3 cancelled/failed = 7 counted."""
    db = _make_db(tmp_path)
    from scripts import circuit_breaker as cb

    monkeypatch.setattr(cb, "_get_conn", lambda: sqlite3.connect(
        f"file:{db}?mode=ro", uri=True, detect_types=0
    ))
    # Row factory so cb.check can access row["cnt"]
    orig = cb._get_conn
    def _conn_with_row_factory():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(cb, "_get_conn", _conn_with_row_factory)

    result = cb.check_daily_order_limit()
    # 3 filled + 1 processing + 2 cancelled + 1 failed = 7 real IB events
    # (31 superseded excluded)
    assert result["ok"] is True, f"got {result}"
    assert result["count"] == 7, f"expected 7 non-superseded, got {result['count']}"


def test_daily_notional_only_committed_capital(tmp_path, monkeypatch):
    """Only filled + processing + partially_filled contribute to committed notional."""
    db = _make_db(tmp_path)
    from scripts import circuit_breaker as cb

    def _conn_with_row_factory():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(cb, "_get_conn", _conn_with_row_factory)

    result = cb.check_daily_notional()
    # 3 * ($100 * 1 * 100) + 1 * ($200 * 1 * 100) = $30,000 + $20,000 = $50,000
    # cancelled, failed, superseded all excluded
    assert result["ok"] is True, f"got {result}"
    assert result["notional"] == 50_000.0, (
        f"expected $50K committed, got ${result['notional']:,.0f}"
    )


def test_daily_orders_halts_at_real_limit(tmp_path, monkeypatch):
    """Verify halt still fires when *real* IB events exceed limit."""
    db = tmp_path / "halt_test.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE pending_orders (id INTEGER PRIMARY KEY, payload JSON, "
        "status TEXT, created_at TIMESTAMP);"
    )
    now = datetime.now(timezone.utc).isoformat()
    # 35 real filled orders (none superseded)
    for _ in range(35):
        conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, ?, ?)",
            (json.dumps({"strike": 100, "quantity": 1}), "filled", now),
        )
    conn.commit()
    conn.close()

    from scripts import circuit_breaker as cb
    def _conn():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(cb, "_get_conn", _conn)

    result = cb.check_daily_order_limit()
    assert result["ok"] is False
    assert result.get("halted") is True
    assert "35/30" in result["reason"]
