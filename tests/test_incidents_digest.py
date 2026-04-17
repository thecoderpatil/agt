"""Tests for scripts/incidents_digest.py — ADR-007 Step 5 CLI.

Covers:
    - main() returns 0 on empty queue and emits the "No active incidents"
      marker instead of an error.
    - Default statuses match DEFAULT_STATUSES (excludes merged / resolved /
      rejected_permanently).
    - --format json emits valid JSON that round-trips to list[dict].
    - --status filter overrides the default set.
    - --since filters rows whose last_action_at is strictly before the
      ISO8601 threshold.
    - --since rejects non-ISO8601 input.
    - Markdown rendering rolls up by invariant_id and includes id / key /
      MR badge when an MR is attached.
    - Exit code 2 on DB read failure (bad db_path).
    - _filter_since / _parse_since unit coverage.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from agt_equities import incidents_repo as repo


pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Module loader — scripts/ is not a package, so import by path.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DIGEST_PATH = _REPO_ROOT / "scripts" / "incidents_digest.py"


def _load_digest():
    spec = importlib.util.spec_from_file_location(
        "_agt_incidents_digest_test", _DIGEST_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def digest():
    return _load_digest()


# ---------------------------------------------------------------------------
# DB fixture — same DDL the repo tests use.
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


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = tmp_path / "incidents_digest_test.db"
    conn = sqlite3.connect(path)
    conn.execute(_INCIDENTS_DDL)
    for stmt in _INCIDENTS_INDEXES:
        conn.execute(stmt)
    conn.execute(_REMEDIATION_DDL)
    conn.commit()
    conn.close()
    return str(path)


def _register(db: str, key: str, *, invariant: str = "TEST_INV",
              severity: str = "warn", tier: str = "medium") -> dict:
    return repo.register(
        key,
        severity=severity,
        scrutiny_tier=tier,
        detector="invariant_check",
        invariant_id=invariant,
        db_path=db,
    )


# ---------------------------------------------------------------------------
# DEFAULT_STATUSES invariant
# ---------------------------------------------------------------------------

def test_default_statuses_excludes_closed(digest) -> None:
    defaults = set(digest.DEFAULT_STATUSES)
    # Closed rows must not appear in the default digest.
    assert repo.STATUS_MERGED not in defaults
    assert repo.STATUS_RESOLVED not in defaults
    assert repo.STATUS_REJECTED_PERM not in defaults
    # Active rows must all appear.
    assert {
        repo.STATUS_OPEN,
        repo.STATUS_AUTHORING,
        repo.STATUS_AWAITING,
        repo.STATUS_ARCHITECT,
        repo.STATUS_REJECTED_ONCE,
        repo.STATUS_REJECTED_TWICE,
    }.issubset(defaults)


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------

def test_parse_since_accepts_iso(digest) -> None:
    assert digest._parse_since("2026-04-10") == "2026-04-10"
    assert digest._parse_since("2026-04-10T12:00:00+00:00").startswith("2026-04-10")
    # Trailing Z is normalized internally but original string is returned.
    assert digest._parse_since("2026-04-10T12:00:00Z") == "2026-04-10T12:00:00Z"


def test_parse_since_rejects_non_iso(digest) -> None:
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        digest._parse_since("not-a-date")
    with pytest.raises(argparse.ArgumentTypeError):
        digest._parse_since("04/10/2026")


# ---------------------------------------------------------------------------
# _filter_since
# ---------------------------------------------------------------------------

def test_filter_since_drops_older_rows(digest) -> None:
    rows = [
        {"id": 1, "last_action_at": "2026-04-01T00:00:00+00:00"},
        {"id": 2, "last_action_at": "2026-04-15T00:00:00+00:00"},
        {"id": 3, "detected_at": "2026-03-01T00:00:00+00:00",
         "last_action_at": None},
    ]
    kept = digest._filter_since(rows, "2026-04-10")
    assert [r["id"] for r in kept] == [2]


def test_filter_since_noop_when_none(digest) -> None:
    rows = [{"id": 1, "last_action_at": "2026-04-01"}]
    assert digest._filter_since(rows, None) == rows


# ---------------------------------------------------------------------------
# main() -- empty queue
# ---------------------------------------------------------------------------

def test_main_empty_queue_returns_zero_with_marker(digest, db: str) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = digest.main(["--db-path", db])
    assert rc == 0
    out = buf.getvalue()
    assert "# Incident Digest" in out
    assert "_No active incidents._" in out


# ---------------------------------------------------------------------------
# main() -- markdown rendering
# ---------------------------------------------------------------------------

def test_main_md_rolls_up_by_invariant(digest, db: str) -> None:
    _register(db, "KEY_A1", invariant="INV_A")
    _register(db, "KEY_A2", invariant="INV_A")
    _register(db, "KEY_B1", invariant="INV_B")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = digest.main(["--db-path", db])
    out = buf.getvalue()

    assert rc == 0
    assert "**Total active:** 3" in out
    assert "**Invariants touched:** 2" in out
    assert "### `INV_A` (2)" in out
    assert "### `INV_B` (1)" in out
    # Every row prints its key inside backticks.
    assert "`KEY_A1`" in out
    assert "`KEY_A2`" in out
    assert "`KEY_B1`" in out


def test_main_md_shows_mr_badge_when_attached(digest, db: str) -> None:
    _register(db, "ATTACHED_KEY", invariant="INV_C")
    # Walk the row through authoring -> awaiting with an MR attached.
    row = repo.get_by_key("ATTACHED_KEY", active_only=True, db_path=db)
    repo.mark_authoring(row["id"], db_path=db)
    repo.mark_awaiting_approval(row["id"], mr_iid=4242, db_path=db)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = digest.main(["--db-path", db])
    out = buf.getvalue()

    assert rc == 0
    assert "MR !4242" in out
    assert "awaiting_approval" in out


# ---------------------------------------------------------------------------
# main() -- JSON format
# ---------------------------------------------------------------------------

def test_main_json_round_trip(digest, db: str) -> None:
    _register(db, "JSON_KEY", invariant="INV_J")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = digest.main(["--db-path", db, "--format", "json"])
    assert rc == 0

    parsed = json.loads(buf.getvalue())
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["incident_key"] == "JSON_KEY"
    assert parsed[0]["invariant_id"] == "INV_J"
    assert parsed[0]["status"] == repo.STATUS_OPEN


# ---------------------------------------------------------------------------
# main() -- --status override
# ---------------------------------------------------------------------------

def test_main_status_filter_overrides_default(digest, db: str) -> None:
    _register(db, "OPEN_ROW", invariant="INV_S")
    # Second row -> authoring via the state machine.
    _register(db, "AUTHORING_ROW", invariant="INV_S")
    r2 = repo.get_by_key("AUTHORING_ROW", active_only=True, db_path=db)
    repo.mark_authoring(r2["id"], db_path=db)

    # --status open should only return OPEN_ROW.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = digest.main([
            "--db-path", db, "--format", "json",
            "--status", repo.STATUS_OPEN,
        ])
    assert rc == 0
    parsed = json.loads(buf.getvalue())
    keys = {r["incident_key"] for r in parsed}
    assert keys == {"OPEN_ROW"}


def test_main_status_filter_multiple(digest, db: str) -> None:
    _register(db, "K_OPEN", invariant="INV_M")
    _register(db, "K_AUTH", invariant="INV_M")
    r = repo.get_by_key("K_AUTH", active_only=True, db_path=db)
    repo.mark_authoring(r["id"], db_path=db)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = digest.main([
            "--db-path", db, "--format", "json",
            "--status", repo.STATUS_OPEN,
            "--status", repo.STATUS_AUTHORING,
        ])
    assert rc == 0
    parsed = json.loads(buf.getvalue())
    assert {r["incident_key"] for r in parsed} == {"K_OPEN", "K_AUTH"}


def test_main_default_excludes_merged(digest, db: str) -> None:
    _register(db, "TO_MERGE", invariant="INV_X")
    r = repo.get_by_key("TO_MERGE", active_only=True, db_path=db)
    repo.mark_authoring(r["id"], db_path=db)
    repo.mark_awaiting_approval(r["id"], mr_iid=9, db_path=db)
    repo.mark_merged(r["id"], db_path=db)

    _register(db, "STILL_OPEN", invariant="INV_X")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = digest.main(["--db-path", db, "--format", "json"])
    assert rc == 0
    parsed = json.loads(buf.getvalue())
    keys = {r["incident_key"] for r in parsed}
    assert keys == {"STILL_OPEN"}
    assert "TO_MERGE" not in keys


# ---------------------------------------------------------------------------
# main() -- DB read failure
# ---------------------------------------------------------------------------

def test_main_bad_db_path_exits_two(digest, tmp_path: Path) -> None:
    bogus = tmp_path / "does_not_exist.db"
    err = io.StringIO()
    with redirect_stderr(err):
        rc = digest.main([
            "--db-path", str(bogus),
            "--status", repo.STATUS_OPEN,
        ])
    assert rc == 2
    assert "incidents_digest" in err.getvalue()
