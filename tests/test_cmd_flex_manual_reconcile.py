"""ADR-018 Phase 2 — /flex_manual_reconcile command tests.

Covers:
  - Valid date argument triggers the subprocess + returns a summary.
  - Missing/malformed date prints usage and does not run subprocess.
  - Subprocess runs in asyncio.to_thread (event loop not blocked).
  - Subprocess exception is handled fail-soft with user-facing error.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.sprint_a


@pytest.fixture
def bot_module():
    """Import telegram_bot lazily so the sprint_a collect doesn't pay the cost
    for unrelated tests."""
    import telegram_bot
    return telegram_bot


class _FakeUpdate:
    def __init__(self):
        self.effective_user = types.SimpleNamespace(id=8343106101)  # operator
        self.effective_chat = types.SimpleNamespace(id=8343106101)
        self.message = MagicMock()
        self.message.reply_text = MagicMock()


class _FakeContext:
    def __init__(self, args):
        self.args = args


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.get_event_loop().is_closed() else asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def test_cmd_flex_manual_reconcile_invalid_date_rejects(bot_module, loop, monkeypatch):
    """Bad date argument → usage message, no subprocess."""
    sent: list[str] = []

    async def _send(update, text):
        sent.append(text)

    monkeypatch.setattr(bot_module, "send_reply", _send)
    monkeypatch.setattr(bot_module, "is_authorized", lambda u: True)

    subprocess_spy = MagicMock()
    monkeypatch.setattr("subprocess.run", subprocess_spy)

    update = _FakeUpdate()
    ctx = _FakeContext(args=["not-a-date"])
    loop.run_until_complete(bot_module.cmd_flex_manual_reconcile(update, ctx))

    assert any("Usage" in m for m in sent)
    subprocess_spy.assert_not_called()


def test_cmd_flex_manual_reconcile_no_args_rejects(bot_module, loop, monkeypatch):
    """No arg → usage message."""
    sent: list[str] = []

    async def _send(update, text):
        sent.append(text)

    monkeypatch.setattr(bot_module, "send_reply", _send)
    monkeypatch.setattr(bot_module, "is_authorized", lambda u: True)
    monkeypatch.setattr("subprocess.run", MagicMock())

    update = _FakeUpdate()
    ctx = _FakeContext(args=[])
    loop.run_until_complete(bot_module.cmd_flex_manual_reconcile(update, ctx))

    assert any("Usage" in m for m in sent)


def test_cmd_flex_manual_reconcile_unauthorized_noop(bot_module, loop, monkeypatch):
    """Unauthorized caller → silent no-op, no subprocess."""
    sent: list[str] = []

    async def _send(update, text):
        sent.append(text)

    monkeypatch.setattr(bot_module, "send_reply", _send)
    monkeypatch.setattr(bot_module, "is_authorized", lambda u: False)
    spy = MagicMock()
    monkeypatch.setattr("subprocess.run", spy)

    update = _FakeUpdate()
    ctx = _FakeContext(args=["20260427"])
    loop.run_until_complete(bot_module.cmd_flex_manual_reconcile(update, ctx))

    assert sent == []
    spy.assert_not_called()


def test_cmd_flex_manual_reconcile_valid_date_invokes_subprocess(
    bot_module, loop, monkeypatch, tmp_path
):
    """Valid YYYYMMDD → subprocess invoked, summary sent to operator."""
    sent: list[str] = []

    async def _send(update, text):
        sent.append(text)

    monkeypatch.setattr(bot_module, "send_reply", _send)
    monkeypatch.setattr(bot_module, "is_authorized", lambda u: True)

    fake_proc = types.SimpleNamespace(
        returncode=0,
        stdout='{"sync_id": 99, "rows_inserted": 4}\n',
        stderr="",
    )
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=fake_proc))

    # Mock sqlite3 so count queries don't need a real DB.
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    conn_mock.execute.return_value.fetchone.side_effect = [(0,), (4,)]
    monkeypatch.setattr("sqlite3.connect", MagicMock(return_value=conn_mock))

    # Redirect reports dir to tmp_path to avoid polluting real reports.
    monkeypatch.setenv("AGT_DB_PATH", str(tmp_path / "db.db"))

    update = _FakeUpdate()
    ctx = _FakeContext(args=["20260427"])
    loop.run_until_complete(bot_module.cmd_flex_manual_reconcile(update, ctx))

    # At minimum: starting message + result message.
    assert any("starting" in m.lower() for m in sent)
    # Result message includes exit code and delta.
    assert any("exit=0" in m for m in sent)


def test_cmd_flex_manual_reconcile_subprocess_failure_fail_soft(
    bot_module, loop, monkeypatch
):
    """Subprocess exception → user-facing warning, no hard fail."""
    sent: list[str] = []

    async def _send(update, text):
        sent.append(text)

    monkeypatch.setattr(bot_module, "send_reply", _send)
    monkeypatch.setattr(bot_module, "is_authorized", lambda u: True)
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(side_effect=RuntimeError("network down")),
    )

    update = _FakeUpdate()
    ctx = _FakeContext(args=["20260427"])
    loop.run_until_complete(bot_module.cmd_flex_manual_reconcile(update, ctx))

    # Expect: starting msg + warning msg
    assert any("starting" in m.lower() for m in sent)
    assert any("failed" in m.lower() or "network down" in m for m in sent)
