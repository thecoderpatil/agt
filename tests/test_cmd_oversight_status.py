"""tests/test_cmd_oversight_status.py — ADR-017 §9 Mega-MR C.

Covers cmd_oversight_status:
  - renders the observability card via A.1 helpers
  - offloads DB read to asyncio.to_thread
  - fail-soft with brief error response on snapshot raise
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.sprint_a

_BOT_PATH = Path(__file__).parent.parent / "telegram_bot.py"


def _load_cmd():
    src = _BOT_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    fn_node = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "cmd_oversight_status"):
            fn_node = node
            break
    assert fn_node is not None
    ns: dict = {
        "logger": SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        ),
        "asyncio": __import__("asyncio"),
        "is_authorized": lambda upd: True,
        "send_reply": AsyncMock(),
    }
    exec(compile(ast.Module(body=[fn_node], type_ignores=[]), str(_BOT_PATH), "exec"), ns)
    return ns


@pytest.mark.asyncio
async def test_oversight_status_renders_card():
    ns = _load_cmd()
    handler = ns["cmd_oversight_status"]
    send_reply = ns["send_reply"]

    build_mock = MagicMock(return_value="SNAP")
    render_mock = MagicMock(return_value="rendered card")
    flags_mock = MagicMock(return_value=["f1"])

    digest_mod = SimpleNamespace(
        build_observability_snapshot=build_mock,
        render_observability_card=render_mock,
    )
    thresholds_mod = SimpleNamespace(compute_threshold_flags=flags_mock)

    update = SimpleNamespace()
    context = SimpleNamespace()

    with patch.dict(
        "sys.modules",
        {
            "agt_equities.observability.digest": digest_mod,
            "agt_equities.observability.thresholds": thresholds_mod,
        },
    ):
        await handler(update, context)

    build_mock.assert_called_once()
    flags_mock.assert_called_once()
    render_mock.assert_called_once_with("SNAP", threshold_flags=["f1"])
    send_reply.assert_awaited_once()
    args = send_reply.await_args.args
    assert args[1] == "rendered card"


@pytest.mark.asyncio
async def test_oversight_status_db_read_offloaded_to_thread():
    """asyncio.to_thread is called to offload the blocking snapshot build."""
    ns = _load_cmd()
    handler = ns["cmd_oversight_status"]

    digest_mod = SimpleNamespace(
        build_observability_snapshot=MagicMock(return_value="SNAP"),
        render_observability_card=MagicMock(return_value="card"),
    )
    thresholds_mod = SimpleNamespace(compute_threshold_flags=MagicMock(return_value=[]))

    update = SimpleNamespace()
    context = SimpleNamespace()

    with patch.dict(
        "sys.modules",
        {
            "agt_equities.observability.digest": digest_mod,
            "agt_equities.observability.thresholds": thresholds_mod,
        },
    ), patch("asyncio.to_thread", new_callable=AsyncMock) as tt_mock:
        tt_mock.return_value = "card"
        await handler(update, context)

    tt_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_oversight_status_fail_soft_on_snapshot_error():
    ns = _load_cmd()
    handler = ns["cmd_oversight_status"]
    send_reply = ns["send_reply"]

    def boom():
        raise RuntimeError("snapshot dead")

    digest_mod = SimpleNamespace(
        build_observability_snapshot=boom,
        render_observability_card=MagicMock(),
    )

    update = SimpleNamespace()
    context = SimpleNamespace()

    with patch.dict("sys.modules",
                    {"agt_equities.observability.digest": digest_mod}):
        await handler(update, context)

    # Must have attempted the user-facing error reply.
    calls_text = [c.args[1] for c in send_reply.await_args_list]
    assert any("oversight_status failed" in t for t in calls_text), calls_text
