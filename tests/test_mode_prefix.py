"""Sprint 1D: Tests for mode prefix on outbound Telegram messages."""
import sqlite3
import os
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestModePrefix(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mode_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                old_mode TEXT NOT NULL, new_mode TEXT NOT NULL,
                trigger_rule TEXT, trigger_household TEXT, trigger_value REAL, notes TEXT
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _seed_mode(self, mode):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO mode_history (old_mode, new_mode) VALUES ('PEACETIME', ?)",
            (mode,),
        )
        conn.commit()
        conn.close()

    @patch("telegram_bot._get_db_connection")
    def test_peacetime_no_prefix(self, mock_db):
        from telegram_bot import _mode_prefix
        mock_db.side_effect = lambda: self._get_conn()
        self._seed_mode("PEACETIME")
        result = _mode_prefix("Hello")
        self.assertEqual(result, "Hello")

    @patch("telegram_bot._get_db_connection")
    def test_amber_prefix(self, mock_db):
        from telegram_bot import _mode_prefix
        mock_db.side_effect = lambda: self._get_conn()
        self._seed_mode("AMBER")
        result = _mode_prefix("Alert text")
        self.assertIn("AMBER", result)
        self.assertIn("Alert text", result)

    @patch("telegram_bot._get_db_connection")
    def test_wartime_prefix(self, mock_db):
        from telegram_bot import _mode_prefix
        mock_db.side_effect = lambda: self._get_conn()
        self._seed_mode("WARTIME")
        result = _mode_prefix("Alert text")
        self.assertIn("WARTIME", result)
        self.assertIn("Alert text", result)

    @patch("telegram_bot.PAPER_MODE", True)
    @patch("telegram_bot._get_db_connection")
    def test_prefix_order_paper_then_mode(self, mock_db):
        from telegram_bot import _format_outbound
        mock_db.side_effect = lambda: self._get_conn()
        self._seed_mode("WARTIME")
        result = _format_outbound("Order placed")
        # Paper prefix comes first, then mode prefix
        self.assertTrue(result.startswith("[PAPER]"))
        self.assertIn("WARTIME", result)
        self.assertIn("Order placed", result)

    @patch("telegram_bot._get_db_connection", side_effect=RuntimeError("DB error"))
    def test_prefix_db_error_noop(self, mock_db):
        from telegram_bot import _mode_prefix
        result = _mode_prefix("Hello")
        self.assertEqual(result, "Hello")


if __name__ == "__main__":
    unittest.main()
