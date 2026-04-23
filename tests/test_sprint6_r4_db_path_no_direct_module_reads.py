"""
tests/test_sprint6_r4_db_path_no_direct_module_reads.py

Sprint 6 Mega-MR 1 §1D — R4 universal grep guard.

Regression context: Sprint 5 R4 was the `invariants/checks.py:726`
SELF_HEALING invariant callsite that still read `agt_db.DB_PATH`
directly after MR B lazy-resolve. Surfaced 6785 `TypeError(Path(None))`
events/day in scheduler stderr before hotfix MR !226.

R1 + R4 were SAME CLASS of bug in different modules. This universal
guard walks every production `.py` file's AST and asserts no
module-attribute *read* of `agt_db.DB_PATH` / `_agt_db.DB_PATH` exists
anywhere in the runtime surface. Assignments (in `agt_equities/db.py`
itself + test-fixture monkeypatch helpers) are allowed — only reads
are flagged.

Excluded (non-runtime / staging / throwaway):
  - `.staged/**` — pre-commit staging area
  - `scripts/_staging/**` — similar
  - `scripts/patch_*.py`, `scripts/commit_*.py`, `scripts/write_*.py`,
    `scripts/_build_*.py`, `scripts/_telegram_bot_*` — one-shot scripts
  - `tests/**` — tests are allowed to monkeypatch (contract test surface)
  - `agt_equities/db.py` itself — this IS the module defining the attr

Production paths covered:
  - `agt_equities/**/*.py` (except `db.py`)
  - `agt_scheduler.py`
  - `telegram_bot.py`
  - `scripts/*.py` (top-level only, per-file excluded by prefix)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO = Path(__file__).resolve().parent.parent

_EXCLUDE_DIR_FRAGMENTS = (
    "/.staged/",
    "\\.staged\\",
    "/_staging/",
    "\\_staging\\",
    "/tests/",
    "\\tests\\",
)

_EXCLUDE_FILE_PREFIXES = (
    "patch_",
    "commit_",
    "write_",
    "_build_",
    "_telegram_bot",
    "apply_",
    "poll_",
    "merge_",
    "observe_",
    "recon_",
    "summarize_",
    "wait",
)


def _is_excluded(path: Path) -> bool:
    normalized = str(path).replace("\\", "/")
    if any(frag.replace("\\", "/") in normalized for frag in _EXCLUDE_DIR_FRAGMENTS):
        return True
    stem = path.name
    for prefix in _EXCLUDE_FILE_PREFIXES:
        if stem.startswith(prefix):
            return True
    # agt_equities/db.py defines the attribute; it's the one legit read site.
    if path.resolve() == (REPO / "agt_equities" / "db.py").resolve():
        return True
    return False


def _iter_prod_py_files():
    # agt_equities/**
    for py in (REPO / "agt_equities").rglob("*.py"):
        if _is_excluded(py):
            continue
        yield py
    # Top-level scheduler + bot
    for name in ("agt_scheduler.py", "telegram_bot.py"):
        p = REPO / name
        if p.is_file():
            yield p
    # scripts/*.py (top-level only, with prefix exclusions)
    for py in (REPO / "scripts").glob("*.py"):
        if _is_excluded(py):
            continue
        yield py


def test_r4_no_direct_agt_db_DB_PATH_reads_in_prod_code():
    violations: list[str] = []
    scanned = 0
    for py_path in _iter_prod_py_files():
        scanned += 1
        try:
            src = py_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(py_path))
        except SyntaxError:
            # A syntax-broken prod file is a separate problem — don't
            # fail this guard, but do surface.
            violations.append(f"{py_path.relative_to(REPO)}: SYNTAX ERROR")
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "DB_PATH"
                and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name)
                and node.value.id in ("agt_db", "_agt_db")
            ):
                try:
                    rel = py_path.relative_to(REPO)
                except ValueError:
                    rel = py_path
                violations.append(f"{rel}:{node.lineno}")

    assert scanned > 10, (
        f"R4 guard scanned only {scanned} files — exclude list too aggressive."
    )
    assert not violations, (
        "Sprint 6 R4 (universal): direct agt_db.DB_PATH reads found in "
        f"{len(violations)} prod-code site(s):\n"
        + "\n".join(f"  - {v}" for v in violations)
        + "\n\nUse agt_db.get_db_path() per MR B lazy-resolve contract."
    )
