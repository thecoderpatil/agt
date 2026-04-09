"""Sprint 1E: Tests for multi-tenant client_id column migration."""
import sqlite3
import unittest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# Minimal table DDLs matching production schema (subset of columns needed for test)
_TABLE_DDLS = {
    "bucket3_dynamic_exit_log": """
        CREATE TABLE IF NOT EXISTS bucket3_dynamic_exit_log (
            audit_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            household TEXT NOT NULL,
            final_status TEXT NOT NULL DEFAULT 'STAGED',
            desk_mode TEXT NOT NULL DEFAULT 'PEACETIME',
            action_type TEXT NOT NULL DEFAULT 'CC',
            household_nlv REAL NOT NULL DEFAULT 0,
            underlying_spot_at_render REAL NOT NULL DEFAULT 0,
            staged_ts REAL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "pending_orders": """
        CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT DEFAULT 'staged',
            created_at TEXT DEFAULT (datetime('now')),
            payload TEXT
        )
    """,
    "el_snapshots": """
        CREATE TABLE IF NOT EXISTS el_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            excess_liquidity REAL,
            nlv REAL,
            source TEXT NOT NULL DEFAULT 'ibkr_live'
        )
    """,
    "mode_history": """
        CREATE TABLE IF NOT EXISTS mode_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            old_mode TEXT NOT NULL,
            new_mode TEXT NOT NULL,
            trigger_rule TEXT
        )
    """,
    "premium_ledger": """
        CREATE TABLE IF NOT EXISTS premium_ledger (
            ticker TEXT PRIMARY KEY,
            shares_owned INTEGER DEFAULT 0,
            total_premium REAL DEFAULT 0
        )
    """,
    "live_blotter": """
        CREATE TABLE IF NOT EXISTS live_blotter (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            ticker TEXT
        )
    """,
    "executed_orders": """
        CREATE TABLE IF NOT EXISTS executed_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            ticker TEXT,
            fill_price REAL
        )
    """,
}


def _run_migration(conn):
    """Run the Sprint 1E migration logic (same as schema.py)."""
    tables = [
        "bucket3_dynamic_exit_log", "pending_orders", "el_snapshots",
        "mode_history", "premium_ledger", "live_blotter", "executed_orders",
    ]
    existing = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for tbl in tables:
        if tbl not in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN client_id TEXT DEFAULT 'AGT'")
        except Exception:
            pass  # Column already exists


class TestMultitenantSchema(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        for ddl in _TABLE_DDLS.values():
            self.conn.execute(ddl)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_client_id_column_exists_on_all_tables(self):
        """After migration, all 7 tables have client_id column."""
        _run_migration(self.conn)
        for tbl in _TABLE_DDLS:
            cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
            self.assertIn("client_id", cols, f"{tbl} missing client_id column")

    def test_client_id_default_is_agt(self):
        """Inserting a row without client_id gets DEFAULT 'AGT'."""
        _run_migration(self.conn)
        self.conn.execute(
            "INSERT INTO mode_history (old_mode, new_mode) VALUES ('PEACETIME', 'AMBER')"
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT client_id FROM mode_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["client_id"], "AGT")

    def test_migration_idempotent(self):
        """Running migration twice does not error."""
        _run_migration(self.conn)
        _run_migration(self.conn)  # Second run — no error
        for tbl in _TABLE_DDLS:
            cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
            self.assertIn("client_id", cols)

    def test_existing_rows_backfilled(self):
        """Rows inserted before migration get client_id='AGT' via DEFAULT."""
        # Seed rows before migration
        self.conn.execute(
            "INSERT INTO pending_orders (status, payload) VALUES ('staged', '{}')"
        )
        self.conn.execute(
            "INSERT INTO el_snapshots (household, excess_liquidity, nlv) "
            "VALUES ('Yash_Household', 45000, 150000)"
        )
        self.conn.commit()

        # Run migration
        _run_migration(self.conn)

        # Existing rows should have client_id = 'AGT'
        row = self.conn.execute("SELECT client_id FROM pending_orders LIMIT 1").fetchone()
        self.assertEqual(row["client_id"], "AGT")

        row = self.conn.execute("SELECT client_id FROM el_snapshots LIMIT 1").fetchone()
        self.assertEqual(row["client_id"], "AGT")


if __name__ == "__main__":
    unittest.main()
