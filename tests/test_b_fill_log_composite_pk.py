"""Sprint B: fill_log composite PK (exec_id, account_id) migration test.

DT Shot 1 §7: FA Block fills emit N execDetails per child account.
Composite PK ensures per-account fill rows survive INSERT OR IGNORE dedup.
"""
import os
import sqlite3
import sys
import unittest

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agt_equities.schema import register_operational_tables

pytestmark = pytest.mark.sprint_a


class TestFillLogCompositePK(unittest.TestCase):
    """fill_log must have composite PK (exec_id, account_id) after init."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        register_operational_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_pk_is_composite(self):
        """fill_log PK must span (exec_id, account_id)."""
        pk_cols = [
            r["name"]
            for r in self.conn.execute("PRAGMA table_info(fill_log)").fetchall()
            if r["pk"] > 0
        ]
        self.assertEqual(sorted(pk_cols), ["account_id", "exec_id"])

    def test_account_id_not_null_default_empty(self):
        """account_id must be NOT NULL with DEFAULT ''."""
        col = next(
            r for r in self.conn.execute("PRAGMA table_info(fill_log)").fetchall()
            if r["name"] == "account_id"
        )
        self.assertTrue(col["notnull"])
        self.assertEqual(col["dflt_value"], "''")

    def test_insert_without_account_id_uses_default(self):
        """INSERT omitting account_id must use empty-string default."""
        self.conn.execute(
            "INSERT INTO fill_log (exec_id, ticker, action) VALUES (?, ?, ?)",
            ("e1", "AAPL", "SELL_CALL"),
        )
        row = self.conn.execute(
            "SELECT account_id FROM fill_log WHERE exec_id = ?", ("e1",)
        ).fetchone()
        self.assertEqual(row["account_id"], "")

    def test_same_exec_id_different_accounts_both_survive(self):
        """FA Block: two children with same exec_id but different accounts."""
        self.conn.execute(
            "INSERT INTO fill_log (exec_id, ticker, action, account_id) "
            "VALUES (?, ?, ?, ?)",
            ("e-shared", "AAPL", "SELL_CALL", "U111"),
        )
        self.conn.execute(
            "INSERT INTO fill_log (exec_id, ticker, action, account_id) "
            "VALUES (?, ?, ?, ?)",
            ("e-shared", "AAPL", "SELL_CALL", "U222"),
        )
        rows = self.conn.execute(
            "SELECT account_id FROM fill_log WHERE exec_id = ?",
            ("e-shared",),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r["account_id"] for r in rows), ["U111", "U222"])

    def test_insert_or_ignore_dedup_on_composite(self):
        """INSERT OR IGNORE deduplicates on (exec_id, account_id)."""
        self.conn.execute(
            "INSERT INTO fill_log (exec_id, ticker, action, account_id) "
            "VALUES (?, ?, ?, ?)",
            ("e1", "AAPL", "SELL_CALL", "U111"),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO fill_log "
            "(exec_id, ticker, action, account_id) "
            "VALUES (?, ?, ?, ?)",
            ("e1", "AAPL", "SELL_CALL", "U111"),
        )
        count = self.conn.execute(
            "SELECT COUNT(*) FROM fill_log WHERE exec_id = ? AND account_id = ?",
            ("e1", "U111"),
        ).fetchone()[0]
        self.assertEqual(count, 1)


class TestFillLogPKMigration(unittest.TestCase):
    """Migrate existing fill_log from single exec_id PK to composite."""

    def test_migration_from_single_pk(self):
        """Pre-Sprint-B fill_log (exec_id TEXT PRIMARY KEY) must migrate."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE fill_log (
                exec_id TEXT PRIMARY KEY, ticker TEXT NOT NULL,
                action TEXT NOT NULL, quantity REAL, price REAL,
                premium_delta REAL, account_id TEXT,
                household_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                inception_delta REAL
            )
        """)
        conn.execute(
            "INSERT INTO fill_log (exec_id, ticker, action, account_id) "
            "VALUES (?, ?, ?, ?)",
            ("e1", "AAPL", "SELL_CALL", "U111"),
        )
        conn.execute(
            "INSERT INTO fill_log (exec_id, ticker, action, account_id) "
            "VALUES (?, ?, ?, ?)",
            ("e2", "MSFT", "SELL_PUT", None),
        )
        register_operational_tables(conn)
        pk_cols = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(fill_log)").fetchall()
            if r["pk"] > 0
        ]
        self.assertEqual(sorted(pk_cols), ["account_id", "exec_id"])
        rows = conn.execute(
            "SELECT * FROM fill_log ORDER BY exec_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["account_id"], "U111")
        self.assertEqual(rows[1]["account_id"], "")  # NULL → ''
        conn.close()

    def test_migration_idempotent(self):
        """Running migration twice must not fail or duplicate data."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE fill_log (
                exec_id TEXT PRIMARY KEY, ticker TEXT NOT NULL,
                action TEXT NOT NULL, quantity REAL, price REAL,
                premium_delta REAL, account_id TEXT,
                household_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                inception_delta REAL
            )
        """)
        conn.execute(
            "INSERT INTO fill_log (exec_id, ticker, action, account_id) "
            "VALUES (?, ?, ?, ?)",
            ("e1", "AAPL", "SELL_CALL", "U111"),
        )
        register_operational_tables(conn)
        register_operational_tables(conn)
        count = conn.execute("SELECT COUNT(*) FROM fill_log").fetchone()[0]
        self.assertEqual(count, 1)
        conn.close()

    def test_migration_preserves_inception_delta(self):
        """inception_delta values must survive PK migration."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE fill_log (
                exec_id TEXT PRIMARY KEY, ticker TEXT NOT NULL,
                action TEXT NOT NULL, quantity REAL, price REAL,
                premium_delta REAL, account_id TEXT,
                household_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                inception_delta REAL
            )
        """)
        conn.execute(
            "INSERT INTO fill_log "
            "(exec_id, ticker, action, account_id, inception_delta) "
            "VALUES (?, ?, ?, ?, ?)",
            ("e1", "AAPL", "SELL_CALL", "U111", 0.18),
        )
        register_operational_tables(conn)
        row = conn.execute(
            "SELECT inception_delta FROM fill_log WHERE exec_id = ?",
            ("e1",),
        ).fetchone()
        self.assertAlmostEqual(row["inception_delta"], 0.18)
        conn.close()


if __name__ == "__main__":
    unittest.main()
