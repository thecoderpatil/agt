"""Regression test for MR !93 — ib_async trades list must be hydrated on (re)connect.

Without this, pending_orders rows placed before a transient disconnect become
"ghost" rows: live+Submitted at IB but invisible to our event handlers because
IB has no active subscription for the client. Root-caused from the 2026-04-17
09:40 CSP batch: 16 orders stuck at ib_perm_id=0 / last_ib_status='sent'.

This is a static source-level check because ensure_ib_connected is >60 lines
and not cheaply mockable at unit-test scope. The guarantee we need is that the
call exists inside the function body after handler registration.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TARGET = _REPO_ROOT / "telegram_bot.py"


def _find_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    return None


def _calls_in(node: ast.AST) -> list[str]:
    calls: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute):
                calls.append(func.attr)
            elif isinstance(func, ast.Name):
                calls.append(func.id)
    return calls


def test_ensure_ib_connected_hydrates_trades_list():
    src = _TARGET.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _find_function(tree, "ensure_ib_connected")
    assert fn is not None, "ensure_ib_connected not found in telegram_bot.py"
    calls = _calls_in(fn)
    assert "reqAllOpenOrdersAsync" in calls, (
        "ensure_ib_connected must call reqAllOpenOrdersAsync after handler "
        "registration. Without it, ib_async's in-memory trades list is empty "
        "on reconnect and IB never pushes orderStatus/openOrder events for "
        "orders placed before the disconnect."
    )


def test_hydrate_call_is_after_handler_registration():
    """Order matters: handlers must be registered before hydrate, else events
    pushed by IB during reqAllOpenOrdersAsync would be dropped."""
    src = _TARGET.read_text(encoding="utf-8")
    idx_handlers = src.find("execDetailsEvent += _offload_fill_handler(_on_cc_fill)")
    idx_hydrate = src.find("await candidate.reqAllOpenOrdersAsync()")
    assert idx_handlers > 0, "handler registration marker not found"
    assert idx_hydrate > 0, "hydrate call not found"
    assert idx_hydrate > idx_handlers, (
        "reqAllOpenOrdersAsync must be called after handler registration in "
        "ensure_ib_connected, otherwise events pushed during hydration are "
        "dropped on the floor."
    )
