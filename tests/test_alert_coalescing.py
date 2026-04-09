"""Sprint 1D: Tests for STAGED alert coalescing."""
import time
import unittest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestStagedCoalescing(unittest.TestCase):

    def setUp(self):
        import telegram_bot
        telegram_bot._staged_alert_buffer.clear()
        telegram_bot._staged_alert_last_flush = 0.0

    def test_staged_buffered_not_sent_immediately(self):
        """Adding to buffer does not trigger immediate send."""
        import telegram_bot
        telegram_bot._staged_alert_buffer.append({
            "ticker": "ADBE", "action_type": "CC", "contracts": 1,
            "strike": 245.0, "limit_price": 3.50, "household": "Yash_Household",
        })
        self.assertEqual(len(telegram_bot._staged_alert_buffer), 1)
        # Buffer exists but no flush has happened
        self.assertEqual(telegram_bot._staged_alert_last_flush, 0.0)

    def test_buffer_accumulates_multiple_rows(self):
        """Multiple STAGED rows accumulate in the buffer."""
        import telegram_bot
        for tk in ["ADBE", "MSFT", "NVDA"]:
            telegram_bot._staged_alert_buffer.append({
                "ticker": tk, "action_type": "CC", "contracts": 1,
                "strike": 100.0, "limit_price": 2.00, "household": "Yash_Household",
            })
        self.assertEqual(len(telegram_bot._staged_alert_buffer), 3)

    def test_coalesce_window_constant(self):
        """Coalesce window is 60 seconds."""
        import telegram_bot
        self.assertEqual(telegram_bot.STAGED_COALESCE_WINDOW, 60)

    def test_critical_alert_bypasses_buffer(self):
        """_alert_telegram is a direct send, not buffered. Critical alerts bypass coalescing."""
        import telegram_bot
        # _alert_telegram is an async function that sends directly via Bot
        # It does NOT touch _staged_alert_buffer
        import inspect
        src = inspect.getsource(telegram_bot._alert_telegram)
        self.assertNotIn("_staged_alert_buffer", src,
                         "_alert_telegram must NOT use the coalescing buffer")


if __name__ == "__main__":
    unittest.main()
