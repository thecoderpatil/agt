"""ADR-020 Phase A piece 3 — terminal-state sweeper tests."""
import json
import sqlite3
import pytest
from datetime import datetime, timedelta, timezone

from agt_equities.order_lifecycle.sweeper import (
    _classify_stuck_order,
    sweep_terminal_states,
    SweepResult,
    STUCK_PENDING_AGE_HOURS,
    STUCK_SENT_NO_PERM_ID_AGE_HOURS,
    STUCK_SENT_WITH_PERM_ID_AGE_HOURS,
    TERMINAL_STATES,
)


# ---------------------------------------------------------------------------
# Unit tests — _classify_stuck_order (pure function, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.sprint_a
def test_expired_option_no_callback_marked_expired():
    """Expired option past grace window is classified as 'expired'."""
    now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    order = {
        "status": "sent",
        "created_at": "2026-04-23T12:00:00+00:00",
        "expiry": "20260424",
        "right": "P",
        "ib_perm_id": 75,
        "_has_status_history": True,
    }
    result = _classify_stuck_order(order_row=order, now_utc=now)
    assert result is not None
    state, reason, evidence = result
    assert state == "expired"
    assert reason == "expiry_passed_no_callback"
    assert evidence["expiry"] == "20260424"
    assert evidence["ib_perm_id"] == 75
    assert evidence["age_hours"] > 0


@pytest.mark.sprint_a
def test_pending_no_history_marked_cancelled_never_sent():
    """Pending order with no status_history past threshold is 'cancelled'."""
    now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    order = {
        "status": "pending",
        "created_at": "2026-04-22T15:00:00+00:00",  # 69h old
        "expiry": "20260508",
        "right": "C",
        "ib_perm_id": 0,
        "_has_status_history": False,
    }
    result = _classify_stuck_order(order_row=order, now_utc=now)
    assert result is not None
    state, reason, _ = result
    assert state == "cancelled"
    assert reason == "never_sent_to_ib"


@pytest.mark.sprint_a
def test_sent_no_perm_id_marked_cancelled():
    """Sent order with ib_perm_id=0 past 48h threshold is 'cancelled'."""
    now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    order = {
        "status": "sent",
        "created_at": "2026-04-25T00:00:00+00:00",  # 60h old
        "expiry": "20260508",
        "right": "P",
        "ib_perm_id": 0,
        "_has_status_history": True,
    }
    result = _classify_stuck_order(order_row=order, now_utc=now)
    assert result is not None
    state, reason, _ = result
    assert state == "cancelled"
    assert reason == "no_ib_perm_id"


@pytest.mark.sprint_a
def test_sent_with_perm_id_no_callback_marked_cancelled():
    """Sent order with perm_id but no callback past 96h is 'cancelled'."""
    now = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    order = {
        "status": "sent",
        "created_at": "2026-04-25T12:00:00+00:00",  # 120h old
        "expiry": "20260508",
        "right": "P",
        "ib_perm_id": 75,
        "_has_status_history": True,
    }
    result = _classify_stuck_order(order_row=order, now_utc=now)
    assert result is not None
    state, reason, _ = result
    assert state == "cancelled"
    assert reason == "no_ib_callback"
    assert result[2]["ib_perm_id"] == 75


@pytest.mark.sprint_a
def test_legitimate_in_flight_order_not_swept():
    """Recent sent order with perm_id is NOT swept — still in-flight."""
    now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    order = {
        "status": "sent",
        "created_at": "2026-04-25T10:00:00+00:00",  # 2h old
        "expiry": "20260508",
        "right": "P",
        "ib_perm_id": 75,
        "_has_status_history": True,
    }
    assert _classify_stuck_order(order_row=order, now_utc=now) is None


@pytest.mark.sprint_a
def test_terminal_state_orders_not_swept():
    """All terminal-state orders return None regardless of age or expiry."""
    now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    for terminal in ["filled", "cancelled", "expired", "rejected", "superseded"]:
        order = {
            "status": terminal,
            "created_at": "2026-04-20T00:00:00+00:00",
            "expiry": "20260424",
            "right": "P",
            "ib_perm_id": 75,
            "_has_status_history": True,
        }
        assert _classify_stuck_order(order_row=order, now_utc=now) is None, (
            f"Terminal state {terminal!r} should not be swept"
        )


