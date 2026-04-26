"""operator_ledger record/query + VALID_KINDS enforcement."""
from __future__ import annotations

import sqlite3

import pytest

from agt_equities.order_lifecycle.operator_ledger import (
    VALID_KINDS, record_intervention, query_interventions,
)

pytestmark = pytest.mark.sprint_a


def _seed(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
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
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_record_intervention_inserts_row(tmp_path, monkeypatch):
    db = tmp_path / "agt.db"
    _seed(db)
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    from agt_equities import db as agt_db
    monkeypatch.setattr(agt_db, "DB_PATH", str(db))

    rid = record_intervention(
        operator_user_id="123", kind="reject",
        target_table="pending_orders", target_id=42,
        before_state={"status": "staged"}, after_state={"status": "rejected"},
        reason="manual reject", db_path=str(db),
    )
    assert rid > 0

    rows = query_interventions(since_utc="2020-01-01T00:00:00", db_path=str(db))
    assert len(rows) == 1
    assert rows[0]["kind"] == "reject"
    assert rows[0]["before_state"]["status"] == "staged"
    assert rows[0]["after_state"]["status"] == "rejected"


def test_record_rejects_unknown_kind():
    with pytest.raises(ValueError):
        record_intervention(operator_user_id="x", kind="invalid_kind")


def test_query_filters_by_kind(tmp_path, monkeypatch):
    db = tmp_path / "agt.db"
    _seed(db)
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    from agt_equities import db as agt_db
    monkeypatch.setattr(agt_db, "DB_PATH", str(db))

    record_intervention(operator_user_id="1", kind="reject", db_path=str(db))
    record_intervention(operator_user_id="1", kind="approve", db_path=str(db))
    record_intervention(operator_user_id="1", kind="halt", db_path=str(db))

    rejects = query_interventions(since_utc="2020-01-01T00:00:00", kind="reject", db_path=str(db))
    assert all(r["kind"] == "reject" for r in rejects)
    assert len(rejects) == 1


def test_valid_kinds_membership():
    expected = {"reject", "reject_rem", "approve", "recover_transmitting",
                "flex_manual_reconcile", "halt", "direct_sql",
                "manual_terminal", "restart_during_market"}
    assert expected == set(VALID_KINDS)
