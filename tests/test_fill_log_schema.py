"""Sprint-1.1: fill_log.inception_delta schema migration test."""
import os
import sqlite3
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agt_equities.schema import register_operational_tables


class TestFillLogInceptionDelta(unittest.TestCase):
    """Verify inception_delta column on fill_log after schema init."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        register_operational_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_inception_delta_column_exists(self):
        """fill_log must have inception_delta after schema init."""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(fill_log)").fetchall()]
        self.assertIn("inception_delta", cols)

    def test_inception_delta_accepts_null(self):
        """inception_delta must accept NULL (legacy rows without Greeks)."""
        self.conn.execute(
            "INSERT INTO fill_log (exec_id, ticker, action) VALUES (?, ?, ?)",
            ("test-exec-1", "AAPL", "SELL_CALL"),
        )
        row = self.conn.execute(
            "SELECT inception_delta FROM fill_log WHERE exec_id = ?",
            ("test-exec-1",),
        ).fetchone()
        self.assertIsNone(row["inception_delta"])

    def test_inception_delta_accepts_float(self):
        """inception_delta must accept a float value (e.g., 0.25)."""
        self.conn.execute(
            "INSERT INTO fill_log (exec_id, ticker, action, inception_delta) "
            "VALUES (?, ?, ?, ?)",
            ("test-exec-2", "MSFT", "SELL_CALL", 0.25),
        )
        row = self.conn.execute(
            "SELECT inception_delta FROM fill_log WHERE exec_id = ?",
            ("test-exec-2",),
        ).fetchone()
        self.assertAlmostEqual(row["inception_delta"], 0.25)

    def test_migration_idempotent(self):
        """Running register_operational_tables twice must not fail."""
        register_operational_tables(self.conn)
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(fill_log)").fetchall()]
        self.assertIn("inception_delta", cols)

    def test_alter_on_existing_table_without_column(self):
        """Simulate existing DB without inception_delta — ALTER must add it."""
        conn2 = sqlite3.connect(":memory:")
        conn2.row_factory = sqlite3.Row
        # Create fill_log WITHOUT inception_delta (pre-migration schema)
        conn2.execute("""
            CREATE TABLE fill_log (
                exec_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, action TEXT NOT NULL,
                quantity REAL, price REAL, premium_delta REAL, account_id TEXT,
                household_id TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Insert a pre-existing row
        conn2.execute(
            "INSERT INTO fill_log (exec_id, ticker, action) VALUES (?, ?, ?)",
            ("old-exec-1", "ADBE", "SELL_CALL"),
        )
        # Run migration — should ALTER TABLE
        register_operational_tables(conn2)
        cols = [r["name"] for r in conn2.execute("PRAGMA table_info(fill_log)").fetchall()]
        self.assertIn("inception_delta", cols)
        # Pre-existing row should have NULL inception_delta
        row = conn2.execute(
            "SELECT inception_delta FROM fill_log WHERE exec_id = ?",
            ("old-exec-1",),
        ).fetchone()
        self.assertIsNone(row["inception_delta"])
        conn2.close()


if __name__ == "__main__":
    unittest.main()
