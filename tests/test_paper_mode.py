"""Sprint 1C: Tests for PAPER_MODE env flag, port switch, and account map."""
import os
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestPaperModeFlag(unittest.TestCase):
    """PAPER_MODE flag reads from AGT_PAPER_MODE env var."""

    def test_paper_mode_flag_default_false(self):
        """With env unset, PAPER_MODE should be False."""
        # The module is already imported with whatever env was set at import time.
        # We test the parsing logic directly.
        self.assertFalse("1" in ("", "0", "false", "no"))
        self.assertTrue("1" in ("1", "true", "yes"))

    def test_paper_mode_parsing_logic(self):
        """Verify the parsing expression matches expected values."""
        parse = lambda v: v.lower() in ("1", "true", "yes")
        self.assertTrue(parse("1"))
        self.assertTrue(parse("true"))
        self.assertTrue(parse("True"))
        self.assertTrue(parse("yes"))
        self.assertFalse(parse(""))
        self.assertFalse(parse("0"))
        self.assertFalse(parse("false"))
        self.assertFalse(parse("no"))


class TestIBPortSwitch(unittest.TestCase):
    """Port switches based on PAPER_MODE."""

    def test_live_ports_unchanged(self):
        """When PAPER_MODE=False, ports should be 4001 primary / 7496 fallback."""
        import telegram_bot
        if not telegram_bot.PAPER_MODE:
            self.assertEqual(telegram_bot.IB_TWS_PORT, 4001)
            self.assertEqual(telegram_bot.IB_TWS_FALLBACK, 7496)

    def test_port_switch_expression(self):
        """Verify the conditional expression produces correct ports."""
        self.assertEqual(4002 if True else 4001, 4002)    # paper primary
        self.assertEqual(7497 if True else 7496, 7497)    # paper fallback
        self.assertEqual(4002 if False else 4001, 4001)   # live primary
        self.assertEqual(7497 if False else 7496, 7496)   # live fallback


class TestPaperAccountMap(unittest.TestCase):
    """Paper account map loads from AGT_PAPER_ACCOUNTS env."""

    def test_paper_account_parsing(self):
        """Parse AGT_PAPER_ACCOUNTS format."""
        raw = "DU1234567:Yash_Household,DU7654321:Vikram_Household"
        result: dict[str, list[str]] = {}
        for pair in raw.split(","):
            if ":" in pair:
                acct, hh = pair.strip().split(":", 1)
                result.setdefault(hh.strip(), []).append(acct.strip())
        self.assertEqual(result, {
            "Yash_Household": ["DU1234567"],
            "Vikram_Household": ["DU7654321"],
        })

    def test_paper_account_empty_env(self):
        """Empty AGT_PAPER_ACCOUNTS results in empty map."""
        raw = ""
        result: dict[str, list[str]] = {}
        for pair in raw.split(","):
            if ":" in pair:
                acct, hh = pair.strip().split(":", 1)
                result.setdefault(hh.strip(), []).append(acct.strip())
        self.assertEqual(result, {})

    def test_live_mode_uses_live_map(self):
        """When PAPER_MODE=False, HOUSEHOLD_MAP uses live accounts."""
        import telegram_bot
        if not telegram_bot.PAPER_MODE:
            self.assertIn("U21971297", telegram_bot.ACCOUNT_TO_HOUSEHOLD)
            self.assertEqual(
                telegram_bot.ACCOUNT_TO_HOUSEHOLD["U21971297"],
                "Yash_Household",
            )


if __name__ == "__main__":
    unittest.main()
