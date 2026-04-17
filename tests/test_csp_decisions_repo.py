"""Tests for csp_decisions_repo — audit trail persistence."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from agt_equities import csp_decisions_repo

pytestmark = pytest.mark.sprint_a


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "test_decisions.db"
    csp_decisions_repo.ensure_schema(db_path=db)
    return db


def test_schema_creates_table(tmp_db: Path):
    with sqlite3.connect(tmp_db) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='csp_decisions'"
        ).fetchone()
    assert row is not None


def test_record_and_read_by_ticker(tmp_db: Path):
    csp_decisions_repo.record_decision(
        run_id="run-001",
        household_id="HH_TEST",
        ticker="AAPL",
        final_outcome="staged",
        gate_verdicts=[
            {"gate": "rule_1_leverage", "ok": True, "reason": None},
            {"gate": "rule_3_sector", "ok": True, "reason": None},
        ],
        evidence_snapshot={"sector": "Technology", "delta": -0.22},
        n_requested=5,
        n_sized=2,
        db_path=tmp_db,
    )
    rows = csp_decisions_repo.list_by_ticker("AAPL", db_path=tmp_db)
    assert len(rows) == 1
    assert rows[0]["final_outcome"] == "staged"
    assert rows[0]["gate_verdicts"][0]["gate"] == "rule_1_leverage"
    assert rows[0]["evidence_snapshot"]["sector"] == "Technology"
    assert rows[0]["n_sized"] == 2


def test_record_rejected_candidate(tmp_db: Path):
    csp_decisions_repo.record_decision(
        run_id="run-002",
        household_id="HH_TEST",
        ticker="MRNA",
        final_outcome="rejected_by_rule_3b_excluded_sector",
        gate_verdicts=[
            {"gate": "rule_1_leverage", "ok": True, "reason": None},
            {"gate": "rule_3b_excluded_sector", "ok": False,
             "reason": "Biotechnology in EXCLUDED_SECTORS"},
        ],
        evidence_snapshot={"sector": "Biotechnology"},
        n_requested=5,
        n_sized=None,
        db_path=tmp_db,
    )
    rows = csp_decisions_repo.list_by_ticker("MRNA", db_path=tmp_db)
    assert len(rows) == 1
    assert rows[0]["final_outcome"] == "rejected_by_rule_3b_excluded_sector"
    assert rows[0]["n_sized"] is None


def test_list_by_run_returns_all(tmp_db: Path):
    for t in ("AAPL", "MSFT", "MRNA"):
        csp_decisions_repo.record_decision(
            run_id="run-multi",
            household_id="HH_TEST",
            ticker=t,
            final_outcome="staged" if t != "MRNA" else "rejected_by_rule_3b",
            gate_verdicts=[{"gate": "rule_1", "ok": True, "reason": None}],
            evidence_snapshot={"sector": "X"},
            db_path=tmp_db,
        )
    rows = csp_decisions_repo.list_by_run("run-multi", db_path=tmp_db)
    assert len(rows) == 3
    assert {r["ticker"] for r in rows} == {"AAPL", "MSFT", "MRNA"}
