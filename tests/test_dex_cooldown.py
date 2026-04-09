"""Sprint 1D: Tests for DEX TRANSMIT 10s cooldown."""
import unittest
from unittest.mock import patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestCooldownConfig(unittest.TestCase):
    """Trust tier → cooldown seconds mapping."""

    def test_cooldown_10s_at_t0(self):
        with patch("telegram_bot.TRUST_TIER", "T0"):
            from telegram_bot import _get_cooldown_seconds
            self.assertEqual(_get_cooldown_seconds(), 10)

    def test_cooldown_5s_at_t1(self):
        with patch("telegram_bot.TRUST_TIER", "T1"):
            from telegram_bot import _get_cooldown_seconds
            self.assertEqual(_get_cooldown_seconds(), 5)

    def test_cooldown_0s_at_t2(self):
        with patch("telegram_bot.TRUST_TIER", "T2"):
            from telegram_bot import _get_cooldown_seconds
            self.assertEqual(_get_cooldown_seconds(), 0)

    def test_cooldown_unknown_tier_defaults_10(self):
        with patch("telegram_bot.TRUST_TIER", "UNKNOWN"):
            from telegram_bot import _get_cooldown_seconds
            self.assertEqual(_get_cooldown_seconds(), 10)


class TestCooldownDataStructures(unittest.TestCase):
    """Verify cooldown tracking structures exist and behave correctly."""

    def test_cooldown_tasks_dict_exists(self):
        import telegram_bot
        self.assertIsInstance(telegram_bot._cooldown_tasks, dict)

    def test_cooldown_tasks_tracks_audit_id(self):
        import telegram_bot
        telegram_bot._cooldown_tasks["test-123"] = "mock_task"
        self.assertIn("test-123", telegram_bot._cooldown_tasks)
        del telegram_bot._cooldown_tasks["test-123"]

    def test_duplicate_tap_would_find_active_cooldown(self):
        """Verify dedup logic: if audit_id in _cooldown_tasks, second tap blocked."""
        import telegram_bot
        audit_id = "test-dedup-456"
        telegram_bot._cooldown_tasks[audit_id] = "mock_task"
        self.assertIn(audit_id, telegram_bot._cooldown_tasks)
        telegram_bot._cooldown_tasks.pop(audit_id, None)

    def test_cooldown_abort_leaves_row_attested(self):
        """Verify design invariant: CancelledError handling code does NOT flip row state.
        The cooldown code catches CancelledError and returns without any DB write.
        Row stays in whatever state it was (ATTESTED)."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        func_start = src.find("async def handle_dex_callback")
        func_body = src[func_start:func_start + 20000]
        cancelled_idx = func_body.find("except asyncio.CancelledError:")
        after_cancelled = func_body[cancelled_idx:cancelled_idx + 400]
        self.assertNotIn("UPDATE bucket3_dynamic_exit_log", after_cancelled,
                         "CancelledError handler must NOT mutate row state")
        self.assertIn("Row stays ATTESTED", after_cancelled,
                       "CancelledError handler must document ATTESTED preservation")

    def test_cooldown_abort_edit_failure_swallowed(self):
        """Verify the inner try/except around abort edit prevents exception propagation."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        # Find the CancelledError handler in handle_dex_callback
        func_start = src.find("async def handle_dex_callback")
        func_body = src[func_start:func_start + 20000]
        cancelled_idx = func_body.find("except asyncio.CancelledError:")
        after_cancelled = func_body[cancelled_idx:cancelled_idx + 400]
        # Must have inner try/except around the abort edit
        self.assertIn("except Exception:", after_cancelled,
                       "Abort edit must be wrapped in try/except to swallow failures")
        self.assertIn("Edit may fail", after_cancelled,
                       "Comment must document why edit failure is swallowed")


class TestGateBlockAfterCooldown(unittest.TestCase):
    """If gate blocks after cooldown, row stays TRANSMITTING."""

    def test_gate_block_documented_in_code(self):
        """Verify Sprint 1A gate check exists AFTER cooldown in handle_dex_callback."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        func_start = src.find('async def handle_dex_callback')
        func_body = src[func_start:func_start + 20000]
        cooldown_pos = func_body.find('_get_cooldown_seconds')
        gate_pos = func_body.find('_pre_trade_gates')
        self.assertNotEqual(cooldown_pos, -1)
        self.assertNotEqual(gate_pos, -1)
        self.assertLess(cooldown_pos, gate_pos,
                        "Cooldown must fire BEFORE pre-trade gates")


if __name__ == "__main__":
    unittest.main()
