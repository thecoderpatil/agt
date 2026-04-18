"""Tests for MR 5.5: hydrate-on-reconnect timeout + reentrancy lock.

sprint_a: no IB connection, no DB writes.

Tests 1-3: asyncio.wait_for semantics + source sentinels.
Tests 4-5: _reconnect_lock reentrancy guard on _auto_reconnect.
"""
from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# 1. wait_for timeout semantics (pure asyncio, no bot import)
# ---------------------------------------------------------------------------

def test_wait_for_raises_asyncio_timeout_error_on_slow_call():
    """asyncio.wait_for propagates asyncio.TimeoutError on timeout."""
    async def slow():
        await asyncio.sleep(10)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(slow(), timeout=0.01))


# ---------------------------------------------------------------------------
# 2. Source sentinels — require MR 5.5 changes in telegram_bot.py
# ---------------------------------------------------------------------------

def test_ensure_ib_connected_hydration_uses_wait_for():
    """ensure_ib_connected wraps reqAllOpenOrdersAsync in asyncio.wait_for."""
    import telegram_bot
    src = inspect.getsource(telegram_bot.ensure_ib_connected)
    assert "asyncio.wait_for(" in src, "ensure_ib_connected must use asyncio.wait_for"
    assert "reqAllOpenOrdersAsync" in src
    assert "timeout=30.0" in src


def test_no_bare_await_reqAllOpenOrdersAsync_in_reconnect_path():
    """Bare await reqAllOpenOrdersAsync must not appear in the reconnect path."""
    import telegram_bot
    src_reconnect = inspect.getsource(telegram_bot._do_auto_reconnect)
    src_1101 = inspect.getsource(telegram_bot._handle_1101_data_lost)

    # Both functions must use wait_for, not bare awaits, for these calls
    for src, fname in [(src_reconnect, "_do_auto_reconnect"), (src_1101, "_handle_1101_data_lost")]:
        assert "asyncio.wait_for(" in src, f"{fname} must use asyncio.wait_for"
        assert "timeout=30.0" in src, f"{fname} must set timeout=30.0"


def test_asyncio_timeout_error_caught_in_ensure_ib_connected():
    """ensure_ib_connected source must catch asyncio.TimeoutError explicitly."""
    import telegram_bot
    src = inspect.getsource(telegram_bot.ensure_ib_connected)
    assert "asyncio.TimeoutError" in src, (
        "ensure_ib_connected must catch asyncio.TimeoutError from wait_for"
    )


# ---------------------------------------------------------------------------
# 3. _reconnect_lock attribute
# ---------------------------------------------------------------------------

def test_reconnect_lock_is_asyncio_lock():
    import telegram_bot
    assert isinstance(telegram_bot._reconnect_lock, asyncio.Lock), (
        "_reconnect_lock must be an asyncio.Lock instance"
    )


# ---------------------------------------------------------------------------
# 4. _auto_reconnect skips when lock is already held
# ---------------------------------------------------------------------------

def test_auto_reconnect_skips_when_lock_contended(caplog):
    """Second _auto_reconnect invocation while lock is held logs and returns."""
    import logging
    import telegram_bot

    async def run():
        # Hold the lock to simulate another reconnect in progress
        await telegram_bot._reconnect_lock.acquire()
        try:
            with patch.object(
                telegram_bot, "_do_auto_reconnect", new_callable=AsyncMock
            ) as mock_impl:
                await telegram_bot._auto_reconnect()
                return mock_impl.call_count
        finally:
            telegram_bot._reconnect_lock.release()

    with caplog.at_level(logging.WARNING, logger="agt_bridge"):
        call_count = asyncio.run(run())

    assert call_count == 0, "_do_auto_reconnect must NOT be called when lock is contended"
    assert any("lock contended" in r.message.lower() for r in caplog.records), (
        "Must log a 'lock contended' warning when skipping"
    )


# ---------------------------------------------------------------------------
# 5. _reconnect_lock released after _auto_reconnect completes
# ---------------------------------------------------------------------------

def test_reconnect_lock_released_after_successful_completion():
    """_reconnect_lock is released after _auto_reconnect finishes."""
    import telegram_bot

    async def run():
        with patch.object(
            telegram_bot, "_do_auto_reconnect", new_callable=AsyncMock
        ):
            await telegram_bot._auto_reconnect()
        return telegram_bot._reconnect_lock.locked()

    locked_after = asyncio.run(run())
    assert not locked_after, "_reconnect_lock must be released after _auto_reconnect completes"
