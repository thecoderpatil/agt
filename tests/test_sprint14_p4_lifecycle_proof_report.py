"""Sprint 14 P4 — proof_report timezone fix + lifecycle partial→filled promotion.

Tests:
  1. _count_pending_in_window: Phase B row with ET-local created_at must NOT be
     misclassified as pre-migration when compared against UTC migration_iso.
  2. _r5_on_exec_details: three PARTIALLY_FILLED callbacks where cumQty reaches
     ordered_qty (remaining stays non-zero throughout) must promote to filled.
  3. VALID_TRANSITIONS sanity: partially_filled → filled is a valid transition.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

import telegram_bot
from agt_equities.order_state import OrderStatus, VALID_TRANSITIONS

pytestmark = pytest.mark.sprint_a

# ---------------------------------------------------------------------------
# Shared schema for proof_report tests (re-uses PHASE_B_SCHEMA columns)
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE pending_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status_history TEXT,
    ib_order_id INTEGER,
    ib_perm_id INTEGER,
    fill_price REAL,
    fill_qty REAL,
    fill_commission REAL,
    fill_time TEXT,
    last_ib_status TEXT,
    client_id INTEGER,
    engine TEXT,
    run_id TEXT,
    broker_mode_at_staging TEXT,
    staged_at_utc TEXT,
    spot_at_staging REAL,
    premium_at_staging REAL,
    submitted_at_utc TEXT,
    spot_at_submission REAL,
    limit_price_at_submission REAL,
    acked_at_utc TEXT,
    gate_verdicts TEXT
);
CREATE TABLE operator_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at_utc TEXT NOT NULL,
    operator_user_id TEXT,
    kind TEXT NOT NULL,
    target_table TEXT,
    target_id INTEGER,
    before_state TEXT,
    after_state TEXT,
    reason TEXT,
    notes TEXT
);
CREATE TABLE master_log_sync (
    sync_id TEXT PRIMARY KEY,
    started_at TEXT,
    status TEXT
);
"""


# ---------------------------------------------------------------------------
# Helpers for lifecycle handler tests (mirrors test_fill_qty_cumulative.py)
# ---------------------------------------------------------------------------

class _ConnProxy:
    def __init__(self, inner):
        self._c = inner

    def execute(self, *a, **kw):
        return self._c.execute(*a, **kw)

    def executemany(self, *a, **kw):
        return self._c.executemany(*a, **kw)

    def commit(self):
        return self._c.commit()

    def rollback(self):
        return self._c.rollback()

    def close(self):
        pass


def _build_lifecycle_db():
    from agt_equities.schema import register_operational_tables, _extend_pending_orders
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    register_operational_tables(c)
    _extend_pending_orders(c)
    c.commit()
    return c


def _make_trade(perm_id, remaining=1):
    return SimpleNamespace(
        order=SimpleNamespace(permId=perm_id, orderId=0, orderRef=""),
        orderStatus=SimpleNamespace(remaining=remaining),
    )


def _make_fill(exec_id, shares, cum_qty, price, avg_price,
               time_str="20260427 14:30:00 ET"):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id,
            shares=float(shares),
            cumQty=float(cum_qty),
            price=float(price),
            avgPrice=float(avg_price),
            time=time_str,
        )
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_count_pending_uses_staged_at_utc_not_created_at():
    """Phase B row with ET-local created_at must NOT be misclassified as pre-migration.

    Bug: created_at stored as ET local ('2026-04-27T09:36:12', no tz suffix) vs
    migration_iso in UTC ('2026-04-27T13:36:12+00:00'). String comparison
    '09:36' >= '13:36' → FALSE, wrongly excludes the row.
    Fix: use staged_at_utc IS NOT NULL AND staged_at_utc >= ? instead.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)

    # Phase B Day 1 row: created_at is ET local, staged_at_utc is UTC
    conn.execute(
        "INSERT INTO pending_orders "
        "(payload, status, created_at, engine, run_id, broker_mode_at_staging, "
        "staged_at_utc, gate_verdicts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "{}",
            "filled",
            "2026-04-27T09:36:12",        # ET local — no tz suffix
            "csp_allocator",
            "run1",
            "paper",
            "2026-04-27T13:36:12+00:00",  # UTC
            json.dumps({"strike_freshness": True}),
        ),
    )
    conn.commit()

    # Window for 2026-04-27 (04:00 EDT = 08:00 UTC)
    start_iso = "2026-04-27T08:00:00+00:00"
    end_iso = "2026-04-28T08:00:00+00:00"
    migration_iso = "2026-04-27T13:36:12+00:00"

    from agt_equities.order_lifecycle.proof_report import _count_pending_in_window
    total, excluded, eligible = _count_pending_in_window(
        conn, start_iso, end_iso, migration_iso
    )

    assert excluded == 0, (
        "Phase B row misclassified as pre-migration: "
        "proof_report must use staged_at_utc (UTC) not created_at (ET local)"
    )
    assert eligible == 1
    conn.close()


def test_partial_fill_promotes_to_filled_at_ordered_qty(monkeypatch):
    """Three PARTIALLY_FILLED callbacks reaching ordered_qty must promote to filled.

    Simulates IB paper engine bug: remaining stays non-zero even when the order
    is fully filled, so new_status = PARTIALLY_FILLED on every callback.
    The promotion block must detect fill_qty >= ordered_qty and call append_status
    to FILLED.
    """
    c = _build_lifecycle_db()

    # Seed order with ordered_qty=5 in payload; initial status 'sent'
    perm_id = 77
    cur = c.execute(
        "INSERT INTO pending_orders (payload, status, ib_perm_id, created_at) "
        "VALUES (?, 'sent', ?, datetime('now'))",
        (json.dumps({"quantity": 5}), perm_id),
    )
    c.commit()
    order_id = int(cur.lastrowid)

    monkeypatch.setattr(telegram_bot, "_get_db_connection", lambda: _ConnProxy(c))

    trade = _make_trade(perm_id=perm_id, remaining=1)  # remaining stays 1 throughout

    # Callback 1: cumQty=2, fill_qty < ordered_qty → no promotion
    telegram_bot._r5_on_exec_details(
        trade, _make_fill("e001", shares=2, cum_qty=2, price=5.40, avg_price=5.40)
    )
    row = c.execute(
        "SELECT status FROM pending_orders WHERE id = ?", (order_id,)
    ).fetchone()
    assert row["status"] == "partially_filled"

    # Callback 2: cumQty=3, still below ordered_qty → no promotion
    telegram_bot._r5_on_exec_details(
        trade, _make_fill("e002", shares=1, cum_qty=3, price=5.35, avg_price=5.37)
    )
    row = c.execute(
        "SELECT status FROM pending_orders WHERE id = ?", (order_id,)
    ).fetchone()
    assert row["status"] == "partially_filled"

    # Callback 3: cumQty=5 == ordered_qty=5 → must promote to filled
    telegram_bot._r5_on_exec_details(
        trade, _make_fill("e003", shares=2, cum_qty=5, price=5.30, avg_price=5.35)
    )
    row = c.execute(
        "SELECT status FROM pending_orders WHERE id = ?", (order_id,)
    ).fetchone()
    assert row["status"] == "filled", (
        f"Expected 'filled' after cumQty reached ordered_qty=5, got '{row['status']}'"
    )
    c.close()


def test_partially_filled_to_filled_is_valid_transition():
    """State machine must permit partially_filled → filled."""
    assert OrderStatus.FILLED in VALID_TRANSITIONS[OrderStatus.PARTIALLY_FILLED]
