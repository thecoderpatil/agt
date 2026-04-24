"""tests/test_oversight_digest_scheduled.py — ADR-017 §9 Mega-MR A.2.

Covers _scheduled_oversight_digest_send:
  - registered on PTB JobQueue with name=oversight_digest_send at 18:35 ET
  - body calls build_observability_snapshot + render_observability_card
  - fail-soft on snapshot failure (no raise out of scheduler)
"""
from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.sprint_a

_BOT_PATH = Path(__file__).parent.parent / "telegram_bot.py"


def test_scheduled_job_registered_with_name_and_time():
    """Source-grep the job registration block — PTB JobQueue.run_daily with name.

    telegram_bot.py registers handlers at import time inside main(), so we
    verify via source inspection (same pattern as command_registry_parity).
    """
    src = _BOT_PATH.read_text(encoding="utf-8", errors="replace")
    # The callback must reference our handler.
    assert "oversight_digest_send" in src
    assert "_scheduled_oversight_digest_send" in src
    # Name keyword.
    assert re.search(r'name\s*=\s*"oversight_digest_send"', src), (
        "JobQueue.run_daily(name='oversight_digest_send', ...) not found"
    )
    # 18:35 ET window.
    assert re.search(r"hour\s*=\s*18\s*,\s*minute\s*=\s*35", src), (
        "run_daily at 18:35 ET not registered"
    )
    # Mon-Fri days tuple (1..5).
    assert re.search(r"days\s*=\s*\(\s*1\s*,\s*2\s*,\s*3\s*,\s*4\s*,\s*5\s*\)", src), (
        "Mon-Fri (days=(1,2,3,4,5)) tuple not present in registration"
    )


def _load_handler():
    """Parse telegram_bot.py and extract the _scheduled_oversight_digest_send function.

    We load the function in isolation (no bot imports) by executing just its AST
    subtree with stubbed module-level names.
    """
    src = _BOT_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    fn_node = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_scheduled_oversight_digest_send"):
            fn_node = node
            break
    assert fn_node is not None, "handler function not found in telegram_bot.py"
    module = ast.Module(body=[fn_node], type_ignores=[])
    ns: dict = {
        "logger": SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        ),
        "asyncio": __import__("asyncio"),
        "AUTHORIZED_USER_ID": 999,
    }
    exec(compile(module, str(_BOT_PATH), "exec"), ns)
    return ns["_scheduled_oversight_digest_send"]


@pytest.mark.asyncio
async def test_scheduled_job_body_calls_build_and_render():
    handler = _load_handler()

    fake_snapshot = object()
    fake_flags = ["flag1"]
    fake_card = "rendered card text"

    build_mock = MagicMock(return_value=fake_snapshot)
    render_mock = MagicMock(return_value=fake_card)
    flags_mock = MagicMock(return_value=fake_flags)

    send_mock = AsyncMock()
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send_mock))

    digest_mod = SimpleNamespace(
        build_observability_snapshot=build_mock,
        render_observability_card=render_mock,
    )
    thresholds_mod = SimpleNamespace(compute_threshold_flags=flags_mock)

    with patch.dict(
        "sys.modules",
        {
            "agt_equities.observability.digest": digest_mod,
            "agt_equities.observability.thresholds": thresholds_mod,
        },
    ):
        await handler(context)

    build_mock.assert_called_once()
    flags_mock.assert_called_once()
    render_mock.assert_called_once_with(fake_snapshot, threshold_flags=fake_flags)
    send_mock.assert_awaited_once()
    _args, kwargs = send_mock.await_args
    assert kwargs.get("chat_id") == 999
    assert kwargs.get("text") == fake_card
    assert kwargs.get("parse_mode") == "Markdown"


@pytest.mark.asyncio
async def test_scheduled_job_fail_soft_on_snapshot_failure():
    handler = _load_handler()

    def boom():
        raise RuntimeError("snapshot dead")

    digest_mod = SimpleNamespace(
        build_observability_snapshot=boom,
        render_observability_card=MagicMock(),
    )
    alerts_mod = SimpleNamespace(enqueue_alert=MagicMock())

    send_mock = AsyncMock()
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send_mock))

    with patch.dict(
        "sys.modules",
        {
            "agt_equities.observability.digest": digest_mod,
            "agt_equities.alerts": alerts_mod,
        },
    ):
        # Must not raise out of the scheduler.
        await handler(context)

    alerts_mod.enqueue_alert.assert_called_once()
    call_args = alerts_mod.enqueue_alert.call_args
    assert call_args.args[0] == "OVERSIGHT_DIGEST_FAILED"
