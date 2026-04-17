"""Regression: telegram_bot.py must import sys at module level.

Bug history (2026-04-17): telegram_bot.py had three ``sys.*`` call sites
(``sys.platform`` in ``_pid_is_alive``, ``sys.stderr`` + ``sys.exit`` in
``_acquire_singleton_lock``) but no module-level ``import sys``. On a
restart with a stale ``.bot.pid`` file, ``_pid_is_alive()`` would raise
``NameError: name 'sys' is not defined`` instead of returning the
dead-process signal — leaving the singleton lock check half-broken.

This guard asserts the import is present at module scope so future
refactors (or auto-formatters pruning "unused" imports) cannot silently
drop it. Parsing via AST keeps the test free of ``telegram_bot`` import
side effects (which run ``init_db()`` at module load and would otherwise
require the tripwire-exempt marker).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _module_imports() -> set[str]:
    src = (_REPO_ROOT / "telegram_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    return {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }


def test_telegram_bot_imports_sys_at_module_level():
    imports = _module_imports()
    assert "sys" in imports, (
        "telegram_bot.py must import sys at module level — "
        "_acquire_singleton_lock() and _pid_is_alive() depend on it. "
        "Removing this import re-introduces the stale-PID NameError crash."
    )


def test_telegram_bot_sys_call_sites_still_exist():
    """Sanity: the call sites that require ``sys`` are still present.

    If someone removes the usages *and* the import in the same change,
    the first test passes vacuously. This test pins the three known
    call sites so a silent regression (half of the pair deleted) shows
    up as a test failure instead of a latent runtime bug.
    """
    src = (_REPO_ROOT / "telegram_bot.py").read_text(encoding="utf-8")
    assert "sys.platform" in src, "sys.platform usage (in _pid_is_alive) missing"
    assert "sys.stderr" in src, "sys.stderr usage (in _acquire_singleton_lock) missing"
    assert "sys.exit" in src, "sys.exit usage (in _acquire_singleton_lock) missing"
