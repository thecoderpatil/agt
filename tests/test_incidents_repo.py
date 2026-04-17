"""Tests for agt_equities.incidents_repo — ADR-007 Step 3 CRUD.

Covers:
    - register() idempotency + consecutive_breaches dedup
    - Every state transition in the ADR-007 §4.2 machine
    - Illegal transitions raise ValueError
    - rejection_history accumulates across mark_rejected calls
    - Dual-write mirrors into remediation_incidents when present
    - Dual-write is a silent no-op when remediation_incidents is absent
    - closed_at stamped on terminal transitions
    - Reads: get, get_by_key (active_only flag), list_by_status,
             list_active_for_invariant
    - JSON payload validation rejects malformed strings
    - Enum validation rejects unknown severity / scrutiny_tier / status
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agt_equities import incidents_repo as repo


pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Schema fixtures
# ---------------------------------------------------------------------------

_INCIDENTS_DDL = """
CREATE TABLE incidents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_key         TEXT NOT NULL,
    invariant_id         TEXT,
    severity             TEXT NOT NULL,
    scrutiny_tier        TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open',
    detector             TEXT NOT NULL,
    detected_at          TEXT NOT NULL,
    closed_at            TEXT,
    last_action_at       TEXT,
    consecutive_breaches INTEGER NOT NULL DEFAULT 1,
    observed_state       TEXT,
    desired_state        TEXT,
    confidence           REAL,
    mr_iid               INTEGER,
    ddiff_url            TEXT,
    rejection_history    TEXT
)
"""

_INCIDENTS_INDEXES = [
    """
    CREATE UNIQUE INDEX idx_incidents_active_key
    ON incidents(incident_key)
    WHERE status NOT IN ('merged','resolved','rejected_permanently')
    """,
    "CREATE INDEX idx_incidents_status ON incidents(status)",
    "CREATE INDEX idx_incidents_invariant_id ON incidents(invariant_id)",
    "CREATE INDEX idx_incidents_mr_iid ON incidents(mr_iid)",
]

_REMEDIATION_DDL = """
CREATE TABLE remediation_incidents (
    incident_id       TEXT PRIMARY KEY,
    first_detected    TEXT NOT NULL,
    directive_source  TEXT,
    fix_authored_at   TEXT,
    mr_iid            INTEGER,
    branch_name       TEXT,
    status            TEXT NOT NULL DEFAULT 'new',
    rejection_reasons TEXT,
    last_nudged_at    TEXT,
    architect_reason  TEXT,
    updated_at        TEXT
)
"""


def _init_db(
    tmp_path: Path,
    *,
    with_remediation: bool = True,
) -> str:
    db = tmp_path / "incidents_test.db"
    conn = sqlite3.connect(db)
    conn.execute(_INCIDENTS_DDL)
    for stmt in _INCIDENTS_INDEXES:
        conn.execute(stmt)
    if with_remediation:
        conn.execute(_REMEDIATION_DDL)
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def db(tmp_path: Path) -> str:
    return _init_db(tmp_path, with_remediation=True)


@pytest.fixture
def db_no_legacy(tmp_path: Path) -> str:
    return _init_db(tmp_path, with_remediation=False)


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

def test_register_inserts_new_row_with_defaults(db: str) -> None:
    row = repo.register(
        "NO_LIVE_IN_PAPER_PO_42",
        severity="crit",
        scrutiny_tier="architect_only",
        detector="invariant_check",
        invariant_id="NO_LIVE_IN_PAPER",
        observed_state={"count": 1, "sample_order_id": 42},
        desired_state={"count": 0},
        confidence=1.0,
        db_path=db,
    )
    assert row["id"] == 1
    assert row["incident_key"] == "NO_LIVE_IN_PAPER_PO_42"
    assert row["status"] == repo.STATUS_OPEN
    assert row["severity"] == "crit"
    assert row["scrutiny_tier"] == "architect_only"
    assert row["detector"] == "invariant_check"
    assert row["invariant_id"] == "NO_LIVE_IN_PAPER"
    assert row["consecutive_breaches"] == 1
    assert row["detected_at"] is not None
    assert row["last_action_at"] == row["detected_at"]
    assert row["closed_at"] is None
    assert row["mr_iid"] is None
    assert row["ddiff_url"] is None
    assert row["rejection_history"] is None
    # JSON payloads round-trip.
    assert json.loads(row["observed_state"]) == {"count": 1, "sample_order_id": 42}
    assert json.loads(row["desired_state"]) == {"count": 0}
    assert row["confidence"] == 1.0


def test_register_is_idempotent_same_key(db: str) -> None:
    r1 = repo.register(
        "DUP_KEY", severity="warn", scrutiny_tier="medium",
        detector="invariant_check", invariant_id="X",
        observed_state={"v": 1}, db_path=db,
    )
    r2 = repo.register(
        "DUP_KEY", severity="warn", scrutiny_tier="medium",
        detector="invariant_check", invariant_id="X",
        observed_state={"v": 2}, db_path=db,
    )
    assert r1["id"] == r2["id"]
    assert r2["consecutive_breaches"] == 2
    # Newer observed_state wins.
    assert json.loads(r2["observed_state"]) == {"v": 2}
    # last_action_at advanced.
    assert r2["last_action_at"] >= r1["last_action_at"]


def test_register_after_close_opens_new_row(db: str) -> None:
    """Closed rows must not block a fresh open under the same key."""
    r1 = repo.register(
        "FLAPPY", severity="medium", scrutiny_tier="medium",
        detector="invariant_check", invariant_id="Y",
        db_path=db,
    )
    repo.mark_authoring(r1["id"], db_path=db)
    repo.mark_awaiting_approval(r1["id"], mr_iid=111, db_path=db)
    repo.mark_merged(r1["id"], db_path=db)

    r2 = repo.register(
        "FLAPPY", severity="medium", scrutiny_tier="medium",
        detector="invariant_check", invariant_id="Y",
        db_path=db,
    )
    assert r2["id"] != r1["id"]
    assert r2["consecutive_breaches"] == 1
    assert r2["status"] == repo.STATUS_OPEN


def test_register_rejects_empty_key(db: str) -> None:
    with pytest.raises(ValueError, match="incident_key is required"):
        repo.register(
            "", severity="crit", scrutiny_tier="high",
            detector="invariant_check", db_path=db,
        )


def test_register_rejects_unknown_severity(db: str) -> None:
    with pytest.raises(ValueError, match="unknown severity"):
        repo.register(
            "K", severity="nope", scrutiny_tier="medium",
            detector="invariant_check", db_path=db,
        )


def test_register_rejects_unknown_scrutiny(db: str) -> None:
    with pytest.raises(ValueError, match="unknown scrutiny_tier"):
        repo.register(
            "K", severity="crit", scrutiny_tier="ultra",
            detector="invariant_check", db_path=db,
        )


def test_register_rejects_empty_detector(db: str) -> None:
    with pytest.raises(ValueError, match="detector is required"):
        repo.register(
            "K", severity="crit", scrutiny_tier="high", detector="",
            db_path=db,
        )


def test_register_rejects_malformed_json_string_payload(db: str) -> None:
    with pytest.raises(ValueError, match="invalid JSON string payload"):
        repo.register(
            "K", severity="crit", scrutiny_tier="high",
            detector="invariant_check",
            observed_state="{not: valid json}",
            db_path=db,
        )


def test_register_accepts_well_formed_json_string_payload(db: str) -> None:
    row = repo.register(
        "K", severity="crit", scrutiny_tier="high",
        detector="invariant_check",
        observed_state='{"already": "json"}',
        db_path=db,
    )
    assert json.loads(row["observed_state"]) == {"already": "json"}


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def _seed_open(db: str, key: str = "INC") -> dict:
    return repo.register(
        key, severity="high", scrutiny_tier="medium",
        detector="invariant_check", invariant_id="INV_X",
        db_path=db,
    )


def test_mark_authoring_from_open(db: str) -> None:
    row = _seed_open(db)
    out = repo.mark_authoring(row["id"], db_path=db)
    assert out["status"] == repo.STATUS_AUTHORING
    assert out["last_action_at"] is not None
    assert out["closed_at"] is None


def test_mark_awaiting_approval_records_mr_iid_and_ddiff(db: str) -> None:
    row = _seed_open(db)
    out = repo.mark_awaiting_approval(
        row["id"], mr_iid=73, ddiff_url="http://ddiff/73", db_path=db,
    )
    assert out["status"] == repo.STATUS_AWAITING
    assert out["mr_iid"] == 73
    assert out["ddiff_url"] == "http://ddiff/73"


def test_mark_merged_sets_closed_at(db: str) -> None:
    row = _seed_open(db)
    repo.mark_awaiting_approval(row["id"], mr_iid=73, db_path=db)
    out = repo.mark_merged(row["id"], db_path=db)
    assert out["status"] == repo.STATUS_MERGED
    assert out["closed_at"] is not None


def test_mark_merged_from_open_is_illegal(db: str) -> None:
    row = _seed_open(db)
    with pytest.raises(ValueError, match="illegal transition"):
        repo.mark_merged(row["id"], db_path=db)


def test_mark_resolved_from_any_active_status(db: str) -> None:
    for key, advance_fn in [
        ("K_OPEN", None),
        ("K_AUTH", lambda i: repo.mark_authoring(i, db_path=db)),
        ("K_AWAIT", lambda i: [
            repo.mark_authoring(i, db_path=db),
            repo.mark_awaiting_approval(i, mr_iid=1, db_path=db),
        ]),
    ]:
        row = _seed_open(db, key=key)
        if advance_fn is not None:
            advance_fn(row["id"])
        out = repo.mark_resolved(row["id"], db_path=db)
        assert out["status"] == repo.STATUS_RESOLVED
        assert out["closed_at"] is not None


def test_mark_rejected_three_strike_ladder(db: str) -> None:
    row = _seed_open(db)
    repo.mark_awaiting_approval(row["id"], mr_iid=1, db_path=db)
    r1 = repo.mark_rejected(row["id"], "flaky test", db_path=db)
    assert r1["status"] == repo.STATUS_REJECTED_ONCE
    r2 = repo.mark_rejected(row["id"], "still flaky", db_path=db)
    assert r2["status"] == repo.STATUS_REJECTED_TWICE
    r3 = repo.mark_rejected(row["id"], "third strike", db_path=db)
    assert r3["status"] == repo.STATUS_REJECTED_PERM
    assert r3["closed_at"] is not None
    # Fourth call is a no-op.
    r4 = repo.mark_rejected(row["id"], "more reasons", db_path=db)
    assert r4["status"] == repo.STATUS_REJECTED_PERM

    history = json.loads(r3["rejection_history"])
    assert [h["reason"] for h in history] == [
        "flaky test", "still flaky", "third strike",
    ]
    assert [h["from_status"] for h in history] == [
        repo.STATUS_AWAITING, repo.STATUS_REJECTED_ONCE, repo.STATUS_REJECTED_TWICE,
    ]


def test_mark_rejected_requires_reason(db: str) -> None:
    row = _seed_open(db)
    with pytest.raises(ValueError, match="rejection reason is required"):
        repo.mark_rejected(row["id"], "", db_path=db)
    with pytest.raises(ValueError, match="rejection reason is required"):
        repo.mark_rejected(row["id"], "   ", db_path=db)


def test_mark_needs_architect(db: str) -> None:
    row = _seed_open(db)
    out = repo.mark_needs_architect(
        row["id"], "schema change spans 3 modules", db_path=db,
    )
    assert out["status"] == repo.STATUS_ARCHITECT
    envelope = json.loads(out["desired_state"])
    assert envelope["architect_escalation"] == "schema change spans 3 modules"
    assert envelope["at"] is not None


def test_mark_needs_architect_requires_reason(db: str) -> None:
    row = _seed_open(db)
    with pytest.raises(ValueError, match="architect escalation reason"):
        repo.mark_needs_architect(row["id"], "", db_path=db)


def test_unknown_incident_id_raises(db: str) -> None:
    with pytest.raises(ValueError, match="unknown incident id"):
        repo.mark_authoring(9999, db_path=db)


def test_append_rejection_reason_does_not_advance_status(db: str) -> None:
    row = _seed_open(db)
    repo.mark_authoring(row["id"], db_path=db)
    out = repo.append_rejection_reason(
        row["id"], "internal critic rejected", db_path=db,
    )
    assert out["status"] == repo.STATUS_AUTHORING
    history = json.loads(out["rejection_history"])
    assert history[0]["reason"] == "internal critic rejected"
    assert history[0]["internal"] is True


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def test_get_by_id_and_key(db: str) -> None:
    row = _seed_open(db, key="LOOKUP")
    assert repo.get(row["id"], db_path=db)["id"] == row["id"]
    assert repo.get_by_key("LOOKUP", db_path=db)["id"] == row["id"]
    assert repo.get_by_key("MISSING", db_path=db) is None


def test_get_by_key_active_only_flag(db: str) -> None:
    row = _seed_open(db, key="K1")
    repo.mark_awaiting_approval(row["id"], mr_iid=1, db_path=db)
    repo.mark_merged(row["id"], db_path=db)
    # active_only=True → closed row is invisible.
    assert repo.get_by_key("K1", active_only=True, db_path=db) is None
    # active_only=False → returns the closed row.
    closed = repo.get_by_key("K1", active_only=False, db_path=db)
    assert closed["status"] == repo.STATUS_MERGED


def test_list_by_status_filters_and_orders(db: str) -> None:
    r1 = _seed_open(db, key="A")
    r2 = _seed_open(db, key="B")
    r3 = _seed_open(db, key="C")
    repo.mark_authoring(r2["id"], db_path=db)
    repo.mark_awaiting_approval(r2["id"], mr_iid=5, db_path=db)

    opens = repo.list_by_status([repo.STATUS_OPEN], db_path=db)
    assert {r["id"] for r in opens} == {r1["id"], r3["id"]}

    awaiting = repo.list_by_status([repo.STATUS_AWAITING], db_path=db)
    assert [r["id"] for r in awaiting] == [r2["id"]]

    assert repo.list_by_status([], db_path=db) == []


def test_list_by_status_rejects_unknown_status(db: str) -> None:
    with pytest.raises(ValueError, match="unknown status"):
        repo.list_by_status(["bogus"], db_path=db)


def test_list_active_for_invariant_excludes_closed(db: str) -> None:
    r_a = repo.register(
        "A", severity="high", scrutiny_tier="medium",
        detector="invariant_check", invariant_id="INV1", db_path=db,
    )
    r_b = repo.register(
        "B", severity="high", scrutiny_tier="medium",
        detector="invariant_check", invariant_id="INV1", db_path=db,
    )
    repo.register(
        "C", severity="high", scrutiny_tier="medium",
        detector="invariant_check", invariant_id="INV2", db_path=db,
    )
    # Close one of the INV1 rows.
    repo.mark_awaiting_approval(r_b["id"], mr_iid=2, db_path=db)
    repo.mark_merged(r_b["id"], db_path=db)

    active_inv1 = repo.list_active_for_invariant("INV1", db_path=db)
    assert [r["id"] for r in active_inv1] == [r_a["id"]]


# ---------------------------------------------------------------------------
# Dual-write into remediation_incidents
# ---------------------------------------------------------------------------

def _legacy_row(db_path: str, incident_key: str) -> dict | None:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM remediation_incidents WHERE incident_id = ?",
            (incident_key,),
        ).fetchone()
        return dict(row) if row is not None else None


def test_register_mirrors_into_remediation_incidents(db: str) -> None:
    row = repo.register(
        "MIRROR_KEY", severity="crit", scrutiny_tier="architect_only",
        detector="invariant_check", invariant_id="I", db_path=db,
    )
    legacy = _legacy_row(db, "MIRROR_KEY")
    assert legacy is not None
    assert legacy["status"] == "new"
    assert legacy["directive_source"] == "invariant_check"
    assert legacy["first_detected"] == row["detected_at"]


def test_mark_awaiting_approval_mirrors_status_and_mr_iid(db: str) -> None:
    row = _seed_open(db, key="MA")
    repo.mark_awaiting_approval(
        row["id"], mr_iid=42, branch_name="feat/x", db_path=db,
    )
    legacy = _legacy_row(db, "MA")
    assert legacy["status"] == "awaiting_approval"
    assert legacy["mr_iid"] == 42
    assert legacy["branch_name"] == "feat/x"
    assert legacy["fix_authored_at"] is not None


def test_mark_merged_mirrors_status(db: str) -> None:
    row = _seed_open(db, key="MM")
    repo.mark_awaiting_approval(row["id"], mr_iid=1, db_path=db)
    repo.mark_merged(row["id"], db_path=db)
    legacy = _legacy_row(db, "MM")
    assert legacy["status"] == "merged"


def test_mark_rejected_mirrors_rejection_reasons_json(db: str) -> None:
    row = _seed_open(db, key="MR")
    repo.mark_awaiting_approval(row["id"], mr_iid=1, db_path=db)
    repo.mark_rejected(row["id"], "nope", db_path=db)
    legacy = _legacy_row(db, "MR")
    assert legacy["status"] == "rejected_once"
    reasons = json.loads(legacy["rejection_reasons"])
    assert reasons[0]["reason"] == "nope"


def test_mark_needs_architect_mirrors_reason(db: str) -> None:
    row = _seed_open(db, key="ARCH")
    repo.mark_needs_architect(row["id"], "too deep", db_path=db)
    legacy = _legacy_row(db, "ARCH")
    assert legacy["status"] == "needs_architect"
    assert legacy["architect_reason"] == "too deep"


def test_mark_resolved_mirrors_as_merged(db: str) -> None:
    row = _seed_open(db, key="RES")
    repo.mark_resolved(row["id"], db_path=db)
    legacy = _legacy_row(db, "RES")
    # 'resolved' has no analogue in the legacy table; we map to 'merged'
    # so the weekly pipeline treats it as closed.
    assert legacy["status"] == "merged"


def test_dual_write_is_noop_when_legacy_table_absent(db_no_legacy: str) -> None:
    """Bot-less test DBs may not carry the legacy table; the repo must
    not blow up on the mirror-write attempt."""
    row = repo.register(
        "STANDALONE", severity="crit", scrutiny_tier="high",
        detector="invariant_check", invariant_id="Z", db_path=db_no_legacy,
    )
    assert row["status"] == repo.STATUS_OPEN
    repo.mark_authoring(row["id"], db_path=db_no_legacy)
    repo.mark_awaiting_approval(row["id"], mr_iid=1, db_path=db_no_legacy)
    repo.mark_merged(row["id"], db_path=db_no_legacy)


# ---------------------------------------------------------------------------
# Partial unique index enforcement
# ---------------------------------------------------------------------------

def test_partial_unique_index_blocks_two_active_rows_for_same_key(db: str) -> None:
    """Direct INSERT of a second active row under the same key must fail."""
    repo.register(
        "UIX", severity="high", scrutiny_tier="medium",
        detector="invariant_check", invariant_id="I", db_path=db,
    )
    with sqlite3.connect(db) as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                """
                INSERT INTO incidents (
                    incident_key, severity, scrutiny_tier, status,
                    detector, detected_at, consecutive_breaches
                ) VALUES ('UIX','high','medium','open','manual',
                          '2026-04-16T00:00:00+00:00', 1)
                """
            )


def test_partial_unique_index_allows_reopen_after_close(db: str) -> None:
    row = repo.register(
        "REOPEN", severity="high", scrutiny_tier="medium",
        detector="invariant_check", invariant_id="I", db_path=db,
    )
    repo.mark_awaiting_approval(row["id"], mr_iid=1, db_path=db)
    repo.mark_merged(row["id"], db_path=db)
    # Now a manual insert with the same key is legal because the prior
    # row is in a closed status.
    with sqlite3.connect(db) as c:
        c.execute(
            """
            INSERT INTO incidents (
                incident_key, severity, scrutiny_tier, status,
                detector, detected_at, consecutive_breaches
            ) VALUES ('REOPEN','high','medium','open','manual',
                      '2026-04-16T00:00:00+00:00', 1)
            """
        )
    # Two rows exist under the same key, only one active.
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT status FROM incidents WHERE incident_key = 'REOPEN' "
            "ORDER BY id"
        ).fetchall()
        assert [r["status"] for r in rows] == ["merged", "open"]
