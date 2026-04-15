"""Decoupling Sprint B Unit B5 -- v_available_nlv view + local margin math.

Scope:
* v_available_nlv SQLite view (registered in schema.register_master_log_tables):
    - Computes per-account available_el / available_nlv from the latest
      el_snapshot minus open (non-terminal) CSP commitment notional drawn
      from pending_order_children JOIN pending_orders.
    - Only SELL PUT children in non-terminal states reduce available_el.
    - BUY orders and non-PUT rights are excluded.
    - available_el / available_nlv floor at 0.0 (never negative).
* csp_allocator.local_margin_check_enabled() flag (AGT_B5_LOCAL_MARGIN_CHECK):
    - Default ON; only literal "0" disables.
* csp_allocator.local_margin_check(conn, account_id, notional):
    - Queries v_available_nlv. Fail-open: no snapshot -> (True, "no_snapshot").
    - Returns (False, reason) when available_el < notional.
    - Never raises; exceptions return (True, "check_error: ...").
* order_state.insert_pending_order_child():
    - Accepts margin_check_status and margin_check_reason kwargs.
    - Persists both columns on the initial INSERT (NULL when not provided).

Tests use in-memory SQLite seeded with register_operational_tables +
register_master_log_tables (which calls _extend_pending_orders internally).
No IB, no network, no real bot process.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn():
    """In-memory DB with all operational + master-log tables including v_available_nlv."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    from agt_equities.schema import register_operational_tables, register_master_log_tables
    register_operational_tables(c)
    register_master_log_tables(c)  # creates el_snapshots + v_available_nlv
    c.commit()
    yield c
    c.close()


