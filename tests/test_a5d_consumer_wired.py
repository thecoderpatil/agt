"""Sprint A unit A5d — bot-side cross_daemon_alerts consumer wired.

Pure-AST check that telegram_bot.py:
  1. Defines async function _drain_cross_daemon_alerts_job.
  2. Registers it via jq.run_repeating(..., name="cross_daemon_alerts_drain", ...)
     inside main()'s JobQueue setup.

Static-only — does NOT live-import telegram_bot (which pulls
python-telegram-bot, fastapi, ib_async, anthropic etc. NOT in the slim
CI container). Same pattern as tests/test_a4_init_db_lazy.py.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.sprint_a

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TELEGRAM_BOT = REPO_ROOT / "telegram_bot.py"


def _parse_bot_source() -> ast.Module:
    src = TELEGRAM_BOT.read_text(encoding="utf-8")
    # python:3.12-slim CI container parses 3.11+ syntax (f-strings with
    # backslashes). Local 3.10 sandbox cannot — that's expected and is why
    # this guard runs in CI, not locally.
    return ast.parse(src)


def test_a5d_drain_job_def_present() -> None:
    tree = _parse_bot_source()
    found = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_drain_cross_daemon_alerts_job"
    ]
    assert len(found) == 1, (
        f"expected exactly one async def _drain_cross_daemon_alerts_job, "
        f"found {len(found)}"
    )


def test_a5d_drain_job_registered_in_main() -> None:
    """jq.run_repeating(..., name='cross_daemon_alerts_drain', ...) must
    appear inside def main()."""
    tree = _parse_bot_source()
    main_fns = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "main"
    ]
    assert main_fns, "def main not found in telegram_bot.py"

    found = False
    for fn in main_fns:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            # Match jq.run_repeating(...)
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "run_repeating"
                and isinstance(func.value, ast.Name)
                and func.value.id == "jq"
            ):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "name"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "cross_daemon_alerts_drain"
                ):
                    found = True
                    break
            if found:
                break
        if found:
            break

    assert found, (
        "jq.run_repeating(..., name='cross_daemon_alerts_drain') not found "
        "inside def main()"
    )


def test_a5d_drain_job_imports_alerts_module() -> None:
    """Inside _drain_cross_daemon_alerts_job, ensure we import the four
    expected symbols from agt_equities.alerts (drain/sent/failed/format)."""
    tree = _parse_bot_source()
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef)
         and n.name == "_drain_cross_daemon_alerts_job"),
        None,
    )
    assert fn is not None

    expected = {
        "drain_pending_alerts",
        "mark_alert_sent",
        "mark_alert_failed",
        "format_alert_text",
    }
    seen: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module == "agt_equities.alerts":
            for alias in node.names:
                seen.add(alias.name)
    missing = expected - seen
    assert not missing, f"missing imports from agt_equities.alerts: {missing}"
