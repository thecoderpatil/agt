"""End-to-end proof_report generator tests across verdict paths.

Exercises is_preview=True/False and PASS / FAIL / PASS_NO_ACTIVITY /
PENDING_FLEX paths via seeded fake-day data.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.test_proof_report_metrics import PHASE_B_SCHEMA, REPORT_DATE  # type: ignore[attr-defined]

pytestmark = pytest.mark.sprint_a


def _seed(tmp_path, monkeypatch, *, with_flex: bool = True, with_filled: bool = True):
    db = tmp_path / "agt.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(PHASE_B_SCHEMA)
    if with_filled:
        conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at, engine, run_id, "
            "broker_mode_at_staging, staged_at_utc, gate_verdicts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("{}", "filled", "2026-04-26T13:00:00+00:00", "csp_allocator", "r1",
             "paper", "2026-04-26T13:00:00+00:00", json.dumps({"strike_freshness": True})),
        )
    else:
        # Inject a marker row OUTSIDE the report window so the migration
        # is detected but in-window activity is zero.
        conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at, engine, run_id, "
            "broker_mode_at_staging, staged_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("{}", "filled", "2026-04-20T13:00:00+00:00", "csp_allocator", "r0",
             "paper", "2026-04-20T13:00:00+00:00"),
        )
    if with_flex:
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


def test_pass_path_emits_json_and_md(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch)
    out_dir = tmp_path / "reports"
    from agt_equities.order_lifecycle import proof_report
    rep = proof_report.generate_proof_report(
        report_date_et=REPORT_DATE, is_preview=False,
        db_path=str(db), output_dir=out_dir,
    )
    assert rep.verdict == "PASS"
    json_path = out_dir / "proof_20260426.json"
    md_path = out_dir / "proof_20260426.md"
    assert json_path.exists()
    assert md_path.exists()
    payload = json.loads(json_path.read_text())
    assert payload["verdict"] == "PASS"
    assert payload["is_preview"] is False
    md_text = md_path.read_text()
    assert "Phase B Proof Report" in md_text
    assert "PASS" in md_text


def test_preview_path_emits_preview_suffix(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch)
    out_dir = tmp_path / "reports"
    from agt_equities.order_lifecycle import proof_report
    rep = proof_report.generate_proof_report(
        report_date_et=REPORT_DATE, is_preview=True,
        db_path=str(db), output_dir=out_dir,
    )
    assert rep.is_preview is True
    assert (out_dir / "proof_20260426_preview.json").exists()
    assert (out_dir / "proof_20260426_preview.md").exists()


def test_pass_no_activity_path(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, with_filled=False)
    from agt_equities.order_lifecycle import proof_report
    rep = proof_report.generate_proof_report(
        report_date_et=REPORT_DATE, is_preview=False, db_path=str(db),
    )
    assert rep.verdict == "PASS_NO_ACTIVITY"


def test_pending_flex_path(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, with_flex=False)
    from agt_equities.order_lifecycle import proof_report
    rep = proof_report.generate_proof_report(
        report_date_et=REPORT_DATE, is_preview=False, db_path=str(db),
    )
    assert rep.verdict in {"PENDING_FLEX", "INSUFFICIENT_DATA"}


def test_insufficient_data_when_no_migration(tmp_path, monkeypatch):
    db = tmp_path / "agt.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(PHASE_B_SCHEMA)
    # Pre-migration row only (no engine column populated).
    conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, ?, ?)",
        ("{}", "filled", "2026-04-26T13:00:00+00:00"),
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
    assert rep.verdict == "INSUFFICIENT_DATA"
