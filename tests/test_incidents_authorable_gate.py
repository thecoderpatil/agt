"""ADR-007 Step 7a + 7b: list_authorable() gate + tier routing.

Step 7a: confirms the manifest's `max_consecutive_violations` is enforced
downstream -- flappy invariants (max=3 or 5) no longer burn LLM spend on
first detection, but stay visible in /report and other read APIs that
call list_by_status() directly.

Step 7b: confirms scrutiny-tier routing splits incidents cleanly between
the Author/Critic pipeline (low/medium/high) and the architect-escalation
lane (architect_only), so the Author never wastes LLM spend authoring
fixes that ``author_critic.run_mechanical_critic`` will hard-block.

Fixtures: in-memory sqlite seeded with the canonical incidents DDL
(mirrors the schema in agt_equities/schema.py:1524+). Manifest is
mocked via a small helper -- no YAML read, no file I/O beyond the
tmp sqlite file.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agt_equities import incidents_repo as repo

pytestmark = pytest.mark.sprint_a


# Canonical incidents DDL (matches schema.py:1524 and test_incidents_repo.py).
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
]


@pytest.fixture
def db(tmp_path: Path) -> str:
    db_path = tmp_path / "authorable.db"
    conn = sqlite3.connect(db_path)
    conn.execute(_INCIDENTS_DDL)
    for stmt in _INCIDENTS_INDEXES:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    return str(db_path)


def _manifest(**id_to_max: int) -> list[dict]:
    """Build a minimal manifest list from id -> max_consecutive_violations."""
    return [
        {"id": k, "max_consecutive_violations": v,
         "severity_floor": "medium", "scrutiny_tier": "low"}
        for k, v in id_to_max.items()
    ]


def _insert(
    db: str,
    *,
    invariant_id: str | None,
    consecutive_breaches: int,
    status: str = repo.STATUS_OPEN,
    severity: str = "medium",
    scrutiny_tier: str = "low",
    key_suffix: str = "a",
    degraded: bool = False,
) -> int:
    """Direct INSERT to seed a row with exact consecutive_breaches value
    (skips register()'s state machine)."""
    now = (
        datetime.now(timezone.utc) - timedelta(minutes=consecutive_breaches)
    ).isoformat(timespec="seconds")
    key = f"{invariant_id or 'NO_INV'}:{key_suffix}-cb{consecutive_breaches}"
    observed_json = '{"degraded": true}' if degraded else None
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "INSERT INTO incidents ("
            "  incident_key, invariant_id, severity, scrutiny_tier, "
            "  status, detector, detected_at, last_action_at, "
            "  consecutive_breaches, observed_state"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, invariant_id, severity, scrutiny_tier, status, "test",
             now, now, consecutive_breaches, observed_json),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. empty queue
# ---------------------------------------------------------------------------

