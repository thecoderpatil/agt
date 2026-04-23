"""Tests for csp_digest.approval_gate — identity / fail-closed / state machine."""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from agt_equities.csp_digest.approval_gate import (  # noqa: E402
    DEFAULT_TIMEOUT_MINUTES,
    fail_closed_timeout_gate,
    identity_approval_gate,
    insert_pending_row,
    resolve_ticker,
    sweep_timeouts,
)

import migrate_llm_cost_ledger  # noqa: E402

pytestmark = pytest.mark.sprint_a


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "agt_desk.db"
    migrate_llm_cost_ledger.migrate(str(p))
    return p


def _now():
    return datetime(2026, 4, 23, 13, 35, tzinfo=timezone.utc)


def _tickets(n: int) -> list[dict]:
    return [{"i": i, "ticker": f"T{i}"} for i in range(n)]


# ---------- identity ----------


def test_identity_gate_passes_through():
    tx = _tickets(3)
    assert identity_approval_gate(tx) == tx


def test_identity_gate_returns_a_copy():
    tx = _tickets(2)
    out = identity_approval_gate(tx)
    out.append({"i": 99})
    assert len(tx) == 2  # original unmutated


# ---------- fail-closed: empty cases ----------


def test_fail_closed_no_record_returns_empty(db_path):
    out = fail_closed_timeout_gate(_tickets(3), db_path=db_path, run_id="missing")
    assert out == []


def test_fail_closed_pending_status_returns_empty(db_path):
    insert_pending_row(
        db_path, run_id="r1", household_id="hh",
        candidates_json=json.dumps([{"i": 0}, {"i": 1}]),
        sent_at_utc=_now(), timeout_at_utc=_now() + timedelta(minutes=90),
    )
    out = fail_closed_timeout_gate(_tickets(2), db_path=db_path, run_id="r1")
    assert out == []


def test_fail_closed_rejected_status_returns_empty(db_path):
    insert_pending_row(
        db_path, run_id="r1", household_id="hh",
        candidates_json=json.dumps([{"i": 0}]),
        sent_at_utc=_now(), timeout_at_utc=_now() + timedelta(minutes=90),
    )
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            "UPDATE csp_pending_approval SET status='rejected' WHERE run_id=?",
            ("r1",),
        )
        conn.commit()
    out = fail_closed_timeout_gate(_tickets(1), db_path=db_path, run_id="r1")
    assert out == []


# ---------- fail-closed: approved subset ----------


def test_fail_closed_partial_returns_only_approved_subset(db_path):
    insert_pending_row(
        db_path, run_id="r1", household_id="hh",
        candidates_json=json.dumps([{"i": 0}, {"i": 1}, {"i": 2}]),
        sent_at_utc=_now(), timeout_at_utc=_now() + timedelta(minutes=90),
    )
    # Approve indices 0 and 2
    resolve_ticker(db_path, run_id="r1", candidate_index=0,
                   decision="approve", resolved_by="telegram:user")
    resolve_ticker(db_path, run_id="r1", candidate_index=2,
                   decision="approve", resolved_by="telegram:user")
    tx = [{"i": 0}, {"i": 1}, {"i": 2}]
    out = fail_closed_timeout_gate(tx, db_path=db_path, run_id="r1")
    assert len(out) == 2
    assert {t["i"] for t in out} == {0, 2}


def test_fail_closed_all_approved_returns_all(db_path):
    insert_pending_row(
        db_path, run_id="r1", household_id="hh",
        candidates_json=json.dumps([{"i": 0}, {"i": 1}]),
        sent_at_utc=_now(), timeout_at_utc=_now() + timedelta(minutes=90),
    )
    resolve_ticker(db_path, run_id="r1", candidate_index=0,
                   decision="approve", resolved_by="telegram:user")
    resolve_ticker(db_path, run_id="r1", candidate_index=1,
                   decision="approve", resolved_by="telegram:user")
    tx = [{"i": 0}, {"i": 1}]
    out = fail_closed_timeout_gate(tx, db_path=db_path, run_id="r1")
    assert len(out) == 2


