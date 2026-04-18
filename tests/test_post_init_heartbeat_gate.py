"""
MR A regression guard: register_bot_heartbeat must be reached before
ensure_ib_connected() in post_init — even when IB is down on startup.

Backlog triage: 2026-04-18 heartbeat stall root cause.
"""

from __future__ import annotations

import ast
import inspect

import pytest

pytestmark = pytest.mark.sprint_a


def test_heartbeat_registered_before_ib_connect_structural():
    """Static ordering guard: register_bot_heartbeat appears before
    ensure_ib_connected in post_init source.

    Prevents regression without requiring a live IB mock. If someone
    re-gates the heartbeat behind ensure_ib_connected again, this fails.
    """
    from telegram_bot import post_init  # noqa: PLC0415

    src = inspect.getsource(post_init)

    # Use the call site (not the comment) to find each anchor
    hb_call = "register_bot_heartbeat(jq_hb)"
    ib_call = "ib_conn = await ensure_ib_connected()"

    assert hb_call in src, f"'{hb_call}' not found in post_init source"
    assert ib_call in src, f"'{ib_call}' not found in post_init source"

    hb_pos = src.index(hb_call)
    ib_pos = src.index(ib_call)

    assert hb_pos < ib_pos, (
        f"register_bot_heartbeat (char {hb_pos}) must appear before "
        f"ensure_ib_connected (char {ib_pos}) in post_init. "
        "Heartbeat must be registered unconditionally, before any failable init step."
    )


def test_heartbeat_not_inside_ib_connect_try_block():
    """register_bot_heartbeat is not nested inside the ensure_ib_connected
    try block — it must be reachable on the early-return path."""
    from telegram_bot import post_init  # noqa: PLC0415

    tree = ast.parse(inspect.getsource(post_init))

    # Find all Try nodes whose body contains ensure_ib_connected
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        body_src = ast.unparse(node)
        if "ensure_ib_connected" not in body_src:
            continue
        # This is the IB connect try block — heartbeat must NOT be inside it
        assert "register_bot_heartbeat" not in body_src, (
            "register_bot_heartbeat is nested inside the ensure_ib_connected "
            "try block. It must be registered BEFORE that try block."
        )