# ---------------------------------------------------------------------------
# Integration test — sweep_terminal_states with tmp SQLite fixture
# ---------------------------------------------------------------------------


def _make_test_db(db_file: str) -> None:
    """Create a minimal pending_orders table for testing."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE pending_orders (
            id INTEGER PRIMARY KEY,
            payload JSON NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            ib_perm_id INTEGER,
            status_history TEXT,
            ib_order_id INTEGER,
            fill_price REAL,
            fill_qty REAL,
            fill_commission REAL,
            fill_time TIMESTAMP,
            last_ib_status TEXT
        )
    """)
    conn.commit()
    conn.close()


def _insert_order(db_file: str, row_id: int, payload: dict, status: str,
                  created_at: str, ib_perm_id: int, status_history) -> None:
    conn = sqlite3.connect(db_file)
    hist = json.dumps(status_history) if status_history is not None else None
    conn.execute(
        "INSERT INTO pending_orders "
        "(id, payload, status, created_at, ib_perm_id, status_history) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row_id, json.dumps(payload), status, created_at, ib_perm_id, hist),
    )
    conn.commit()
    conn.close()


@pytest.mark.sprint_a
def test_now_utc_dependency_injection(tmp_path):
    """End-to-end: sweep_terminal_states honors injected now_utc and writes
    correct status + status_history to a real SQLite fixture."""
    db_file = str(tmp_path / "test_sweep.db")
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    _make_test_db(db_file)

    # Row 1: expired option past grace window -> 'expired'
    _insert_order(db_file, 1, {"expiry": "20260424", "right": "P"}, "sent",
                  "2026-04-23T12:00:00+00:00", 75, [])

    # Row 2: pending, no history, 69h old -> 'cancelled' (never_sent_to_ib)
    _insert_order(db_file, 2, {"expiry": "20260508", "right": "C"}, "pending",
                  "2026-04-22T15:00:00+00:00", 0, None)

    # Row 3: sent, no perm_id, 60h old -> 'cancelled' (no_ib_perm_id)
    _insert_order(db_file, 3, {"expiry": "20260508", "right": "P"}, "sent",
                  "2026-04-25T00:00:00+00:00", 0, [])

    # Row 4: recent sent in-flight, 2h old -> NOT swept
    _insert_order(db_file, 4, {"expiry": "20260508", "right": "P"}, "sent",
                  "2026-04-27T10:00:00+00:00", 99, [])

    # Row 5: already terminal -> NOT swept
    _insert_order(db_file, 5, {"expiry": "20260424", "right": "P"}, "filled",
                  "2026-04-20T00:00:00+00:00", 75, [{"status": "filled", "at": "2026-04-24T20:00:00+00:00", "by": "ib"}])

    result = sweep_terminal_states(db_path=db_file, now_utc=fixed_now)

    assert result.swept_count == 3
    assert result.skipped_in_flight == 1
    assert result.error_count == 0
    assert result.by_classification.get("expiry_passed_no_callback") == 1
    assert result.by_classification.get("never_sent_to_ib") == 1
    assert result.by_classification.get("no_ib_perm_id") == 1

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM pending_orders").fetchall()}
    conn.close()

    assert rows[1]["status"] == "expired"
    assert rows[2]["status"] == "cancelled"
    assert rows[3]["status"] == "cancelled"
    assert rows[4]["status"] == "sent"   # untouched
    assert rows[5]["status"] == "filled"  # untouched

    hist1 = json.loads(rows[1]["status_history"])
    assert hist1[-1]["status"] == "expired"
    assert hist1[-1]["by"] == "terminal_state_sweeper"
    assert hist1[-1]["payload"]["reason"] == "expiry_passed_no_callback"

    hist2 = json.loads(rows[2]["status_history"])
    assert hist2[-1]["status"] == "cancelled"
    assert hist2[-1]["payload"]["reason"] == "never_sent_to_ib"

    hist3 = json.loads(rows[3]["status_history"])
    assert hist3[-1]["status"] == "cancelled"
    assert hist3[-1]["payload"]["reason"] == "no_ib_perm_id"

    # Running twice with the same now_utc should not re-sweep already-terminal rows
    result2 = sweep_terminal_states(db_path=db_file, now_utc=fixed_now)
    assert result2.swept_count == 0
