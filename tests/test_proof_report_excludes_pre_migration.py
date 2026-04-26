"""Pre-migration rows are excluded from scoring; count surfaced in JSON."""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.test_proof_report_metrics import PHASE_B_SCHEMA, REPORT_DATE  # type: ignore[attr-defined]

pytestmark = pytest.mark.sprint_a


def test_pre_migration_rows_excluded_count_surfaces(tmp_path, monkeypatch):
    db = tmp_path / "agt.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(PHASE_B_SCHEMA)

    # Insert 3 PRE-migration rows in window: created_at present BUT engine NULL.
    for i in range(3):
        conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, ?, ?)",
            ("{}", "filled", f"2026-04-26T{12+i:02d}:00:00+00:00"),
        )

    # Insert one POST-migration row (engine set, staged_at_utc set).
    conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at, engine, run_id, "
        "broker_mode_at_staging, staged_at_utc, gate_verdicts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("{}", "filled", "2026-04-26T16:00:00+00:00", "csp_allocator", "r1",
         "paper", "2026-04-26T16:00:00+00:00", json.dumps({"strike_freshness": True})),
    )

    # Flex sync row so we don't short-circuit on PENDING_FLEX.
    conn.execute(
        "INSERT INTO master_log_sync (sync_id, started_at, status) VALUES (?, ?, ?)",
        ("s1", "2026-04-27T11:00:00+00:00", "success"),
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

    # The pre-migration rows must surface as excluded count.
    assert rep.data_freshness["pre_migration_rows_excluded"] == 3
    # And NOT contribute to the same-day terminal calculation -- 1 of 1 == 100%.
    assert rep.metrics["pct_same_day_terminal"] == 100.0


def test_no_pre_migration_when_all_rows_post(tmp_path, monkeypatch):
    db = tmp_path / "agt.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(PHASE_B_SCHEMA)
    conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at, engine, run_id, "
        "broker_mode_at_staging, staged_at_utc, gate_verdicts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("{}", "filled", "2026-04-26T13:00:00+00:00", "csp_allocator", "r1", "paper",
         "2026-04-26T13:00:00+00:00", json.dumps({"strike_freshness": True})),
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

    from agt_equities.order_lifecycle import proof_report
    rep = proof_report.generate_proof_report(
        report_date_et=REPORT_DATE, is_preview=False, db_path=str(db),
    )
    assert rep.data_freshness["pre_migration_rows_excluded"] == 0