# ---------- state transitions ----------


def test_resolve_ticker_partial_then_approved(db_path):
    insert_pending_row(
        db_path, run_id="r1", household_id="hh",
        candidates_json=json.dumps([{"i": 0}, {"i": 1}]),
        sent_at_utc=_now(), timeout_at_utc=_now() + timedelta(minutes=90),
    )
    resolve_ticker(db_path, run_id="r1", candidate_index=0,
                   decision="approve", resolved_by="user")
    with closing(sqlite3.connect(str(db_path))) as conn:
        status = conn.execute(
            "SELECT status FROM csp_pending_approval WHERE run_id=?", ("r1",),
        ).fetchone()
    assert status[0] == "partial"
    resolve_ticker(db_path, run_id="r1", candidate_index=1,
                   decision="approve", resolved_by="user")
    with closing(sqlite3.connect(str(db_path))) as conn:
        status = conn.execute(
            "SELECT status FROM csp_pending_approval WHERE run_id=?", ("r1",),
        ).fetchone()
    assert status[0] == "approved"


def test_resolve_ticker_invalid_decision_raises(db_path):
    insert_pending_row(
        db_path, run_id="r1", household_id="hh",
        candidates_json=json.dumps([{"i": 0}]),
        sent_at_utc=_now(), timeout_at_utc=_now() + timedelta(minutes=90),
    )
    with pytest.raises(ValueError, match="approve.*reject"):
        resolve_ticker(db_path, run_id="r1", candidate_index=0,
                       decision="maybe", resolved_by="x")


def test_resolve_ticker_returns_false_on_unknown_run_id(db_path):
    out = resolve_ticker(db_path, run_id="missing", candidate_index=0,
                         decision="approve", resolved_by="x")
    assert out is False


# ---------- timeout sweep ----------


def test_sweep_flips_stale_pending_to_timeout(db_path):
    past = _now() - timedelta(minutes=120)
    insert_pending_row(
        db_path, run_id="stale1", household_id="hh",
        candidates_json=json.dumps([{"i": 0}]),
        sent_at_utc=past, timeout_at_utc=past + timedelta(minutes=90),
    )
    n = sweep_timeouts(db_path, now_utc=_now())
    assert n == 1
    with closing(sqlite3.connect(str(db_path))) as conn:
        st = conn.execute(
            "SELECT status, resolved_by FROM csp_pending_approval WHERE run_id=?",
            ("stale1",),
        ).fetchone()
    assert st[0] == "timeout"
    assert st[1] == "timeout"


def test_sweep_flips_stale_partial_when_some_approved(db_path):
    past = _now() - timedelta(minutes=120)
    insert_pending_row(
        db_path, run_id="stale2", household_id="hh",
        candidates_json=json.dumps([{"i": 0}, {"i": 1}]),
        sent_at_utc=past, timeout_at_utc=past + timedelta(minutes=90),
    )
    # Manually set one as approved while keeping status='pending' to simulate
    # the race where a tap landed but ticker tx didn't commit timeout flip.
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            "UPDATE csp_pending_approval SET approved_indices_json=? WHERE run_id=?",
            (json.dumps([0]), "stale2"),
        )
        conn.commit()
    sweep_timeouts(db_path, now_utc=_now())
    with closing(sqlite3.connect(str(db_path))) as conn:
        st = conn.execute(
            "SELECT status FROM csp_pending_approval WHERE run_id=?", ("stale2",),
        ).fetchone()
    assert st[0] == "partial"


def test_sweep_skips_fresh_pending(db_path):
    insert_pending_row(
        db_path, run_id="fresh", household_id="hh",
        candidates_json=json.dumps([{"i": 0}]),
        sent_at_utc=_now(), timeout_at_utc=_now() + timedelta(minutes=90),
    )
    n = sweep_timeouts(db_path, now_utc=_now())
    assert n == 0


def test_default_timeout_minutes_is_90():
    assert DEFAULT_TIMEOUT_MINUTES == 90
