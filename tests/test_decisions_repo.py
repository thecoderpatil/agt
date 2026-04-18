"""Unit tests for ADR-012 decisions_repo."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

from agt_equities import decisions_repo


@pytest.fixture
def tmp_db() -> Path:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    p = Path(path)
    from scripts.migrate_decisions_schema import run as migrate

    migrate(db_path=p)
    yield p
    p.unlink(missing_ok=True)


def _count(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]


@pytest.mark.sprint_a
def test_record_decision_inserts_row(tmp_db):
    decisions_repo.record_decision(
        decision_id="01HZ_TEST_01",
        engine="csp_entry",
        ticker="MSFT",
        raw_input_hash="a" * 64,
        operator_action="approved",
        prompt_version="v1",
        db_path=tmp_db,
    )
    assert _count(tmp_db) == 1


@pytest.mark.sprint_a
def test_record_decision_idempotent_on_id(tmp_db):
    for _ in range(3):
        decisions_repo.record_decision(
            decision_id="01HZ_TEST_02",
            engine="csp_entry",
            ticker="NVDA",
            raw_input_hash="b" * 64,
            operator_action="approved",
            prompt_version="v1",
            db_path=tmp_db,
        )
    assert _count(tmp_db) == 1


@pytest.mark.sprint_a
def test_record_operator_action_updates(tmp_db):
    decisions_repo.record_decision(
        decision_id="01HZ_TEST_03",
        engine="csp_entry",
        ticker="AMD",
        raw_input_hash="c" * 64,
        operator_action="pending",
        prompt_version="v1",
        db_path=tmp_db,
    )
    decisions_repo.record_operator_action(
        decision_id="01HZ_TEST_03",
        operator_action="rejected",
        db_path=tmp_db,
    )
    with closing(sqlite3.connect(tmp_db)) as conn:
        action = conn.execute(
            "SELECT operator_action FROM decisions WHERE decision_id = ?",
            ("01HZ_TEST_03",),
        ).fetchone()[0]
    assert action == "rejected"


@pytest.mark.sprint_a
def test_settle_realized_pnl_updates(tmp_db):
    decisions_repo.record_decision(
        decision_id="01HZ_TEST_04",
        engine="cc_harvest",
        ticker="AAPL",
        raw_input_hash="d" * 64,
        operator_action="autonomous",
        prompt_version="v1",
        db_path=tmp_db,
    )
    decisions_repo.settle_realized_pnl(
        decision_id="01HZ_TEST_04",
        realized_pnl=147.32,
        db_path=tmp_db,
    )
    with closing(sqlite3.connect(tmp_db)) as conn:
        pnl = conn.execute(
            "SELECT realized_pnl FROM decisions WHERE decision_id = ?",
            ("01HZ_TEST_04",),
        ).fetchone()[0]
    assert pnl == pytest.approx(147.32)
