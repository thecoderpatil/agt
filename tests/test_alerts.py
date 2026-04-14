"""Sprint A unit A5b — cross_daemon_alerts bus tests.

Pure-DB unit tests. Each test gets a tmp_path SQLite file, registers the
operational schema, and exercises the alerts module via its db_path
kwarg. No monkeypatching of agt_equities.db.DB_PATH (FU-A-04 pattern).
The alerts module must be importable in CI's slim container (it depends
only on stdlib + agt_equities.db).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from agt_equities import alerts as alerts_mod
from agt_equities.alerts import (
    MAX_ATTEMPTS,
    drain_pending_alerts,
    enqueue_alert,
    get_alert,
    mark_alert_failed,
    mark_alert_sent,
)
from agt_equities.db import get_db_connection
from agt_equities.schema import register_operational_tables

pytestmark = pytest.mark.sprint_a


@pytest.fixture
def alerts_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "alerts_a5b.db"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        register_operational_tables(conn)
        conn.commit()
    return db_path


def test_enqueue_then_drain_round_trip(alerts_db: Path) -> None:
    aid = enqueue_alert(
        "STAGED_DIGEST",
        {"household": "Yash_Household", "n_pending": 3},
        severity="info",
        db_path=alerts_db,
    )
    assert isinstance(aid, int) and aid > 0

    pending = get_alert(aid, db_path=alerts_db)
    assert pending is not None
    assert pending["status"] == "pending"
    assert pending["kind"] == "STAGED_DIGEST"
    assert pending["severity"] == "info"
    assert pending["attempts"] == 0
    assert pending["payload"] == {"household": "Yash_Household", "n_pending": 3}

    drained = drain_pending_alerts(db_path=alerts_db)
    assert len(drained) == 1
    rec = drained[0]
    assert rec["id"] == aid
    assert rec["kind"] == "STAGED_DIGEST"
    assert rec["payload"]["household"] == "Yash_Household"
    assert rec["attempts"] == 1

    in_flight = get_alert(aid, db_path=alerts_db)
    assert in_flight is not None
    assert in_flight["status"] == "in_flight"
    assert in_flight["attempts"] == 1


def test_mark_alert_sent_terminal(alerts_db: Path) -> None:
    aid = enqueue_alert("ATTESTED_KEYBOARD", {"x": 1}, db_path=alerts_db)
    drain_pending_alerts(db_path=alerts_db)
    mark_alert_sent(aid, db_path=alerts_db)

    rec = get_alert(aid, db_path=alerts_db)
    assert rec is not None
    assert rec["status"] == "sent"
    assert rec["sent_ts"] is not None
    # Subsequent drains must not re-surface a sent alert
    assert drain_pending_alerts(db_path=alerts_db) == []


def test_mark_alert_failed_retries_then_terminal(alerts_db: Path) -> None:
    aid = enqueue_alert("APEX_MARGIN_WARN", {"acct": "Uxxx"}, severity="warn", db_path=alerts_db)
    # MAX_ATTEMPTS retries should walk pending -> in_flight -> pending,
    # then on the MAX-th drain the failure becomes terminal 'failed'.
    for i in range(1, MAX_ATTEMPTS + 1):
        drained = drain_pending_alerts(db_path=alerts_db)
        assert len(drained) == 1, f"iteration {i}: expected 1 drained, got {len(drained)}"
        assert drained[0]["id"] == aid
        assert drained[0]["attempts"] == i
        mark_alert_failed(aid, f"network error iter {i}", db_path=alerts_db)

    rec = get_alert(aid, db_path=alerts_db)
    assert rec is not None
    assert rec["status"] == "failed"
    assert rec["attempts"] == MAX_ATTEMPTS
    assert rec["last_error"] is not None and "iter" in rec["last_error"]
    # Terminal failed must not re-drain
    assert drain_pending_alerts(db_path=alerts_db) == []


def test_drain_fifo_order(alerts_db: Path) -> None:
    ids = [
        enqueue_alert("FLEX_SYNC_DIGEST", {"i": i}, db_path=alerts_db)
        for i in range(5)
    ]
    drained = drain_pending_alerts(db_path=alerts_db)
    assert [r["id"] for r in drained] == ids


def test_drain_limit_respected(alerts_db: Path) -> None:
    for i in range(7):
        enqueue_alert("STAGED_DIGEST", {"i": i}, db_path=alerts_db)
    first = drain_pending_alerts(limit=3, db_path=alerts_db)
    assert len(first) == 3
    second = drain_pending_alerts(limit=10, db_path=alerts_db)
    assert len(second) == 4  # remaining


def test_drain_empty_returns_empty_list(alerts_db: Path) -> None:
    assert drain_pending_alerts(db_path=alerts_db) == []
    assert drain_pending_alerts(limit=0, db_path=alerts_db) == []


def test_enqueue_rejects_invalid_severity(alerts_db: Path) -> None:
    with pytest.raises(ValueError):
        enqueue_alert("X", {}, severity="emergency", db_path=alerts_db)


def test_enqueue_rejects_empty_kind(alerts_db: Path) -> None:
    with pytest.raises(ValueError):
        enqueue_alert("   ", {"a": 1}, db_path=alerts_db)


def test_enqueue_rejects_unserializable_payload(alerts_db: Path) -> None:
    class Opaque:
        pass

    # default=str rescues most things; an object whose str() raises will fail.
    class BadStr:
        def __str__(self) -> str:  # pragma: no cover - exercised via json.dumps
            raise RuntimeError("nope")

    # Sets are not JSON-serializable and default=str will stringify them
    # successfully, so use a self-referential structure to force a failure.
    a: dict = {}
    a["self"] = a
    with pytest.raises(ValueError):
        enqueue_alert("X", a, db_path=alerts_db)


def test_mark_failed_unknown_id_is_noop(alerts_db: Path) -> None:
    # Should not raise even for nonexistent ids.
    mark_alert_failed(999_999, "ghost", db_path=alerts_db)
    assert get_alert(999_999, db_path=alerts_db) is None


def test_get_alert_decodes_payload(alerts_db: Path) -> None:
    aid = enqueue_alert("STAGED_DIGEST", {"nested": {"k": [1, 2, 3]}}, db_path=alerts_db)
    rec = get_alert(aid, db_path=alerts_db)
    assert rec is not None
    assert rec["payload"]["nested"]["k"] == [1, 2, 3]


def test_table_indexed_for_drain(alerts_db: Path) -> None:
    """Ensure the (status, created_ts) index exists — drain hot path."""
    with closing(sqlite3.connect(str(alerts_db))) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='cross_daemon_alerts'"
        ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_cross_daemon_alerts_status_created" in names
