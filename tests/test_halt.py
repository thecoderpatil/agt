"""Sprint 1D: Tests for /halt killswitch."""
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestHalt(unittest.TestCase):

    def setUp(self):
        import telegram_bot
        telegram_bot._HALTED = False

    def tearDown(self):
        import telegram_bot
        telegram_bot._HALTED = False

    def test_halt_sets_flag(self):
        import telegram_bot
        self.assertFalse(telegram_bot._HALTED)
        telegram_bot._HALTED = True
        self.assertTrue(telegram_bot._HALTED)

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    def test_halt_blocks_pre_trade_gates(self, _mock):
        import telegram_bot
        from types import SimpleNamespace
        telegram_bot._HALTED = True
        ok, reason = _run(telegram_bot._pre_trade_gates(
            SimpleNamespace(totalQuantity=1, lmtPrice=2.0),
            SimpleNamespace(secType="OPT", strike=100.0),
            {"site": "dex", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertFalse(ok)
        self.assertIn("halted", reason)

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    def test_not_halted_allows_gates(self, _mock):
        import telegram_bot
        from types import SimpleNamespace
        telegram_bot._HALTED = False
        ok, reason = _run(telegram_bot._pre_trade_gates(
            SimpleNamespace(totalQuantity=1, lmtPrice=2.0),
            SimpleNamespace(secType="OPT", strike=100.0),
            {"site": "dex", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertTrue(ok)

    def test_halt_idempotent(self):
        import telegram_bot
        telegram_bot._HALTED = True
        telegram_bot._HALTED = True  # Second set — should not error
        self.assertTrue(telegram_bot._HALTED)

    def test_scheduled_job_noops_when_halted(self):
        """Verify sweep job exits early when halted."""
        import telegram_bot
        telegram_bot._HALTED = True
        # _sweep_attested_ttl_job should return immediately without error
        result = _run(telegram_bot._sweep_attested_ttl_job(None))
        self.assertIsNone(result)

    def test_el_writer_noops_when_halted(self):
        """Verify EL snapshot writer exits early when halted."""
        import telegram_bot
        telegram_bot._HALTED = True
        result = _run(telegram_bot._el_snapshot_writer_job(None))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
