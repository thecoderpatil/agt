"""Sprint 12 P0 — DEFECT-2 fix: sweep_terminal_states() per-row tx_immediate tests.

Verifies that each row sweep is wrapped in its own tx_immediate, bounding the
lock-hold window to a single row regardless of sweep batch size.
See: reports/heartbeat_stale_dual_daemon_20260426.md §5 DEFECT-2.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


def _seed(path: Path, orders: list[dict]) -> None:
    """Create minimal pending_orders schema and insert test rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE pending_orders (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                status_history TEXT DEFAULT '[]',
                last_ib_status TEXT,
                ib_perm_id INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                staged_at_utc TEXT,
                payload TEXT DEFAULT '{}'
            );
            CREATE TABLE cross_daemon_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts REAL,
                kind TEXT,
                severity TEXT,
                payload_json TEXT,
                status TEXT DEFAULT 'pending',
                sent_ts REAL,
                attempts INTEGER DEFAULT 0
            );
            """
        )
        for o in orders:
            conn.execute(
                "INSERT INTO pending_orders (id, status, created_at, staged_at_utc, ib_perm_id, payload) "
                "VALUES (:id, :status, :created_at, :created_at, :ib_perm_id, :payload)",
                o,
            )
        conn.commit()
    finally:
        conn.close()


def _old_timestamp(hours_ago: float = 60) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ---------------------------------------------------------------------------
# Source inspection
# ---------------------------------------------------------------------------

def test_tx_immediate_present_in_sweep_loop():
    """sweep_terminal_states() must call tx_immediate inside the row loop."""
    from agt_equities.order_lifecycle.sweeper import sweep_terminal_states
    source = inspect.getsource(sweep_terminal_states)
    assert "tx_immediate" in source, "sweep_terminal_states must use tx_immediate per row"


def test_no_single_commit_at_end_of_sweep_loop():
    """sweep_terminal_states() must NOT call conn.commit() after the loop — per-row tx handles it."""
    from agt_equities.order_lifecycle.sweeper import sweep_terminal_states
    source = inspect.getsource(sweep_terminal_states)
    assert "conn.commit()" not in source, (
        "sweep_terminal_states must not call conn.commit(); per-row tx_immediate handles commit"
    )


# ---------------------------------------------------------------------------
# Functional: per-row commit correctness
# ---------------------------------------------------------------------------

def test_sweep_single_sent_order(tmp_path):
    """A single sent/no-perm-id order aged >48h is swept to cancelled."""
    db = tmp_path / "sw.db"
    _seed(db, [
        {"id": 1, "status": "sent", "ib_perm_id": 0, "created_at": _old_timestamp(60), "payload": "{}"},
    ])
    from agt_equities.order_lifecycle.sweeper import sweep_terminal_states
    result = sweep_terminal_states(db_path=db)
    assert result.swept_count == 1
    assert result.error_count == 0
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT status FROM pending_orders WHERE id=1").fetchone()
    conn.close()
    assert row[0] == "cancelled"


def test_per_row_commit_partial_success(tmp_path, monkeypatch):
    """Per-row tx_immediate: rows swept before an error are committed; later rows' state is preserved."""
    db = tmp_path / "sw.db"
    now = datetime.now(timezone.utc)
    _seed(db, [
        {"id": 1, "status": "sent", "ib_perm_id": 0, "created_at": _old_timestamp(60), "payload": "{}"},
        {"id": 2, "status": "sent", "ib_perm_id": 0, "created_at": _old_timestamp(60), "payload": "{}"},
        {"id": 3, "status": "sent", "ib_perm_id": 0, "created_at": _old_timestamp(60), "payload": "{}"},
    ])

    call_count = {"n": 0}
    from agt_equities.order_lifecycle import sweeper as sw_mod
    original_apply = sw_mod._apply_sweep

    def patched_apply(conn, *, order_id, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("injected failure on row 2")
        original_apply(conn, order_id=order_id, **kwargs)

    monkeypatch.setattr(sw_mod, "_apply_sweep", patched_apply)

    result = sweep_terminal_states = sw_mod.sweep_terminal_states
    res = sweep_terminal_states(db_path=db)

    assert res.error_count == 1
    assert res.swept_count == 2  # rows 1 and 3 succeeded

    conn = sqlite3.connect(str(db))
    statuses = {r[0]: r[1] for r in conn.execute("SELECT id, status FROM pending_orders").fetchall()}
    conn.close()
    # Row 1 swept (first)
    assert statuses[1] == "cancelled"
    # Row 2 not swept (error), remains sent
    assert statuses[2] == "sent"
    # Row 3 swept (third)
    assert statuses[3] == "cancelled"


def test_sweep_large_n_completes(tmp_path):
    """100 sent/aged orders are all swept successfully via per-row tx_immediate."""
    db = tmp_path / "sw.db"
    orders = [
        {"id": i, "status": "sent", "ib_perm_id": 0, "created_at": _old_timestamp(60), "payload": "{}"}
        for i in range(1, 101)
    ]
    _seed(db, orders)
    from agt_equities.order_lifecycle.sweeper import sweep_terminal_states
    start = time.monotonic()
    result = sweep_terminal_states(db_path=db)
    elapsed = time.monotonic() - start
    assert result.swept_count == 100
    assert result.error_count == 0
    # 100 per-row tx_immediate commits must finish in < 10s (generous for CI)
    assert elapsed < 10.0, f"sweep of 100 rows took {elapsed:.1f}s — per-row tx too slow"


def test_sweep_result_classification_counts(tmp_path):
    """by_classification counts reflect correct rule assignment."""
    db = tmp_path / "sw.db"
    _seed(db, [
        # Rule 3: sent, no perm_id, >48h
        {"id": 1, "status": "sent", "ib_perm_id": 0, "created_at": _old_timestamp(60), "payload": "{}"},
        {"id": 2, "status": "sent", "ib_perm_id": 0, "created_at": _old_timestamp(72), "payload": "{}"},
        # Rule 2: pending, no history, >24h
        {"id": 3, "status": "pending", "ib_perm_id": 0, "created_at": _old_timestamp(30), "payload": "{}"},
    ])
    from agt_equities.order_lifecycle.sweeper import sweep_terminal_states
    result = sweep_terminal_states(db_path=db)
    assert result.swept_count == 3
    assert result.by_classification.get("no_ib_perm_id", 0) == 2
    assert result.by_classification.get("never_sent_to_ib", 0) == 1
