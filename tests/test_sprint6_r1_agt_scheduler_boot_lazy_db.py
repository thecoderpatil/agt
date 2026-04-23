"""
tests/test_sprint6_r1_agt_scheduler_boot_lazy_db.py

Sprint 6 Mega-MR 1 §1A — R1 regression guard.

Regression context: Sprint 5 R1 was the scheduler-boot crash on
`agt_scheduler.py:943` that still read `agt_db.DB_PATH` directly after MR B
made it None at import time. Fixed in MR !225 (now line 946:
`assert_canonical_db_path(resolved_path=agt_db.get_db_path())`).

This guard asserts:

1. The boot callsite uses `get_db_path()` (sentinel grep).
2. No AST-level direct `agt_db.DB_PATH` read anywhere in
   `agt_scheduler.py` (belt-and-braces against a future regression that
   might re-introduce the pattern).
3. At runtime with the module attribute forced to None and a valid
   `AGT_DB_PATH` env var, `get_db_path()` resolves successfully and
   returns a non-None path.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO = Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_r1_agt_scheduler_boot_uses_get_db_path():
    """Sentinel: the boot validator must call `get_db_path()`."""
    src = _read(REPO / "agt_scheduler.py")
    assert "agt_db.get_db_path()" in src, (
        "Sprint 6 R1: agt_scheduler.py main() must call agt_db.get_db_path() "
        "for the canonical-DB-path boot assertion. Direct DB_PATH read "
        "regressed on 2026-04-23 17:21 (pre-MR-225 crash)."
    )


def test_r1_agt_scheduler_no_direct_db_path_reads():
    """AST guard: no `agt_db.DB_PATH` / `_agt_db.DB_PATH` load anywhere."""
    src = _read(REPO / "agt_scheduler.py")
    tree = ast.parse(src)
    offending: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "DB_PATH"
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id in ("agt_db", "_agt_db")
        ):
            offending.append(node.lineno)
    assert not offending, (
        f"Sprint 6 R1: agt_scheduler.py reads agt_db.DB_PATH directly at "
        f"lines {offending}. Use agt_db.get_db_path() instead. Regression "
        "class caught by MR !225 hotfix."
    )


def test_r1_get_db_path_resolves_when_module_attr_none(tmp_path, monkeypatch):
    """Functional: DB_PATH module attr None + AGT_DB_PATH env set → resolves."""
    from agt_equities import db as agt_db

    db_file = tmp_path / "r1_test.db"
    db_file.write_bytes(b"")  # Empty file sufficient for path resolution

    monkeypatch.setattr(agt_db, "DB_PATH", None, raising=False)
    monkeypatch.setenv("AGT_DB_PATH", str(db_file))

    resolved = agt_db.get_db_path()
    assert resolved is not None
    assert Path(resolved).resolve() == db_file.resolve()
