"""ADR-010 §6.1 structural sentinel.

Invariant NO_UNCACHED_LLM_CALL_IN_HOT_PATH: `anthropic` may only be
imported from agt_equities/cached_client.py. Any other module
importing it bypasses budget, cache, and audit — rejected at CI.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


AGT_EQUITIES_DIR = Path(__file__).resolve().parent.parent / "agt_equities"
ALLOWLIST = {"cached_client.py"}


def _iter_py_files():
    for path in AGT_EQUITIES_DIR.rglob("*.py"):
        if path.name in ALLOWLIST:
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def _imports_anthropic(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "anthropic" or alias.name.startswith("anthropic."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "anthropic" or node.module.startswith("anthropic.")):
                return True
    return False


def test_no_raw_anthropic_imports():
    """Only agt_equities/cached_client.py may import anthropic."""
    offenders: list[str] = []
    for path in _iter_py_files():
        source = path.read_text(encoding="utf-8")
        if _imports_anthropic(source):
            offenders.append(str(path.relative_to(AGT_EQUITIES_DIR.parent)))
    assert not offenders, (
        f"anthropic imported outside cached_client.py — ADR-010 §6.1 violation: {offenders}"
    )


def test_cached_client_file_exists_and_imports_anthropic():
    """Sanity check: the one file that IS supposed to import anthropic does."""
    target = AGT_EQUITIES_DIR / "cached_client.py"
    assert target.exists(), "cached_client.py must exist"
    source = target.read_text(encoding="utf-8")
    assert _imports_anthropic(source), "cached_client.py must import anthropic (the only module that may)"
