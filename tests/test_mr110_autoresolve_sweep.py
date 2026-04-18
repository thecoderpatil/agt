"""MR !110 — invariants tick auto-resolve sweep + heartbeat stable_key.

Tests:
  1-5: post-check sweep resolves open incidents when invariant clears,
       skips architect_only tier, tolerates exceptions.
  6-7: NO_MISSING_DAEMON_HEARTBEAT now carries stable_key on missing-row
       and stale-row Violations.
  8:   stable_key causes dedup across ticks (UPDATE not INSERT).

Recon: reports/stranded_incident_recon_20260418.md
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agt_equities.invariants.checks import check_no_missing_daemon_heartbeat
from agt_equities.invariants.tick import check_invariants_tick
from agt_equities.invariants.types import CheckContext, Violation

pytestmark = pytest.mark.sprint_a

# ---------------------------------------------------------------------------
# Schema helpers
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
);
CREATE UNIQUE INDEX idx_incidents_active_key
    ON incidents(incident_key)
    WHERE status NOT IN ('merged','resolved','rejected_permanently');
CREATE INDEX idx_incidents_status ON incidents(status);
CREATE INDEX idx_incidents_invariant_id ON incidents(invariant_id);
"""

_DAEMON_HEARTBEAT_DDL = """
CREATE TABLE daemon_heartbeat (
    daemon_name   TEXT PRIMARY KEY,
    last_beat_utc TEXT,
    pid           INTEGER,
    client_id     TEXT,
    notes         TEXT
);
"""

NOW = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
_STALE_BEAT = (NOW - timedelta(seconds=200)).isoformat()  # 200s > 120s TTL


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def incidents_db(tmp_path: Path, monkeypatch) -> Path:
    """Temp file DB with incidents schema; monkeypatches agt_equities.db.DB_PATH."""
    db_path = tmp_path / "agt_desk.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_INCIDENTS_DDL)
    conn.commit()
    conn.close()
    monkeypatch.setattr("agt_equities.db.DB_PATH", str(db_path), raising=False)
    return db_path


