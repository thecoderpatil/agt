"""Sentinel: forbid shutil.copy*() of any .db artifact in AGT code paths.

Phase 4b AUDIT-PASS close (2026-04-18). The WAL-aware path is
`agt_equities.runtime.clone_sqlite_db_with_wal()` which uses
sqlite3.Connection.backup(). shutil.copy* on a live SQLite file
can observe a partial transaction + torn WAL.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN_ROOTS = [REPO_ROOT / "agt_equities", REPO_ROOT / "scripts"]
SHUTIL_COPY_NAMES = {"copy", "copy2", "copyfile", "copytree"}


def _iter_py_files():
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            yield p


def _call_targets_shutil_copy(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in SHUTIL_COPY_NAMES:
        if isinstance(func.value, ast.Name) and func.value.id == "shutil":
            return True
    if isinstance(func, ast.Name) and func.id in SHUTIL_COPY_NAMES:
        # Catches `from shutil import copy2; copy2(...)`.
        return True
    return False


def _arg_is_db_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ".db" in node.value.lower()
    if isinstance(node, ast.JoinedStr):  # f-string
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if ".db" in value.value.lower():
                    return True
    return False


@pytest.mark.sprint_a
def test_no_shutil_copy_of_db_files():
    offenders: list[str] = []
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _call_targets_shutil_copy(node):
                continue
            for arg in node.args:
                if _arg_is_db_literal(arg):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
                    break
    assert not offenders, (
        "shutil.copy*() targeting a .db file is forbidden. "
        "Use agt_equities.runtime.clone_sqlite_db_with_wal() instead. "
        f"Offenders: {offenders}"
    )
