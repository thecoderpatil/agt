"""Sprint 1C: Tests for _round_to_nickel helper (nickel ≤$3, dime >$3)."""
import unittest
from unittest.mock import patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestRoundToNickelLiveMode(unittest.TestCase):
    """In live mode, _round_to_nickel is a no-op."""

    @patch("telegram_bot.PAPER_MODE", False)
    def test_live_mode_noop(self):
        from telegram_bot import _round_to_nickel
        self.assertEqual(_round_to_nickel(5.42), 5.42)
        self.assertEqual(_round_to_nickel(2.53), 2.53)
        self.assertEqual(_round_to_nickel(0.01), 0.01)


class TestRoundToNickelPaperMode(unittest.TestCase):
    """In paper mode: nickel for ≤$3.00, dime for >$3.00."""

    @patch("telegram_bot.PAPER_MODE", True)
    def test_nickel_zone_exact(self):
        from telegram_bot import _round_to_nickel
        self.assertEqual(_round_to_nickel(2.50), 2.50)
        self.assertEqual(_round_to_nickel(2.55), 2.55)
        self.assertEqual(_round_to_nickel(3.00), 3.00)

    @patch("telegram_bot.PAPER_MODE", True)
    def test_nickel_zone_rounds(self):
        from telegram_bot import _round_to_nickel
        self.assertEqual(_round_to_nickel(2.53), 2.55)
        self.assertEqual(_round_to_nickel(2.51), 2.50)
        self.assertEqual(_round_to_nickel(2.48), 2.50)

    @patch("telegram_bot.PAPER_MODE", True)
    def test_dime_zone_rounds(self):
        from telegram_bot import _round_to_nickel
        self.assertEqual(_round_to_nickel(3.01), 3.00)
        self.assertEqual(_round_to_nickel(3.06), 3.10)
        self.assertEqual(_round_to_nickel(4.27), 4.30)
        self.assertEqual(_round_to_nickel(5.42), 5.40)
        self.assertEqual(_round_to_nickel(5.48), 5.50)

    @patch("telegram_bot.PAPER_MODE", True)
    def test_zero_returns_zero(self):
        from telegram_bot import _round_to_nickel
        self.assertEqual(_round_to_nickel(0), 0)

    @patch("telegram_bot.PAPER_MODE", True)
    def test_none_returns_none(self):
        from telegram_bot import _round_to_nickel
        self.assertIsNone(_round_to_nickel(None))

    @patch("telegram_bot.PAPER_MODE", True)
    def test_boundary_at_three(self):
        """$3.00 uses nickel, $3.01 uses dime."""
        from telegram_bot import _round_to_nickel
        self.assertEqual(_round_to_nickel(3.00), 3.00)  # nickel (≤3)
        self.assertEqual(_round_to_nickel(3.01), 3.00)  # dime (>3, rounds down)
        self.assertEqual(_round_to_nickel(3.06), 3.10)  # dime (>3, rounds up)


if __name__ == "__main__":
    unittest.main()
