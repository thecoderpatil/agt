"""Sprint 1A: Tests for cold-start mode bootstrap pin."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _create_mode_history(conn):
    """Create mode_history table matching agt_equities/schema.py:886-895."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mode_history (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL DEFAULT (datetime('now')),
            old_mode            TEXT NOT NULL,
            new_mode            TEXT NOT NULL,
            trigger_rule        TEXT,
            trigger_household   TEXT,
            trigger_value       REAL,
            notes               TEXT
        )
    """)
    conn.commit()


class TestColdStartPin(unittest.TestCase):
    """_pin_mode_on_startup() should reset WARTIME → PEACETIME on boot."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        _create_mode_history(conn)
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _seed(self, old_mode, new_mode, trigger="manual"):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO mode_history (old_mode, new_mode, trigger_rule) VALUES (?, ?, ?)",
            (old_mode, new_mode, trigger),
        )
        conn.commit()
        conn.close()

    def _count_rows(self):
        conn = self._get_conn()
        count = conn.execute("SELECT COUNT(*) FROM mode_history").fetchone()[0]
        conn.close()
        return count

    def _last_row(self):
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM mode_history ORDER BY id DESC LIMIT 1").fetchone()
        result = dict(row) if row else None
        conn.close()
        return result

    @patch("telegram_bot._get_db_connection")
    def test_wartime_pinned_to_peacetime(self, mock_db):
        mock_db.side_effect = lambda: self._get_conn()
        self._seed("PEACETIME", "WARTIME")

        from telegram_bot import _pin_mode_on_startup
        alert = _pin_mode_on_startup()

        self.assertIsNotNone(alert)
        self.assertIn("PEACETIME", alert)
        self.assertIn("COLD-START", alert)

        row = self._last_row()
        self.assertEqual(row["new_mode"], "PEACETIME")
        self.assertEqual(row["trigger_rule"], "cold_start_pin")

    @patch("telegram_bot._get_db_connection")
    def test_peacetime_noop(self, mock_db):
        mock_db.side_effect = lambda: self._get_conn()
        self._seed("WARTIME", "PEACETIME")

        from telegram_bot import _pin_mode_on_startup
        alert = _pin_mode_on_startup()

        self.assertIsNone(alert)
        self.assertEqual(self._count_rows(), 1)

    @patch("telegram_bot._get_db_connection")
    def test_amber_noop(self, mock_db):
        mock_db.side_effect = lambda: self._get_conn()
        self._seed("PEACETIME", "AMBER", trigger="rule_11")

        from telegram_bot import _pin_mode_on_startup
        alert = _pin_mode_on_startup()

        self.assertIsNone(alert)
        self.assertEqual(self._count_rows(), 1)

    @patch("telegram_bot._get_db_connection")
    def test_empty_history_noop(self, mock_db):
        mock_db.side_effect = lambda: self._get_conn()

        from telegram_bot import _pin_mode_on_startup
        alert = _pin_mode_on_startup()

        self.assertIsNone(alert)
        self.assertEqual(self._count_rows(), 0)

    @patch("telegram_bot._get_db_connection", side_effect=RuntimeError("DB locked"))
    def test_db_error_does_not_raise(self, mock_db):
        from telegram_bot import _pin_mode_on_startup
        alert = _pin_mode_on_startup()
        self.assertIsNone(alert)


if __name__ == "__main__":
    unittest.main()
