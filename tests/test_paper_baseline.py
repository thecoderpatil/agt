
"""Tests for ADR-011 §2 promotion gate DB adapter.

Covers G2 (zero-trip) and G5 (operator override variance) which are the
two implemented gates.  G1 / G3 / G4 stubs are verified to return
insufficient_data without raising.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Minimal DB fixture
# ---------------------------------------------------------------------------
def _make_db(tmp_path: Path) -> str:
    """Return path string to a test SQLite DB with decisions + incidents tables."""
    db = str(tmp_path / "test_gates.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE incidents (
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
            burn_weight REAL NOT NULL DEFAULT 10
        );
        CREATE TABLE decisions (
            decision_id TEXT PRIMARY KEY,
            engine TEXT NOT NULL,
            ticker TEXT NOT NULL,
            decision_timestamp TIMESTAMP NOT NULL,
            raw_input_hash TEXT NOT NULL,
            llm_reasoning_text TEXT,
            llm_confidence_score REAL,
            llm_rank INTEGER,
            operator_action TEXT NOT NULL,
            action_timestamp TIMESTAMP NOT NULL,
            strike REAL,
            expiry DATE,
            contracts INTEGER,
            premium_collected REAL,
            realized_pnl REAL,
            realized_pnl_timestamp TIMESTAMP,
            counterfactual_pnl REAL,
            counterfactual_basis TEXT,
            market_state_embedding BLOB,
            operator_credibility_at_decision REAL,
            prompt_version TEXT NOT NULL,
            notes TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _insert_incident(db: str, severity_tier: int, fault_source: str, days_ago: float = 1.0) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO incidents (incident_key, severity, scrutiny_tier, detector, "
            "detected_at, fault_source, severity_tier, burn_weight) VALUES (?,?,?,?,?,?,?,?)",
            (f"TEST:{ts}", "high", "medium", "test", ts, fault_source, severity_tier, 10.0),
        )
        conn.commit()


def _insert_decision(
    db: str,
    engine: str,
    operator_action: str,
    realized_pnl: float,
    counterfactual_pnl: float,
    days_ago: float = 1.0,
) -> None:
    import uuid
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO decisions (decision_id, engine, ticker, decision_timestamp, "
            "raw_input_hash, operator_action, action_timestamp, prompt_version, "
            "realized_pnl, counterfactual_pnl) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), engine, "AAPL", ts, "hash", operator_action, ts,
             "v1", realized_pnl, counterfactual_pnl),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# G1 / G3 / G4 stubs -- must not raise, must return insufficient_data
# ---------------------------------------------------------------------------
def test_g1_stub_returns_insufficient_data():
    from agt_equities.paper_baseline import evaluate_g1
    result = evaluate_g1("csp_allocator")
    assert result.gate_id == "G1"
    assert result.status == "insufficient_data"


def test_g3_stub_returns_insufficient_data():
    from agt_equities.paper_baseline import evaluate_g3
    result = evaluate_g3("csp_allocator")
    assert result.gate_id == "G3"
    assert result.status == "insufficient_data"


def test_g4_stub_returns_insufficient_data():
    from agt_equities.paper_baseline import evaluate_g4
    result = evaluate_g4("csp_allocator")
    assert result.gate_id == "G4"
    assert result.status == "insufficient_data"


# ---------------------------------------------------------------------------
# G2 tests
# ---------------------------------------------------------------------------
def test_g2_green_no_trips(tmp_path):
    db = _make_db(tmp_path)
    from agt_equities.paper_baseline import evaluate_g2
    result = evaluate_g2("csp_allocator", db_path=db)
    assert result.gate_id == "G2"
    assert result.status == "green"
    assert result.value == 0.0


def test_g2_red_has_tier0_trip(tmp_path):
    db = _make_db(tmp_path)
    _insert_incident(db, severity_tier=0, fault_source="internal", days_ago=2.0)
    from agt_equities.paper_baseline import evaluate_g2
    result = evaluate_g2("csp_allocator", db_path=db)
    assert result.status == "red"
    assert result.value == 1.0


def test_g2_tier2_does_not_count(tmp_path):
    db = _make_db(tmp_path)
    _insert_incident(db, severity_tier=2, fault_source="internal", days_ago=2.0)
    from agt_equities.paper_baseline import evaluate_g2
    result = evaluate_g2("csp_allocator", db_path=db)
    assert result.status == "green", "Tier-2 trips must not block G2"


def test_g2_vendor_trip_does_not_count(tmp_path):
    db = _make_db(tmp_path)
    _insert_incident(db, severity_tier=1, fault_source="vendor", days_ago=2.0)
    from agt_equities.paper_baseline import evaluate_g2
    result = evaluate_g2("csp_allocator", db_path=db)
    assert result.status == "green", "Vendor fault_source must not block G2"


def test_g2_old_trip_outside_window_ignored(tmp_path):
    db = _make_db(tmp_path)
    _insert_incident(db, severity_tier=0, fault_source="internal", days_ago=20.0)
    from agt_equities.paper_baseline import evaluate_g2
    result = evaluate_g2("csp_allocator", window_days=14, db_path=db)
    assert result.status == "green", "Trip older than window must not count"


# ---------------------------------------------------------------------------
# G5 tests
# ---------------------------------------------------------------------------
def test_g5_na_for_no_gate_engine(tmp_path):
    db = _make_db(tmp_path)
    from agt_equities.paper_baseline import evaluate_g5
    for eng in ("cc_exit", "roll_engine", "csp_harvest"):
        result = evaluate_g5(eng, db_path=db)
        assert result.status == "green", f"G5 should be N/A (green) for {eng}"


def test_g5_insufficient_data_below_min_settled(tmp_path):
    db = _make_db(tmp_path)
    for i in range(5):
        _insert_decision(db, "csp_allocator", "approved", 100.0 + i, 90.0 + i)
    from agt_equities.paper_baseline import evaluate_g5
    result = evaluate_g5("csp_allocator", db_path=db)
    assert result.status == "insufficient_data"
    assert result.value == 5.0


def test_g5_green_overrides_dont_beat_engine(tmp_path):
    db = _make_db(tmp_path)
    for i in range(30):
        _insert_decision(db, "csp_allocator", "approved", 50.0, 50.0)
    for i in range(5):
        _insert_decision(db, "csp_allocator", "rejected", -20.0, 50.0)
    from agt_equities.paper_baseline import evaluate_g5
    result = evaluate_g5("csp_allocator", db_path=db)
    assert result.status == "green", f"Override P&L worse than engine -- should be green, got {result}"


def test_g5_red_overrides_beat_engine(tmp_path):
    db = _make_db(tmp_path)
    for i in range(30):
        _insert_decision(db, "csp_allocator", "approved", 50.0, 50.0)
    for i in range(10):
        _insert_decision(db, "csp_allocator", "rejected", 200.0, 10.0)
    from agt_equities.paper_baseline import evaluate_g5
    result = evaluate_g5("csp_allocator", db_path=db)
    assert result.status == "red", f"Operator consistently beats engine -- should be red, got {result}"
    assert result.value is not None and result.value > 1.645