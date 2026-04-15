"""Sprint A unit A5d.c — dead staged_alert coalescing buffer removed.

Pure-AST + textual checks that telegram_bot.py no longer contains the
unused Sprint 1D `_staged_alert_buffer` machinery (replaced end-to-end by
the cross_daemon_alerts bus in A5b/c/d). This is a resurrection-prevention
guard: if anyone re-introduces these symbols, CI fails here.

Scope explicitly removed:
  * globals: `_staged_alert_buffer`, `_staged_alert_last_flush`,
    `STAGED_COALESCE_WINDOW`
  * async def `_flush_staged_alerts_job`
  * `jq.run_repeating(name="staged_alert_flush", ...)` registration

Static-only — does NOT live-import telegram_bot. Mirrors the pattern used
in tests/test_a5d_consumer_wired.py.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.sprint_a

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TELEGRAM_BOT = REPO_ROOT / "telegram_bot.py"


def _read_bot_source() -> str:
    return TELEGRAM_BOT.read_text(encoding="utf-8")


def _parse_bot_source() -> ast.Module:
    # python:3.12-slim CI container parses 3.11+ syntax (f-strings with
    # backslashes). Local 3.10 sandbox cannot — that's expected and is why
    # this guard runs in CI.
    return ast.parse(_read_bot_source())


# ---------------------------------------------------------------------------
# Textual absence — catches both raw identifier references and string names
# used in jq.run_repeating(name=...). Covers accidental reintroduction.
# ---------------------------------------------------------------------------

DEAD_SYMBOLS = (
    "_staged_alert_buffer",
    "_staged_alert_last_flush",
    "STAGED_COALESCE_WINDOW",
    "_flush_staged_alerts_job",
    "staged_alert_flush",
)


@pytest.mark.parametrize("needle", DEAD_SYMBOLS)
def test_a5dc_dead_symbol_absent(needle: str) -> None:
    src = _read_bot_source()
    assert needle not in src, (
        f"dead Sprint 1D symbol {needle!r} resurfaced in telegram_bot.py; "
        f"the staged_alert coalescing buffer was deleted in A5d.c. Any "
        f"new coalescing should ride the cross_daemon_alerts bus."
    )


# ---------------------------------------------------------------------------
# AST absence — stronger guard: no async def with the old name, no globals
# with those names, no jq.run_repeating(name='staged_alert_flush') call.
# ---------------------------------------------------------------------------


def test_a5dc_flush_job_def_absent() -> None:
    tree = _parse_bot_source()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name != "_flush_staged_alerts_job", (
                "_flush_staged_alerts_job function definition still present"
            )


def test_a5dc_dead_globals_absent() -> None:
    tree = _parse_bot_source()
    dead_names = {"_staged_alert_buffer", "_staged_alert_last_flush",
                  "STAGED_COALESCE_WINDOW"}
    for node in ast.iter_child_nodes(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                assert t.id not in dead_names, (
                    f"dead module-level assignment {t.id!r} still present"
                )


def test_a5dc_staged_alert_flush_registration_absent() -> None:
    """No jq.run_repeating(..., name='staged_alert_flush', ...) anywhere."""
    tree = _parse_bot_source()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
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
                and kw.value.value == "staged_alert_flush"
            ):
                raise AssertionError(
                    "jq.run_repeating(name='staged_alert_flush') registration "
                    "still present; A5d.c deleted it."
                )