def test_empty_queue_returns_empty(db: str) -> None:
    rows = repo.list_authorable(
        manifest=_manifest(FOO=1),
        db_path=db,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# 2. below threshold excluded
# ---------------------------------------------------------------------------

def test_below_threshold_excluded(db: str) -> None:
    """max=3 invariant with consecutive_breaches=1 and =2 excluded."""
    _insert(db, invariant_id="FLAPPY", consecutive_breaches=1, key_suffix="a")
    _insert(db, invariant_id="FLAPPY", consecutive_breaches=2, key_suffix="b")

    rows = repo.list_authorable(
        manifest=_manifest(FLAPPY=3),
        db_path=db,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# 3. at threshold included
# ---------------------------------------------------------------------------

def test_at_threshold_included(db: str) -> None:
    _insert(db, invariant_id="FLAPPY", consecutive_breaches=3, key_suffix="a")

    rows = repo.list_authorable(
        manifest=_manifest(FLAPPY=3),
        db_path=db,
    )
    assert len(rows) == 1
    assert rows[0]["invariant_id"] == "FLAPPY"
    assert rows[0]["consecutive_breaches"] == 3


# ---------------------------------------------------------------------------
# 4. above threshold included
# ---------------------------------------------------------------------------

def test_above_threshold_included(db: str) -> None:
    _insert(db, invariant_id="FLAPPY", consecutive_breaches=5, key_suffix="a")

    rows = repo.list_authorable(
        manifest=_manifest(FLAPPY=3),
        db_path=db,
    )
    assert len(rows) == 1
    assert rows[0]["consecutive_breaches"] == 5


# ---------------------------------------------------------------------------
# 5. unknown invariant_id fail-open
# ---------------------------------------------------------------------------

def test_unknown_invariant_id_fail_open(db: str) -> None:
    """Row whose invariant_id is missing from the manifest must still
    appear in the authorable list -- better to generate a spurious
    Author pass than silently drop it (e.g. mid-rename)."""
    _insert(db, invariant_id="ORPHAN_INV", consecutive_breaches=1)
    _insert(db, invariant_id=None, consecutive_breaches=1, key_suffix="b")  # no invariant_id

    rows = repo.list_authorable(
        manifest=_manifest(FOO=99),  # ORPHAN_INV not here
        db_path=db,
    )
    assert len(rows) == 2
    inv_ids = {r["invariant_id"] for r in rows}
    assert inv_ids == {"ORPHAN_INV", None}


# ---------------------------------------------------------------------------
# 6. status filter respected
# ---------------------------------------------------------------------------

def test_status_filter_respected(db: str) -> None:
    """AUTHORING / AWAITING / ARCHITECT rows are excluded by default
    statuses arg -- they're already past the Author-kickoff gate."""
    _insert(db, invariant_id="FOO", consecutive_breaches=5,
            status=repo.STATUS_OPEN, key_suffix="a")
    _insert(db, invariant_id="FOO", consecutive_breaches=5,
            status=repo.STATUS_AUTHORING, key_suffix="b")
    _insert(db, invariant_id="FOO", consecutive_breaches=5,
            status=repo.STATUS_AWAITING, key_suffix="c")
    _insert(db, invariant_id="FOO", consecutive_breaches=5,
            status=repo.STATUS_ARCHITECT, key_suffix="d")
    _insert(db, invariant_id="FOO", consecutive_breaches=5,
            status=repo.STATUS_REJECTED_ONCE, key_suffix="e")

    rows = repo.list_authorable(
        manifest=_manifest(FOO=1),
        db_path=db,
    )
    got_statuses = sorted(r["status"] for r in rows)
    assert got_statuses == [repo.STATUS_OPEN, repo.STATUS_REJECTED_ONCE]


# ---------------------------------------------------------------------------
# 7. mixed thresholds per invariant
# ---------------------------------------------------------------------------

def test_mixed_thresholds_per_invariant(db: str) -> None:
    """One invariant max=1 (hard rail), one max=3 (flappy). Rows just
    below the flappy threshold must drop while the hard-rail row
    passes on first detection."""
    # HARD max=1: 1 breach is enough
    _insert(db, invariant_id="HARD", consecutive_breaches=1, key_suffix="h1")
    # SOFT max=3: 2 breaches not enough
    _insert(db, invariant_id="SOFT", consecutive_breaches=2, key_suffix="s2")
    # SOFT max=3: 3 breaches enough
    _insert(db, invariant_id="SOFT", consecutive_breaches=3, key_suffix="s3")

    rows = repo.list_authorable(
        manifest=_manifest(HARD=1, SOFT=3),
        db_path=db,
    )
    keys = sorted(r["incident_key"] for r in rows)
    assert keys == ["HARD:h1-cb1", "SOFT:s3-cb3"]


# ---------------------------------------------------------------------------
# 8. degraded evidence still gated
# ---------------------------------------------------------------------------

def test_degraded_evidence_still_gated(db: str) -> None:
    """A row with degraded=True evidence is still subject to the
    threshold -- we don't want Author to burn cycles on flappy
    "I can't see the process table" degraded rows either."""
    _insert(db, invariant_id="FLAPPY", consecutive_breaches=1,
            key_suffix="d1", degraded=True)
    _insert(db, invariant_id="FLAPPY", consecutive_breaches=3,
            key_suffix="d3", degraded=True)

    rows = repo.list_authorable(
        manifest=_manifest(FLAPPY=3),
        db_path=db,
    )
    assert len(rows) == 1
    assert rows[0]["incident_key"] == "FLAPPY:d3-cb3"



# ---------------------------------------------------------------------------
# 9. Step 7b: default scrutiny_tiers excludes architect_only
# ---------------------------------------------------------------------------

def test_default_excludes_architect_only_tier(db: str) -> None:
    """Default ``scrutiny_tiers`` (low/medium/high) must drop
    ``architect_only`` rows so the Author/Critic pipeline never wastes
    LLM spend on incidents ``run_mechanical_critic`` will hard-block."""
    _insert(db, invariant_id="LOW_INV", consecutive_breaches=1,
            scrutiny_tier="low", key_suffix="a")
    _insert(db, invariant_id="MED_INV", consecutive_breaches=1,
            scrutiny_tier="medium", key_suffix="b")
    _insert(db, invariant_id="HIGH_INV", consecutive_breaches=1,
            scrutiny_tier="high", key_suffix="c")
    _insert(db, invariant_id="ARCH_INV", consecutive_breaches=1,
            scrutiny_tier="architect_only", key_suffix="d")

    rows = repo.list_authorable(
        manifest=_manifest(LOW_INV=1, MED_INV=1, HIGH_INV=1, ARCH_INV=1),
        db_path=db,
    )
    got = sorted(r["invariant_id"] for r in rows)
    assert got == ["HIGH_INV", "LOW_INV", "MED_INV"]
    # architect_only explicitly absent
    assert "ARCH_INV" not in got


# ---------------------------------------------------------------------------
# 10. Step 7b: explicit scrutiny_tiers override
# ---------------------------------------------------------------------------

def test_scrutiny_tiers_override_can_include_architect_only(db: str) -> None:
    """Callers that want the full set (e.g. /report) can pass the
    complete ``SCRUTINY_TIERS`` enum and get every tier."""
    _insert(db, invariant_id="LOW_INV", consecutive_breaches=1,
            scrutiny_tier="low", key_suffix="a")
    _insert(db, invariant_id="ARCH_INV", consecutive_breaches=1,
            scrutiny_tier="architect_only", key_suffix="b")

    rows = repo.list_authorable(
        scrutiny_tiers=repo.SCRUTINY_TIERS,
        manifest=_manifest(LOW_INV=1, ARCH_INV=1),
        db_path=db,
    )
    got = sorted(r["invariant_id"] for r in rows)
    assert got == ["ARCH_INV", "LOW_INV"]


# ---------------------------------------------------------------------------
# 11. Step 7b: unknown / empty tier fails open
# ---------------------------------------------------------------------------

def test_unknown_scrutiny_tier_fail_open(db: str) -> None:
    """Rows with an empty or unrecognized ``scrutiny_tier`` are still
    included by default. A schema skew (manual INSERT, legacy row) must
    not silently drop incidents out of the Author queue."""
    # NOTE: register() rejects empty tier at the write path; this models
    # a legacy/manual row that bypassed register(). Directly INSERTing
    # here simulates that state.
    import sqlite3
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO incidents ("
            "  incident_key, invariant_id, severity, scrutiny_tier, "
            "  status, detector, detected_at, last_action_at, "
            "  consecutive_breaches"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            ("EMPTY_TIER:z", "EMPTY_INV", "medium", "",
             repo.STATUS_OPEN, "test", now, now, 1),
        )
        conn.commit()
    finally:
        conn.close()

    rows = repo.list_authorable(
        manifest=_manifest(EMPTY_INV=1),
        db_path=db,
    )
    keys = {r["incident_key"] for r in rows}
    assert "EMPTY_TIER:z" in keys, (
        "empty scrutiny_tier must fail-open (included) to avoid "
        "silently dropping legacy rows"
    )


# ---------------------------------------------------------------------------
# 12. Step 7b: list_architect_only returns only architect_only tier
# ---------------------------------------------------------------------------

def test_list_architect_only_happy_path(db: str) -> None:
    _insert(db, invariant_id="LOW_INV", consecutive_breaches=1,
            scrutiny_tier="low", key_suffix="a")
    _insert(db, invariant_id="ARCH_INV", consecutive_breaches=1,
            scrutiny_tier="architect_only", key_suffix="b")
    _insert(db, invariant_id="ARCH_INV", consecutive_breaches=1,
            scrutiny_tier="architect_only", key_suffix="c")

    rows = repo.list_architect_only(
        manifest=_manifest(LOW_INV=1, ARCH_INV=1),
        db_path=db,
    )
    assert len(rows) == 2
    tiers = {r["scrutiny_tier"] for r in rows}
    assert tiers == {"architect_only"}


# ---------------------------------------------------------------------------
# 13. Step 7b: list_architect_only respects threshold gate
# ---------------------------------------------------------------------------

def test_list_architect_only_below_threshold_excluded(db: str) -> None:
    """Architect-only incidents still honour the manifest threshold --
    don't wake Yash up for a single blip on a flappy architect-tier
    invariant (``max_consecutive_violations`` > 1)."""
    _insert(db, invariant_id="ARCH_FLAPPY", consecutive_breaches=1,
            scrutiny_tier="architect_only", key_suffix="a")
    _insert(db, invariant_id="ARCH_FLAPPY", consecutive_breaches=3,
            scrutiny_tier="architect_only", key_suffix="b")

    rows = repo.list_architect_only(
        manifest=_manifest(ARCH_FLAPPY=3),
        db_path=db,
    )
    assert len(rows) == 1
    assert rows[0]["consecutive_breaches"] == 3


# ---------------------------------------------------------------------------
# 14. Step 7b: authorable + architect_only lanes are disjoint
# ---------------------------------------------------------------------------

def test_authorable_and_architect_only_disjoint(db: str) -> None:
    """Every eligible row appears in exactly one of the two lanes."""
    _insert(db, invariant_id="LOW_INV", consecutive_breaches=1,
            scrutiny_tier="low", key_suffix="a")
    _insert(db, invariant_id="MED_INV", consecutive_breaches=1,
            scrutiny_tier="medium", key_suffix="b")
    _insert(db, invariant_id="HIGH_INV", consecutive_breaches=1,
            scrutiny_tier="high", key_suffix="c")
    _insert(db, invariant_id="ARCH_INV", consecutive_breaches=1,
            scrutiny_tier="architect_only", key_suffix="d")

    auth = repo.list_authorable(
        manifest=_manifest(LOW_INV=1, MED_INV=1, HIGH_INV=1, ARCH_INV=1),
        db_path=db,
    )
    arch = repo.list_architect_only(
        manifest=_manifest(LOW_INV=1, MED_INV=1, HIGH_INV=1, ARCH_INV=1),
        db_path=db,
    )
    auth_ids = {r["id"] for r in auth}
    arch_ids = {r["id"] for r in arch}
    assert not (auth_ids & arch_ids), "authorable and architect_only must be disjoint"
    assert auth_ids | arch_ids == {r["id"] for r in auth + arch}


# ---------------------------------------------------------------------------
# 15. Step 7b: tier gate + threshold gate are AND-ed, not OR-ed
# ---------------------------------------------------------------------------

def test_tier_and_threshold_gates_both_required(db: str) -> None:
    """A below-threshold low-tier row must drop even though its tier is
    allowed. Both gates must pass simultaneously."""
    _insert(db, invariant_id="LOW_FLAPPY", consecutive_breaches=1,
            scrutiny_tier="low", key_suffix="a")
    _insert(db, invariant_id="LOW_FLAPPY", consecutive_breaches=5,
            scrutiny_tier="low", key_suffix="b")

    rows = repo.list_authorable(
        manifest=_manifest(LOW_FLAPPY=3),
        db_path=db,
    )
    assert len(rows) == 1
    assert rows[0]["consecutive_breaches"] == 5
