"""A4 (Decoupling Sprint A) — telegram_bot does not call init_db at import.

Static-source / AST regression guard. Pre-A4, telegram_bot.py:454 invoked
``init_db()`` at module scope, which fired ``sqlite3.connect(DB_PATH)``
before any test-level patch could intercept and silently mutated the
production DB. A4 moves the call into ``main()`` so importing the module
is side-effect-free.

This test is intentionally a static check rather than a live ``import
telegram_bot``: telegram_bot pulls heavy deps (python-telegram-bot,
fastapi, ib_async, anthropic SDK, …) that are deliberately NOT in
requirements-ci.txt. The regression we care about — *where* ``init_db()``
is called — is fully visible from the source AST.

Asserts:

1. The module body of telegram_bot.py contains ZERO ``init_db()`` Call
   expressions at top level.
2. The body of ``def init_db`` is a ``FunctionDef`` (still defined).
3. The body of ``def main`` contains an ``init_db()`` Call expression.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


REPO_ROOT = Path(__file__).resolve().parent.parent
TELEGRAM_BOT_PY = REPO_ROOT / "telegram_bot.py"


def _parse() -> ast.Module:
    src = TELEGRAM_BOT_PY.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(TELEGRAM_BOT_PY))


def _is_init_db_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "init_db"
    )


def test_telegram_bot_source_exists():
    assert TELEGRAM_BOT_PY.is_file(), f"missing {TELEGRAM_BOT_PY}"


def test_no_module_level_init_db_call():
    """The module body must not contain a top-level init_db() call."""
    tree = _parse()
    offenders = [
        n.lineno for n in tree.body if _is_init_db_call(n)
    ]
    assert not offenders, (
        f"telegram_bot.py has module-level init_db() call(s) at "
        f"line(s) {offenders} — A4 regression."
    )


def test_init_db_function_still_defined():
    tree = _parse()
    fns = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "init_db"
    ]
    assert len(fns) == 1, (
        f"expected exactly one `def init_db` at module level, "
        f"found {len(fns)}"
    )


def test_main_calls_init_db():
    tree = _parse()
    mains = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    ]
    assert len(mains) == 1, "expected exactly one `def main` at module level"

    def _walk(body):
        for stmt in body:
            for sub in ast.walk(stmt):
                if _is_init_db_call(sub):
                    return True
        return False

    assert _walk(mains[0].body), (
        "main() body does not call init_db() — daemon boot would skip "
        "schema registration. A4 regression."
    )
