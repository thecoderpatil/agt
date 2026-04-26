"""Per-metric unit tests for proof_report. Each test isolates one metric path."""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3

import pytest

pytestmark = pytest.mark.sprint_a


PHASE_B_SCHEMA = """
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
CREATE TABLE daemon_heartbeat_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    daemon_name TEXT NOT NULL,
    beat_utc TEXT NOT NULL,
    pid INTEGER NOT NULL,
    client_id INTEGER,
    notes TEXT
);
CREATE TABLE master_log_sync (
    sync_id TEXT PRIMARY KEY,
    started_at TEXT,
    status TEXT
);
CREATE TABLE cross_daemon_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    severity TEXT,
    payload TEXT,
    created_ts REAL
);
"""

REPORT_DATE = "2026-04-26"


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db = tmp_path / "agt.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(PHASE_B_SCHEMA)
    # Mark migration "complete" by inserting an enriched row early in window.
    conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at, engine, run_id, "
        "broker_mode_at_staging, staged_at_utc, gate_verdicts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("{}", "filled", "2026-04-26T13:00:00+00:00", "csp_allocator", "r1",
         "paper", "2026-04-26T13:00:00+00:00", json.dumps({"strike_freshness": True})),
    )
    conn.execute(
        "INSERT INTO master_log_sync (sync_id, started_at, status) VALUES (?, ?, ?)",
        ("s1", "2026-04-27T11:00:00+00:00", "success"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    from agt_equities import db as agt_db
    monkeypatch.setattr(agt_db, "DB_PATH", str(db))
    return db


def _generate(db_path):
    from agt_equities.order_lifecycle import proof_report
    return proof_report.generate_proof_report(
        report_date_et=REPORT_DATE, is_preview=False, db_path=str(db_path),
    )


def test_pass_baseline_with_one_filled(seeded_db):
    rep = _generate(seeded_db)
    assert rep.verdict == "PASS"
    assert rep.metrics["pct_same_day_terminal"] == 100.0
    assert rep.metrics["walker_reconstruction_defects"] == 0
    assert rep.metrics["heartbeat_gaps_over_180s"] == 0


def test_route_mismatch_metric(seeded_db):
    conn = sqlite3.connect(str(seeded_db))
    conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at, engine, run_id, "
        "broker_mode_at_staging, staged_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (json.dumps({"account_mode": "live"}), "filled", "2026-04-26T14:00:00+00:00",
         "cc_engine", "r2", "paper", "2026-04-26T14:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    rep = _generate(seeded_db)
    assert rep.metrics["route_mismatches"] == 1
    assert rep.verdict == "FAIL"


def test_stale_strike_metric(seeded_db):
    conn = sqlite3.connect(str(seeded_db))
    conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at, engine, run_id, "
        "broker_mode_at_staging, staged_at_utc, gate_verdicts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("{}", "filled", "2026-04-26T15:00:00+00:00", "cc_engine", "r3",
         "paper", "2026-04-26T15:00:00+00:00",
         json.dumps({"strike_freshness": False})),
    )
    conn.commit()
    conn.close()
    rep = _generate(seeded_db)
    assert rep.metrics["stale_strike_submissions_succeeded"] == 1
    assert rep.verdict == "FAIL"


def test_operator_intervention_metric(seeded_db):
    conn = sqlite3.connect(str(seeded_db))
    conn.execute(
        "INSERT INTO operator_interventions (occurred_at_utc, kind) VALUES (?, ?)",
        ("2026-04-26T16:00:00+00:00", "direct_sql"),
    )
    conn.commit()
    conn.close()
    rep = _generate(seeded_db)
    assert rep.metrics["direct_db_or_manual_interventions"] == 1
    assert rep.verdict == "FAIL"


def test_tier_incident_metric(seeded_db):
    conn = sqlite3.connect(str(seeded_db))
    # Insert a tier_0 alert in the window (2026-04-26 04:00 ET = 08:00 UTC)
    epoch = _dt.datetime(2026, 4, 26, 14, 0, tzinfo=_dt.timezone.utc).timestamp()
    conn.execute(
        "INSERT INTO cross_daemon_alerts (kind, severity, payload, created_ts) "
        "VALUES (?, ?, ?, ?)",
        ("STUCK_ORDER_SWEEP", "tier_0", "{}", epoch),
    )
    conn.commit()
    conn.close()
    rep = _generate(seeded_db)
    assert rep.metrics["tier_0_or_tier_1_incidents"] == 1


def test_pending_flex_when_no_sync_row(tmp_path, monkeypatch):
    db = tmp_path / "agt.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(PHASE_B_SCHEMA)
    # No master_log_sync row -> Flex incomplete.
    conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at, engine, run_id, "
        "broker_mode_at_staging, staged_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("{}", "filled", "2026-04-26T13:00:00+00:00", "csp_allocator", "r1", "paper",
         "2026-04-26T13:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    from agt_equities import db as agt_db
    monkeypatch.setattr(agt_db, "DB_PATH", str(db))

    from agt_equities.order_lifecycle import proof_report
    rep = proof_report.generate_proof_report(
        report_date_et=REPORT_DATE, is_preview=False, db_path=str(db),
    )
    assert rep.verdict in {"PENDING_FLEX", "INSUFFICIENT_DATA"}
