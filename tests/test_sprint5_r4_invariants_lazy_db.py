"""
tests/test_sprint5_r4_invariants_lazy_db.py

Sprint 5 R4 regression guard — post-MR-B callsites that still read
`agt_db.DB_PATH` directly instead of `agt_db.get_db_path()`.

Scope: This is the 4th regression from the Sprint 5 A+B+C+D deploy. MR !225
fixed R1 (agt_scheduler.py:943) and R2/R3 (async executor, deploy.ps1). R4
surfaced post-MR-225 in the scheduler's invariants runner:

    agt_equities/invariants/checks.py:726
    write_path = Path(agt_db.DB_PATH).resolve()

Because `agt_db.DB_PATH` is `None` at module import time under MR B's
lazy-resolve contract, the Path() constructor raises TypeError, which the
invariants runner catches and records as a "degraded" Violation. The
SELF_HEALING_WRITE_PATH_CANONICAL invariant silently stopped protecting the
canonical-write-path invariant on every scheduler tick until this fix.

A sibling site in scripts/check_invariants.py:48 (CLI entry) was fixed for
consistency — invoking the CLI without `--db` would hit the same pattern.

The fix pattern (matches R1): `agt_db.DB_PATH` -> `agt_db.get_db_path()`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


REPO = Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# R4 site 1: agt_equities/invariants/checks.py (SELF_HEALING check)
# ---------------------------------------------------------------------------


def test_r4_invariants_checks_uses_get_db_path():
    src = _read(REPO / "agt_equities" / "invariants" / "checks.py")
    assert "Path(agt_db.get_db_path()).resolve()" in src, (
        "Sprint 5 R4: agt_equities/invariants/checks.py (SELF_HEALING check) "
        "must call get_db_path() — not read agt_db.DB_PATH directly. "
        "DB_PATH is None post-MR-B until lazy-resolved."
    )


def test_r4_invariants_checks_no_direct_dbpath_read():
    """Belt-and-braces: no `agt_db.DB_PATH` token at all in the module."""
    src = _read(REPO / "agt_equities" / "invariants" / "checks.py")
    assert "agt_db.DB_PATH" not in src, (
        "Sprint 5 R4: agt_equities/invariants/checks.py must not read "
        "agt_db.DB_PATH directly. Use get_db_path()."
    )


# ---------------------------------------------------------------------------
# R4 site 2: scripts/check_invariants.py (CLI entry)
# ---------------------------------------------------------------------------


def test_r4_check_invariants_cli_uses_get_db_path():
    src = _read(REPO / "scripts" / "check_invariants.py")
    assert "str(agt_db.get_db_path())" in src, (
        "Sprint 5 R4: scripts/check_invariants.py CLI must call "
        "get_db_path() when --db is not supplied — str(None) would produce "
        "the literal 'None' path."
    )


def test_r4_check_invariants_cli_no_direct_dbpath_read():
    """Belt-and-braces: no `agt_db.DB_PATH` token in the CLI module."""
    src = _read(REPO / "scripts" / "check_invariants.py")
    assert "agt_db.DB_PATH" not in src, (
        "Sprint 5 R4: scripts/check_invariants.py must not read "
        "agt_db.DB_PATH directly."
    )


# ---------------------------------------------------------------------------
# Functional guard: running the SELF_HEALING check with DB_PATH=None must
# not TypeError. It may still return a Violation about canonical mismatch,
# but the *Path() call itself* must succeed.
# ---------------------------------------------------------------------------


def test_r4_self_healing_check_does_not_typeerror_on_module_path_none(
    tmp_path, monkeypatch
):
    """With agt_db.DB_PATH attribute set to None but AGT_DB_PATH env var
    resolving via get_db_path(), the SELF_HEALING invariant check must
    complete without a TypeError from Path(None).resolve()."""
    import sqlite3
    from agt_equities import db as agt_db
    from agt_equities.invariants import checks

    # Seed a minimal DB so get_ro_connection doesn't fail first
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE daemon_heartbeat ("
        "daemon_name TEXT PRIMARY KEY, last_beat_utc TEXT NOT NULL, "
        "pid INTEGER NOT NULL, client_id INTEGER, notes TEXT)"
    )
    conn.commit()
    conn.close()

    # Wipe module attr, set env so get_db_path() resolves via env
    monkeypatch.setattr(agt_db, "DB_PATH", None, raising=False)
    monkeypatch.setenv("AGT_DB_PATH", str(db_file))

    # Call the invariant — must not TypeError
    try:
        violations = checks.check_self_healing_write_path_canonical(None, {})
    except TypeError as exc:
        pytest.fail(
            f"R4 regression: SELF_HEALING check raised TypeError on module "
            f"DB_PATH=None: {exc}. get_db_path() should be used."
        )
    # Any violation returned is fine — the point is no TypeError.
    assert isinstance(violations, list), (
        "check_self_healing_write_path_canonical must return a list."
    )
