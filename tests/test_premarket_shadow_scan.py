"""Tests for F.5 premarket shadow scan PTB callback."""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

pytestmark = pytest.mark.sprint_a


@pytest.fixture
def premarket_callback():
    """Import the callback by name; fails loudly if renamed."""
    import telegram_bot
    return telegram_bot._premarket_shadow_scan


def test_callback_imports(premarket_callback) -> None:
    """Sanity: the callable exists and is async."""
    assert asyncio.iscoroutinefunction(premarket_callback)


def test_callback_invokes_shadow_scan_main(premarket_callback) -> None:
    """Callback calls shadow_scan.main(["--emit", "telegram"])."""
    with mock.patch("scripts.shadow_scan.main", return_value=0) as m:
        asyncio.run(premarket_callback(context=mock.MagicMock()))
    assert m.called
    args, _ = m.call_args
    assert args[0] == ["--emit", "telegram"]


def test_nonzero_exit_pages_telegram(premarket_callback) -> None:
    """Non-zero exit from shadow_scan.main triggers _alert_telegram."""
    with mock.patch("scripts.shadow_scan.main", return_value=3),          mock.patch("telegram_bot._alert_telegram") as alert:
        asyncio.run(premarket_callback(context=mock.MagicMock()))
    assert alert.called
    call_text = alert.call_args[0][0]
    assert "rc=3" in call_text


def test_exception_pages_telegram(premarket_callback) -> None:
    """Uncaught exception in the thread returns rc=99 and pages telegram."""
    def _boom(*a, **k):
        raise RuntimeError("simulated crash")

    with mock.patch("scripts.shadow_scan.main", side_effect=_boom),          mock.patch("telegram_bot._alert_telegram") as alert:
        asyncio.run(premarket_callback(context=mock.MagicMock()))
    assert alert.called
    call_text = alert.call_args[0][0]
    assert "rc=99" in call_text
