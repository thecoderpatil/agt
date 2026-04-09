"""Sprint 1D: Tests for command prune — killed commands unregistered, kept commands present."""
import unittest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


KILLED_COMMANDS = [
    "health", "cycles", "ledger", "fills", "dashboard", "cc", "mode1",
    "scan", "rollcheck", "declare_wartime", "sync_universe",
    "cleanup_blotter", "status_orders", "stop", "dynamic_exit",
    "override", "override_earnings", "reconcile", "clear_quarantine",
]

KEPT_COMMANDS = [
    "start", "status", "cure", "mode", "approve", "reject", "reconnect",
    "recover_transmitting", "budget", "think", "deep", "clear", "vrp",
    "orders", "declare_peacetime", "halt",
]


class TestCommandPrune(unittest.TestCase):

    def _get_registered_commands(self):
        """Parse handler registration block from telegram_bot.py source."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        import re
        return set(re.findall(r'CommandHandler\("(\w+)"', src))

    def test_killed_commands_not_registered(self):
        registered = self._get_registered_commands()
        for cmd in KILLED_COMMANDS:
            self.assertNotIn(cmd, registered, f"/{cmd} should be killed but is still registered")

    def test_kept_commands_still_registered(self):
        registered = self._get_registered_commands()
        for cmd in KEPT_COMMANDS:
            self.assertIn(cmd, registered, f"/{cmd} should be kept but is not registered")

    def test_halt_command_registered(self):
        registered = self._get_registered_commands()
        self.assertIn("halt", registered)

    def test_start_menu_reflects_new_list(self):
        """Verify /start menu text does not list killed commands."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        # Find menu text in _send_command_menu
        start = src.find('# Sprint 1D: pruned command menu')
        end = src.find('await update.message.reply_text(menu', start)
        menu_block = src[start:end]
        for cmd in ["cc", "scan", "health", "dashboard", "fills", "ledger",
                     "rollcheck", "dynamic_exit", "override_earnings",
                     "sync_universe", "override"]:
            self.assertNotIn(f"/{cmd}", menu_block,
                             f"/{cmd} should not appear in /start menu")

    def test_cc_scheduled_job_preserved(self):
        """Verify _scheduled_cc daily job is still wired (separate from cmd_cc)."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        self.assertIn("_scheduled_cc", src)
        self.assertIn('name="cc_daily"', src)


if __name__ == "__main__":
    unittest.main()