@pytest.fixture
def heartbeat_conn_ctx():
    """In-memory DB with daemon_heartbeat + CheckContext for agt_bot."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DAEMON_HEARTBEAT_DDL)
    ctx = CheckContext(
        now_utc=NOW,
        db_path=":memory:",
        paper_mode=True,
        live_accounts=frozenset(),
        paper_accounts=frozenset(),
        expected_daemons=frozenset({"agt_bot"}),
        daemon_heartbeat_ttl_s=120,
    )
    return conn, ctx


# ---------------------------------------------------------------------------
# Seed / query helpers
# ---------------------------------------------------------------------------

def _seed_incident(
    db_path: Path,
    *,
    invariant_id: str,
    scrutiny_tier: str = "medium",
    key_suffix: str = "a",
) -> int:
    now_s = NOW.isoformat()
    key = f"{invariant_id}:test-{key_suffix}"
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO incidents "
            "(incident_key, invariant_id, severity, scrutiny_tier, "
            " status, detector, detected_at, last_action_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (key, invariant_id, "medium", scrutiny_tier,
             "open", "test", now_s, now_s),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _get_status(db_path: Path, row_id: int) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status FROM incidents WHERE id = ?", (row_id,)
        ).fetchone()
        return row["status"] if row else "MISSING"
    finally:
        conn.close()


def _make_manifest(inv_id: str, scrutiny_tier: str = "medium") -> list[dict]:
    return [{
        "id": inv_id,
        "description": "test invariant",
        "check_fn": "no_op",
        "scrutiny_tier": scrutiny_tier,
        "severity_floor": "medium",
        "fix_by_sprint": "A",
        "max_consecutive_violations": 1,
    }]


def _stub_run_all(result: dict):
    def _mock(*args, **kwargs):
        return result
    return _mock


# ===========================================================================
# 1. Sweep resolves incidents when invariant produces zero violations
# ===========================================================================

def test_sweep_resolves_incidents_for_zero_violation_invariant(
    incidents_db, monkeypatch,
) -> None:
    """Two open incidents for a cleared invariant → both resolved."""
    inv_id = "NO_STRANDED_STAGED_ORDERS"
    id1 = _seed_incident(incidents_db, invariant_id=inv_id, key_suffix="x")
    id2 = _seed_incident(incidents_db, invariant_id=inv_id, key_suffix="y")

    monkeypatch.setattr("agt_equities.invariants.run_all", _stub_run_all({inv_id: []}))
    monkeypatch.setattr(
        "agt_equities.invariants.load_invariants",
        lambda *a, **kw: _make_manifest(inv_id),
    )

    check_invariants_tick()

    assert _get_status(incidents_db, id1) == "resolved"
    assert _get_status(incidents_db, id2) == "resolved"


# ===========================================================================
# 2. Sweep skips architect_only tier
# ===========================================================================

def test_sweep_skips_architect_only_tier(incidents_db, monkeypatch) -> None:
    """architect_only incidents never auto-resolve — require human review."""
    inv_id = "NO_SHADOW_ON_PROD_DB"
    row_id = _seed_incident(
        incidents_db, invariant_id=inv_id, scrutiny_tier="architect_only",
    )

    monkeypatch.setattr("agt_equities.invariants.run_all", _stub_run_all({inv_id: []}))
    monkeypatch.setattr(
        "agt_equities.invariants.load_invariants",
        lambda *a, **kw: _make_manifest(inv_id, scrutiny_tier="architect_only"),
    )

    check_invariants_tick()

    assert _get_status(incidents_db, row_id) == "open"


# ===========================================================================
# 3. Sweep leaves incidents when invariant still has live violations
# ===========================================================================

def test_sweep_leaves_incidents_with_live_violations(incidents_db, monkeypatch) -> None:
    inv_id = "NO_LIVE_IN_PAPER"
    row_id = _seed_incident(incidents_db, invariant_id=inv_id)

    live_vio = Violation(
        invariant_id=inv_id,
        description="still active",
        severity="high",
        evidence={"pending_order_id": 99},
    )
    monkeypatch.setattr(
        "agt_equities.invariants.run_all", _stub_run_all({inv_id: [live_vio]}),
    )
    monkeypatch.setattr(
        "agt_equities.invariants.load_invariants",
        lambda *a, **kw: _make_manifest(inv_id),
    )

    check_invariants_tick()

    assert _get_status(incidents_db, row_id) == "open"


# ===========================================================================
# 4. Sweep handles list_by_status exception gracefully (no crash)
# ===========================================================================

def test_sweep_handles_list_by_status_exception(incidents_db, monkeypatch) -> None:
    inv_id = "NO_STRANDED_STAGED_ORDERS"
    row_id = _seed_incident(incidents_db, invariant_id=inv_id)

    monkeypatch.setattr("agt_equities.invariants.run_all", _stub_run_all({inv_id: []}))
    monkeypatch.setattr(
        "agt_equities.invariants.load_invariants",
        lambda *a, **kw: _make_manifest(inv_id),
    )
    import agt_equities.incidents_repo as _repo

    def _boom(*a, **kw):
        raise RuntimeError("injected list_by_status failure")

    monkeypatch.setattr(_repo, "list_by_status", _boom)

    check_invariants_tick()  # must not raise

    assert _get_status(incidents_db, row_id) == "open"


# ===========================================================================
# 5. Sweep handles mark_resolved exception on one row; continues to next
# ===========================================================================

def test_sweep_handles_mark_resolved_exception(incidents_db, monkeypatch) -> None:
    inv_id = "NO_STRANDED_STAGED_ORDERS"
    id_fail = _seed_incident(incidents_db, invariant_id=inv_id, key_suffix="fail")
    id_ok   = _seed_incident(incidents_db, invariant_id=inv_id, key_suffix="ok")

    monkeypatch.setattr("agt_equities.invariants.run_all", _stub_run_all({inv_id: []}))
    monkeypatch.setattr(
        "agt_equities.invariants.load_invariants",
        lambda *a, **kw: _make_manifest(inv_id),
    )

    import agt_equities.incidents_repo as _repo
    _orig_resolve = _repo.mark_resolved

    def _selective(incident_id, **kwargs):
        if incident_id == id_fail:
            raise RuntimeError("injected mark_resolved failure")
        return _orig_resolve(incident_id, **kwargs)

    monkeypatch.setattr(_repo, "mark_resolved", _selective)

    check_invariants_tick()  # must not raise

    assert _get_status(incidents_db, id_fail) == "open"
    assert _get_status(incidents_db, id_ok)   == "resolved"


# ===========================================================================
# 6. Missing heartbeat row → stable_key set on Violation
# ===========================================================================

def test_missing_heartbeat_stable_key_matches_format(heartbeat_conn_ctx) -> None:
    conn, ctx = heartbeat_conn_ctx
    vios = check_no_missing_daemon_heartbeat(conn, ctx)
    assert len(vios) == 1
    assert vios[0].stable_key == "NO_MISSING_DAEMON_HEARTBEAT:agt_bot"


# ===========================================================================
# 7. Stale heartbeat row → stable_key set on Violation
# ===========================================================================

def test_stale_heartbeat_stable_key_matches_format(heartbeat_conn_ctx) -> None:
    conn, ctx = heartbeat_conn_ctx
    conn.execute(
        "INSERT INTO daemon_heartbeat (daemon_name, last_beat_utc) VALUES (?, ?)",
        ("agt_bot", _STALE_BEAT),
    )
    vios = check_no_missing_daemon_heartbeat(conn, ctx)
    assert len(vios) == 1
    assert vios[0].stable_key == "NO_MISSING_DAEMON_HEARTBEAT:agt_bot"


# ===========================================================================
# 8. Same daemon stale across two ticks → one incident row (dedup via stable_key)
# ===========================================================================

def test_heartbeat_stable_key_dedups_across_ticks(incidents_db, monkeypatch) -> None:
    """Two ticks with the same stable_key → UPDATE consecutive_breaches, not INSERT."""
    inv_id = "NO_MISSING_DAEMON_HEARTBEAT"
    stable_key = f"NO_MISSING_DAEMON_HEARTBEAT:agt_bot"

    stale_vio = Violation(
        invariant_id=inv_id,
        description="Daemon 'agt_bot' heartbeat stale by 200s",
        severity="high",
        evidence={"daemon_name": "agt_bot", "stale_seconds": 200.0},
        stable_key=stable_key,
    )

    monkeypatch.setattr(
        "agt_equities.invariants.run_all", _stub_run_all({inv_id: [stale_vio]}),
    )
    monkeypatch.setattr(
        "agt_equities.invariants.load_invariants",
        lambda *a, **kw: _make_manifest(inv_id),
    )

    check_invariants_tick()
    check_invariants_tick()

    conn = sqlite3.connect(str(incidents_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM incidents WHERE invariant_id = ?", (inv_id,)
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, f"Expected 1 row (dedup), got {len(rows)}"
    assert rows[0]["consecutive_breaches"] == 2
