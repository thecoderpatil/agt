"""Sprint 1C: Tests for _paper_prefix helper."""
import unittest
from unittest.mock import patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestPaperPrefix(unittest.TestCase):

    @patch("telegram_bot.PAPER_MODE", True)
    def test_adds_prefix_when_active(self):
        from telegram_bot import _paper_prefix
        self.assertEqual(_paper_prefix("Hello"), "[PAPER] Hello")

    @patch("telegram_bot.PAPER_MODE", False)
    def test_noop_when_inactive(self):
        from telegram_bot import _paper_prefix
        self.assertEqual(_paper_prefix("Hello"), "Hello")

    @patch("telegram_bot.PAPER_MODE", True)
    def test_idempotent_no_double_prefix(self):
        from telegram_bot import _paper_prefix
        self.assertEqual(_paper_prefix("[PAPER] Already prefixed"), "[PAPER] Already prefixed")

    @patch("telegram_bot.PAPER_MODE", True)
    def test_preserves_content(self):
        from telegram_bot import _paper_prefix
        msg = "🚨 TRANSMIT BLOCKED\naudit_id: abc123\nReason: test"
        result = _paper_prefix(msg)
        self.assertTrue(result.startswith("[PAPER] "))
        self.assertIn("TRANSMIT BLOCKED", result)
        self.assertIn("abc123", result)


if __name__ == "__main__":
    unittest.main()
