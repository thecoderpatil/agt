"""update_submission_evidence helper writes correct columns; idempotent."""
from __future__ import annotations

import json
import sqlite3

import pytest

from agt_equities.order_state import update_submission_evidence

pytestmark = pytest.mark.sprint_a


def _seed(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE pending_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                submitted_at_utc TEXT,
                spot_at_submission REAL,
                limit_price_at_submission REAL,
                gate_verdicts TEXT,
                acked_at_utc TEXT
            );
            INSERT INTO pending_orders (payload, status, created_at)
                VALUES ('{}', 'sent', '2026-04-25T12:00:00Z');
            """
        )
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def test_update_writes_columns(tmp_path):
    conn = _seed(tmp_path / "agt.db")
    try:
        update_submission_evidence(
            conn, 1,
            submitted_at_utc="2026-04-26T13:00:00Z",
            spot_at_submission=392.5,
            limit_price_at_submission=1.05,
            gate_verdicts={"mode_match": True, "strike_freshness": True},
        )
        conn.commit()
        row = conn.execute(
            "SELECT submitted_at_utc, spot_at_submission, limit_price_at_submission, gate_verdicts "
            "FROM pending_orders WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "2026-04-26T13:00:00Z"
    assert row[1] == 392.5
    assert row[2] == 1.05
    assert json.loads(row[3])["mode_match"] is True


def test_update_idempotent_with_none_gate_verdicts(tmp_path):
    conn = _seed(tmp_path / "agt.db")
    try:
        update_submission_evidence(
            conn, 1, submitted_at_utc="2026-04-26T13:00:00Z",
            gate_verdicts=None,
        )
        conn.commit()
        row = conn.execute("SELECT gate_verdicts FROM pending_orders WHERE id = 1").fetchone()
    finally:
        conn.close()
    assert row[0] is None


def test_acked_at_utc_coalesce_semantics(tmp_path):
    """Repeated COALESCE writes should preserve the FIRST timestamp."""
    db = tmp_path / "agt.db"
    conn = _seed(db)
    try:
        conn.execute(
            "UPDATE pending_orders SET acked_at_utc = COALESCE(acked_at_utc, ?) WHERE id = 1",
            ("first-ack",),
        )
        conn.execute(
            "UPDATE pending_orders SET acked_at_utc = COALESCE(acked_at_utc, ?) WHERE id = 1",
            ("second-ack",),
        )
        conn.commit()
        row = conn.execute("SELECT acked_at_utc FROM pending_orders WHERE id = 1").fetchone()
    finally:
        conn.close()
    assert row[0] == "first-ack"
