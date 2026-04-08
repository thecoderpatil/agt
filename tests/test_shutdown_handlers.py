"""
F23 — Graceful Shutdown + 1101/1102 Differentiation Tests.

10 tests covering:
- post_shutdown callback (4 tests: disconnect, failure, reconnect cancel, idempotent)
- errorEvent handler (3 tests: 1100, 1101, 1102 code routing)
- _handle_1101_data_lost (3 tests: deferred, edge-case reconciliation, failure alerting)

No live IBKR — mock ib_async objects.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import telegram_bot


class TestPostShutdown(unittest.IsolatedAsyncioTestCase):
    """Tests for _graceful_shutdown (F23-1)."""

    async def test_post_shutdown_disconnects_ibkr(self):
        """Mock ib as connected, call _graceful_shutdown, assert disconnect
        was called and globals are None."""
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True

        original_ib = telegram_bot.ib
        original_task = telegram_bot._reconnect_task
        original_flag = telegram_bot._shutdown_started
        try:
            telegram_bot.ib = mock_ib
            telegram_bot._reconnect_task = None
            telegram_bot._shutdown_started = False

            await telegram_bot._graceful_shutdown(MagicMock())

            mock_ib.disconnect.assert_called_once()
            self.assertIsNone(telegram_bot.ib)
            self.assertIsNone(telegram_bot._reconnect_task)
        finally:
            telegram_bot.ib = original_ib
            telegram_bot._reconnect_task = original_task
            telegram_bot._shutdown_started = original_flag

    async def test_post_shutdown_handles_disconnect_failure(self):
        """Mock ib.disconnect to raise, assert _graceful_shutdown does not
        propagate the exception and still sets ib = None."""
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True
        mock_ib.disconnect.side_effect = RuntimeError("socket error")

        original_ib = telegram_bot.ib
        original_task = telegram_bot._reconnect_task
        original_flag = telegram_bot._shutdown_started
        try:
            telegram_bot.ib = mock_ib
            telegram_bot._reconnect_task = None
            telegram_bot._shutdown_started = False

            # Must not raise
            await telegram_bot._graceful_shutdown(MagicMock())

            self.assertIsNone(telegram_bot.ib)
        finally:
            telegram_bot.ib = original_ib
            telegram_bot._reconnect_task = original_task
            telegram_bot._shutdown_started = original_flag

    async def test_post_shutdown_cancels_reconnect_task(self):
        """Set _reconnect_task to a fake pending task, call _graceful_shutdown,
        assert task was cancelled."""
        cancelled = False

        async def fake_reconnect():
            nonlocal cancelled
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled = True
                raise

        original_ib = telegram_bot.ib
        original_task = telegram_bot._reconnect_task
        original_flag = telegram_bot._shutdown_started
        try:
            telegram_bot.ib = None
            telegram_bot._reconnect_task = asyncio.create_task(fake_reconnect())
            telegram_bot._shutdown_started = False
            # Let the task start
            await asyncio.sleep(0)

            await telegram_bot._graceful_shutdown(MagicMock())

            self.assertTrue(cancelled)
            self.assertIsNone(telegram_bot._reconnect_task)
        finally:
            telegram_bot.ib = original_ib
            telegram_bot._reconnect_task = original_task
            telegram_bot._shutdown_started = original_flag

    async def test_post_shutdown_reentry_guard(self):
        """F23-patch-1: post_shutdown fires once even if PTB calls it 7 times."""
        mock_ib = MagicMock()
        mock_ib.disconnectedEvent = MagicMock()
        mock_ib.errorEvent = MagicMock()
        mock_ib.isConnected.return_value = True

        original_ib = telegram_bot.ib
        original_task = telegram_bot._reconnect_task
        original_flag = telegram_bot._shutdown_started
        try:
            telegram_bot.ib = mock_ib
            telegram_bot._reconnect_task = None
            telegram_bot._shutdown_started = False

            await telegram_bot._graceful_shutdown(MagicMock())
            # Second call — reentry guard should skip
            await telegram_bot._graceful_shutdown(MagicMock())
            # Third call
            await telegram_bot._graceful_shutdown(MagicMock())

            # disconnect called exactly once, not 3 times
            mock_ib.disconnect.assert_called_once()
        finally:
            telegram_bot.ib = original_ib
            telegram_bot._reconnect_task = original_task
            telegram_bot._shutdown_started = original_flag

    async def test_post_shutdown_detaches_both_handlers(self):
        """F23-patch-1: both disconnectedEvent and errorEvent must be
        detached BEFORE disconnect() to prevent 1100/1101/1102 cascade."""
        call_order = []

        class FakeEvent:
            def __init__(self, name):
                self._name = name
            def __isub__(self, handler):
                call_order.append(f"detach_{self._name}")
                return self

        mock_ib = MagicMock()
        mock_ib.disconnectedEvent = FakeEvent("disconnected")
        mock_ib.errorEvent = FakeEvent("error")
        mock_ib.isConnected.return_value = True
        mock_ib.disconnect.side_effect = lambda: call_order.append("disconnect")

        original_ib = telegram_bot.ib
        original_task = telegram_bot._reconnect_task
        original_flag = telegram_bot._shutdown_started
        try:
            telegram_bot.ib = mock_ib
            telegram_bot._reconnect_task = None
            telegram_bot._shutdown_started = False

            await telegram_bot._graceful_shutdown(MagicMock())

            self.assertEqual(
                call_order,
                ["detach_disconnected", "detach_error", "disconnect"],
                "both handlers must detach BEFORE disconnect",
            )
        finally:
            telegram_bot.ib = original_ib
            telegram_bot._reconnect_task = original_task
            telegram_bot._shutdown_started = original_flag


class TestOnIbError(unittest.IsolatedAsyncioTestCase):
    """Tests for _on_ib_error routing (F23-2)."""

    @patch.object(telegram_bot, '_handle_1101_data_lost', new_callable=AsyncMock)
    @patch.object(telegram_bot, '_alert_1102', new_callable=AsyncMock)
    async def test_on_ib_error_1102_no_action(self, mock_1102, mock_1101):
        """errorCode=1102: alert_1102 scheduled, handle_1101 NOT called."""
        telegram_bot._on_ib_error(
            reqId=-1, errorCode=1102,
            errorString="Connectivity restored - data maintained.",
            contract=None,
        )
        # Let any created tasks run
        await asyncio.sleep(0)

        mock_1102.assert_awaited_once()
        mock_1101.assert_not_awaited()

    @patch.object(telegram_bot, '_handle_1101_data_lost', new_callable=AsyncMock)
    @patch.object(telegram_bot, '_alert_1102', new_callable=AsyncMock)
    async def test_on_ib_error_1101_triggers_reconciliation(self, mock_1102, mock_1101):
        """errorCode=1101: handle_1101 scheduled, alert_1102 NOT called."""
        telegram_bot._on_ib_error(
            reqId=-1, errorCode=1101,
            errorString="Connectivity restored - data lost.",
            contract=None,
        )
        await asyncio.sleep(0)

        mock_1101.assert_awaited_once()
        mock_1102.assert_not_awaited()

    @patch.object(telegram_bot, '_handle_1101_data_lost', new_callable=AsyncMock)
    @patch.object(telegram_bot, '_alert_1102', new_callable=AsyncMock)
    async def test_on_ib_error_1100_no_reconciliation(self, mock_1102, mock_1101):
        """errorCode=1100: neither 1101 handler nor 1102 alert triggered
        (1100 is handled by disconnectedEvent)."""
        telegram_bot._on_ib_error(
            reqId=-1, errorCode=1100,
            errorString="Connectivity between IB and TWS has been lost.",
            contract=None,
        )
        await asyncio.sleep(0)

        mock_1101.assert_not_awaited()
        mock_1102.assert_not_awaited()


class TestHandle1101(unittest.IsolatedAsyncioTestCase):
    """Tests for _handle_1101_data_lost (F23-2 revised design)."""

    @patch.object(telegram_bot, '_alert_telegram', new_callable=AsyncMock)
    async def test_handle_1101_disconnected_defers_to_reconnect(self, mock_alert):
        """When ib is None (disconnect path active), orphan scan is NOT called,
        alert IS sent, handler defers to _auto_reconnect."""
        original_ib = telegram_bot.ib
        try:
            telegram_bot.ib = None

            with patch.object(
                telegram_bot, '_scan_orphaned_transmitting_rows',
                new_callable=AsyncMock,
            ) as mock_scan:
                await telegram_bot._handle_1101_data_lost()

                mock_scan.assert_not_awaited()
            # Initial alert should have fired
            mock_alert.assert_awaited_once()
            self.assertIn("1101", mock_alert.call_args[0][0])
        finally:
            telegram_bot.ib = original_ib

    @patch.object(telegram_bot, '_alert_telegram', new_callable=AsyncMock)
    @patch.object(telegram_bot, '_scan_orphaned_transmitting_rows', new_callable=AsyncMock)
    async def test_handle_1101_still_connected_runs_reconciliation(
        self, mock_scan, mock_alert,
    ):
        """When ib is still connected (edge case: 1101 without disconnect),
        reqOpenOrdersAsync, reqExecutionsAsync, and orphan scan all called."""
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True
        mock_ib.reqAllOpenOrdersAsync = AsyncMock()
        mock_ib.reqExecutionsAsync = AsyncMock()

        original_ib = telegram_bot.ib
        try:
            telegram_bot.ib = mock_ib

            await telegram_bot._handle_1101_data_lost()

            mock_ib.reqAllOpenOrdersAsync.assert_awaited_once()
            mock_ib.reqExecutionsAsync.assert_awaited_once()
            mock_scan.assert_awaited_once()
            # Should have: initial alert + success alert
            self.assertGreaterEqual(mock_alert.await_count, 2)
        finally:
            telegram_bot.ib = original_ib

    @patch.object(telegram_bot, '_alert_telegram', new_callable=AsyncMock)
    @patch.object(telegram_bot, '_scan_orphaned_transmitting_rows', new_callable=AsyncMock)
    async def test_handle_1101_open_orders_refetch_failure_aborts(
        self, mock_scan, mock_alert,
    ):
        """If reqAllOpenOrdersAsync raises, orphan scan is NOT called and
        operator is alerted with MANUAL REVIEW REQUIRED. Critical fail-closed test."""
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True
        mock_ib.reqAllOpenOrdersAsync = AsyncMock(
            side_effect=ConnectionError("Gateway down")
        )

        original_ib = telegram_bot.ib
        try:
            telegram_bot.ib = mock_ib

            # Must not raise
            await telegram_bot._handle_1101_data_lost()

            mock_scan.assert_not_awaited()
            # Check MANUAL REVIEW REQUIRED alert was sent
            alert_texts = [call[0][0] for call in mock_alert.call_args_list]
            manual_review = [t for t in alert_texts if "MANUAL REVIEW REQUIRED" in t]
            self.assertTrue(
                len(manual_review) > 0,
                f"Expected MANUAL REVIEW REQUIRED alert, got: {alert_texts}",
            )
        finally:
            telegram_bot.ib = original_ib


if __name__ == "__main__":
    unittest.main()