def _seed_el_snapshot(conn, account_id: str, nlv: float, el: float,
                      bp: float = 0.0, ts: str = "2026-04-15T10:00:00") -> int:
    cur = conn.execute(
        "INSERT INTO el_snapshots (household, account_id, timestamp, nlv, "
        "excess_liquidity, buying_power) VALUES (?, ?, ?, ?, ?, ?)",
        ("Yash_Household", account_id, ts, nlv, el, bp),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_parent(conn, payload: dict, status: str = "sent") -> int:
    cur = conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at) "
        "VALUES (?, ?, datetime('now'))",
        (json.dumps(payload), status),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_child(conn, parent_id: int, account_id: str, status: str = "sent") -> int:
    cur = conn.execute(
        "INSERT INTO pending_order_children "
        "(parent_order_id, account_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        (parent_id, account_id, status),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# 1. View existence
# ---------------------------------------------------------------------------

def test_view_exists_after_register(conn):
    """register_master_log_tables must create v_available_nlv in sqlite_master."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name='v_available_nlv'"
    ).fetchone()
    assert row is not None, "v_available_nlv view not found after schema registration"


# ---------------------------------------------------------------------------
# 2. Empty baseline
# ---------------------------------------------------------------------------

def test_view_no_rows_when_no_snapshots(conn):
    """With no el_snapshots rows, v_available_nlv returns no rows."""
    rows = conn.execute("SELECT * FROM v_available_nlv").fetchall()
    assert rows == []


# ---------------------------------------------------------------------------
# 3. Full EL when no commitments
# ---------------------------------------------------------------------------

def test_view_full_el_no_commitments(conn):
    """One el_snapshot, no pending children -> available_el == el_snapshot."""
    _seed_el_snapshot(conn, "U21971297", nlv=150_000.0, el=40_000.0)

    row = conn.execute(
        "SELECT * FROM v_available_nlv WHERE account_id = 'U21971297'"
    ).fetchone()
    assert row is not None
    assert float(row["el_snapshot"]) == pytest.approx(40_000.0)
    assert float(row["committed_csp_notional"]) == pytest.approx(0.0)
    assert float(row["available_el"]) == pytest.approx(40_000.0)
    assert float(row["available_nlv"]) == pytest.approx(150_000.0)


# ---------------------------------------------------------------------------
# 4. Open CSP child subtracts from EL
# ---------------------------------------------------------------------------

def test_view_subtracts_open_csp_child(conn):
    """One open SELL PUT child (strike=400, qty=1) reduces available_el by 40000."""
    _seed_el_snapshot(conn, "U21971297", nlv=150_000.0, el=40_000.0)
    payload = {"right": "P", "action": "SELL", "strike": 400.0, "quantity": 1}
    pid = _seed_parent(conn, payload)
    _seed_child(conn, pid, "U21971297", status="sent")

    row = conn.execute(
        "SELECT * FROM v_available_nlv WHERE account_id = 'U21971297'"
    ).fetchone()
    assert float(row["committed_csp_notional"]) == pytest.approx(40_000.0)
    assert float(row["available_el"]) == pytest.approx(0.0)       # 40000 - 40000
    assert float(row["available_nlv"]) == pytest.approx(110_000.0)  # 150000 - 40000


# ---------------------------------------------------------------------------
# 5. Multiple open children same account sum correctly
# ---------------------------------------------------------------------------

def test_view_multiple_children_sum(conn):
    """Two open CSP children for the same account: notional sums."""
    _seed_el_snapshot(conn, "U21971297", nlv=200_000.0, el=80_000.0)
    pid1 = _seed_parent(conn, {"right": "P", "action": "SELL", "strike": 300.0, "quantity": 2})
    pid2 = _seed_parent(conn, {"right": "P", "action": "SELL", "strike": 250.0, "quantity": 1})
    _seed_child(conn, pid1, "U21971297", status="working")
    _seed_child(conn, pid2, "U21971297", status="acked")

    row = conn.execute(
        "SELECT * FROM v_available_nlv WHERE account_id = 'U21971297'"
    ).fetchone()
    # 300*2*100 + 250*1*100 = 60000 + 25000 = 85000
    assert float(row["committed_csp_notional"]) == pytest.approx(85_000.0)
    assert float(row["available_el"]) == pytest.approx(0.0)         # 80000-85000 floors at 0
    assert float(row["available_nlv"]) == pytest.approx(115_000.0)


# ---------------------------------------------------------------------------
# 6. Terminal status children not counted
# ---------------------------------------------------------------------------

def test_view_terminal_status_excluded(conn):
    """Children in terminal states do not reduce available_el."""
    _seed_el_snapshot(conn, "U21971297", nlv=100_000.0, el=30_000.0)
    payload = {"right": "P", "action": "SELL", "strike": 300.0, "quantity": 1}

    for terminal in ("filled", "cancelled", "rejected", "failed",
                     "expired", "rejected_naked", "superseded", "duplicate_skipped"):
        pid = _seed_parent(conn, payload)
        _seed_child(conn, pid, "U21971297", status=terminal)

    row = conn.execute(
        "SELECT * FROM v_available_nlv WHERE account_id = 'U21971297'"
    ).fetchone()
    assert float(row["committed_csp_notional"]) == pytest.approx(0.0)
    assert float(row["available_el"]) == pytest.approx(30_000.0)


# ---------------------------------------------------------------------------
# 7. BUY order excluded
# ---------------------------------------------------------------------------

def test_view_buy_order_excluded(conn):
    """BUY PUT children are not counted (only SELL creates a margin obligation)."""
    _seed_el_snapshot(conn, "U21971297", nlv=100_000.0, el=30_000.0)
    payload = {"right": "P", "action": "BUY", "strike": 300.0, "quantity": 1}
    pid = _seed_parent(conn, payload)
    _seed_child(conn, pid, "U21971297", status="sent")

    row = conn.execute(
        "SELECT * FROM v_available_nlv WHERE account_id = 'U21971297'"
    ).fetchone()
    assert float(row["committed_csp_notional"]) == pytest.approx(0.0)
    assert float(row["available_el"]) == pytest.approx(30_000.0)


# ---------------------------------------------------------------------------
# 8. SELL CALL excluded (only PUT)
# ---------------------------------------------------------------------------

def test_view_sell_call_excluded(conn):
    """SELL CALL children are not counted -- only SELL PUT creates CSP obligation."""
    _seed_el_snapshot(conn, "U21971297", nlv=100_000.0, el=30_000.0)
    payload = {"right": "C", "action": "SELL", "strike": 300.0, "quantity": 1}
    pid = _seed_parent(conn, payload)
    _seed_child(conn, pid, "U21971297", status="working")

    row = conn.execute(
        "SELECT * FROM v_available_nlv WHERE account_id = 'U21971297'"
    ).fetchone()
    assert float(row["committed_csp_notional"]) == pytest.approx(0.0)
    assert float(row["available_el"]) == pytest.approx(30_000.0)


# ---------------------------------------------------------------------------
# 9. Two accounts, independent
# ---------------------------------------------------------------------------

def test_view_two_accounts_independent(conn):
    """Each account's available_el is computed independently."""
    _seed_el_snapshot(conn, "U21971297", nlv=200_000.0, el=60_000.0)
    _seed_el_snapshot(conn, "U22388499", nlv=100_000.0, el=20_000.0)

    pid = _seed_parent(conn, {"right": "P", "action": "SELL", "strike": 200.0, "quantity": 1})
    _seed_child(conn, pid, "U21971297", status="sent")  # U21971297 has 20000 commitment

    rows = {
        row["account_id"]: row
        for row in conn.execute("SELECT * FROM v_available_nlv").fetchall()
    }
    assert "U21971297" in rows and "U22388499" in rows
    assert float(rows["U21971297"]["committed_csp_notional"]) == pytest.approx(20_000.0)
    assert float(rows["U21971297"]["available_el"]) == pytest.approx(40_000.0)
    assert float(rows["U22388499"]["committed_csp_notional"]) == pytest.approx(0.0)
    assert float(rows["U22388499"]["available_el"]) == pytest.approx(20_000.0)


# ---------------------------------------------------------------------------
# 10. available_el floors at zero (never negative)
# ---------------------------------------------------------------------------

def test_view_available_el_floors_at_zero(conn):
    """When committed_notional exceeds el_snapshot, available_el = 0 not negative."""
    _seed_el_snapshot(conn, "U21971297", nlv=100_000.0, el=5_000.0)
    # Commitment 50000 >> el 5000
    payload = {"right": "P", "action": "SELL", "strike": 500.0, "quantity": 1}
    pid = _seed_parent(conn, payload)
    _seed_child(conn, pid, "U21971297", status="sent")

    row = conn.execute(
        "SELECT * FROM v_available_nlv WHERE account_id = 'U21971297'"
    ).fetchone()
    assert float(row["available_el"]) == pytest.approx(0.0)
    assert float(row["available_el"]) >= 0.0  # never negative


# ---------------------------------------------------------------------------
# 11. local_margin_check -- ok case
# ---------------------------------------------------------------------------

def test_local_margin_check_ok(conn):
    """available_el >= notional -> (True, reason containing 'available_el')."""
    from agt_equities.csp_allocator import local_margin_check
    _seed_el_snapshot(conn, "U21971297", nlv=100_000.0, el=50_000.0)

    ok, reason = local_margin_check(conn, "U21971297", 30_000.0)
    assert ok is True
    assert "available_el" in reason


# ---------------------------------------------------------------------------
# 12. local_margin_check -- blocked case
# ---------------------------------------------------------------------------

def test_local_margin_check_blocked(conn):
    """available_el < notional -> (False, reason with $-figures)."""
    from agt_equities.csp_allocator import local_margin_check
    _seed_el_snapshot(conn, "U21971297", nlv=100_000.0, el=10_000.0)

    ok, reason = local_margin_check(conn, "U21971297", 30_000.0)
    assert ok is False
    assert "available_el" in reason
    assert "<" in reason


# ---------------------------------------------------------------------------
# 13. local_margin_check -- no snapshot -> fail-open
# ---------------------------------------------------------------------------

def test_local_margin_check_no_snapshot_failopen(conn):
    """No el_snapshot for account -> (True, 'no_snapshot') -- fail-open."""
    from agt_equities.csp_allocator import local_margin_check

    ok, reason = local_margin_check(conn, "U_UNKNOWN_ACCT", 50_000.0)
    assert ok is True
    assert reason == "no_snapshot"


# ---------------------------------------------------------------------------
# 14-16. local_margin_check_enabled flag semantics
# ---------------------------------------------------------------------------

def test_local_margin_check_enabled_default_on(monkeypatch):
    """Without env override, local_margin_check_enabled() returns True."""
    from agt_equities.csp_allocator import local_margin_check_enabled
    monkeypatch.delenv("AGT_B5_LOCAL_MARGIN_CHECK", raising=False)
    assert local_margin_check_enabled() is True


def test_local_margin_check_flag_zero_disables(monkeypatch):
    """AGT_B5_LOCAL_MARGIN_CHECK=0 -> local_margin_check_enabled() returns False."""
    from agt_equities.csp_allocator import local_margin_check_enabled
    monkeypatch.setenv("AGT_B5_LOCAL_MARGIN_CHECK", "0")
    assert local_margin_check_enabled() is False


def test_local_margin_check_flag_non_zero_stays_on(monkeypatch):
    """Values other than literal '0' keep the check enabled."""
    from agt_equities.csp_allocator import local_margin_check_enabled
    for val in ("1", "true", "yes", "", "off"):
        monkeypatch.setenv("AGT_B5_LOCAL_MARGIN_CHECK", val)
        assert local_margin_check_enabled() is True, f"flag={val!r} should be ON"


# ---------------------------------------------------------------------------
# 17. insert_pending_order_child -- margin_check fields persisted
# ---------------------------------------------------------------------------

def test_insert_child_margin_check_ok_stored(conn):
    """insert_pending_order_child stores margin_check_status='ok' and reason."""
    from agt_equities.order_state import insert_pending_order_child
    pid = _seed_parent(conn, {"right": "P", "action": "SELL", "strike": 300.0, "quantity": 1})

    child_id = insert_pending_order_child(
        conn,
        parent_order_id=pid,
        account_id="U21971297",
        status="sent",
        margin_check_status="ok",
        margin_check_reason="available_el $40,000 >= notional $30,000",
    )
    conn.commit()

    row = conn.execute(
        "SELECT margin_check_status, margin_check_reason "
        "FROM pending_order_children WHERE id = ?",
        (child_id,),
    ).fetchone()
    assert row["margin_check_status"] == "ok"
    assert "available_el" in row["margin_check_reason"]


def test_insert_child_margin_check_blocked_stored(conn):
    """insert_pending_order_child stores 'blocked' status and rejection reason."""
    from agt_equities.order_state import insert_pending_order_child
    pid = _seed_parent(conn, {"right": "P", "action": "SELL", "strike": 300.0, "quantity": 1})

    reason = "available_el $5,000 < notional $30,000 (committed $0)"
    child_id = insert_pending_order_child(
        conn,
        parent_order_id=pid,
        account_id="U21971297",
        status="sent",
        margin_check_status="blocked",
        margin_check_reason=reason,
    )
    conn.commit()

    row = conn.execute(
        "SELECT margin_check_status, margin_check_reason "
        "FROM pending_order_children WHERE id = ?",
        (child_id,),
    ).fetchone()
    assert row["margin_check_status"] == "blocked"
    assert row["margin_check_reason"] == reason


def test_insert_child_null_margin_check_when_not_provided(conn):
    """Without margin_check kwargs, columns are NULL (no regression to B3 callers)."""
    from agt_equities.order_state import insert_pending_order_child
    pid = _seed_parent(conn, {"right": "P", "action": "SELL", "strike": 300.0, "quantity": 1})

    child_id = insert_pending_order_child(
        conn,
        parent_order_id=pid,
        account_id="U21971297",
        status="sent",
    )
    conn.commit()

    row = conn.execute(
        "SELECT margin_check_status, margin_check_reason "
        "FROM pending_order_children WHERE id = ?",
        (child_id,),
    ).fetchone()
    assert row["margin_check_status"] is None
    assert row["margin_check_reason"] is None


# ---------------------------------------------------------------------------
# 20. Latest snapshot wins (multiple snapshots per account)
# ---------------------------------------------------------------------------

def test_view_uses_latest_snapshot(conn):
    """When el_snapshots has multiple rows per account, the most recent wins."""
    _seed_el_snapshot(conn, "U21971297", nlv=100_000.0, el=10_000.0, ts="2026-04-14T09:00:00")
    _seed_el_snapshot(conn, "U21971297", nlv=150_000.0, el=45_000.0, ts="2026-04-15T10:00:00")

    row = conn.execute(
        "SELECT * FROM v_available_nlv WHERE account_id = 'U21971297'"
    ).fetchone()
    assert float(row["el_snapshot"]) == pytest.approx(45_000.0)
    assert float(row["nlv_snapshot"]) == pytest.approx(150_000.0)
