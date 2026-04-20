"""Smoke test for scripts/dump_rules.py — validates plumbing, not rule math."""
import os
import sqlite3
import sys
import unittest
from io import StringIO
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestDumpRulesSmoke(unittest.TestCase):
    """dump_rules.main() runs without raising on a minimal fixture DB."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self._tmp.name
        self._tmp.close()

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # Minimal tables for dump_rules
        conn.execute("CREATE TABLE master_log_nav (account_id TEXT, report_date TEXT, total TEXT)")
        conn.execute("INSERT INTO master_log_nav VALUES ('U21971297', '20260409', '200000')")
        conn.execute("INSERT INTO master_log_nav VALUES ('U22388499', '20260409', '80000')")

        # Minimal master_log_trades so Walker can run (empty is fine — 0 cycles)
        conn.execute("""CREATE TABLE master_log_trades (
            transaction_id TEXT PRIMARY KEY, account_id TEXT, acct_alias TEXT,
            model TEXT, currency TEXT, asset_category TEXT, symbol TEXT,
            description TEXT, conid INTEGER, underlying_conid INTEGER,
            underlying_symbol TEXT, multiplier REAL, strike REAL, expiry TEXT,
            put_call TEXT, trade_id TEXT, ib_order_id INTEGER, ib_exec_id TEXT,
            related_transaction_id TEXT, orig_trade_id TEXT, date_time TEXT,
            trade_date TEXT, report_date TEXT, order_time TEXT,
            open_date_time TEXT, transaction_type TEXT, exchange TEXT,
            buy_sell TEXT, open_close TEXT, order_type TEXT, notes TEXT,
            quantity REAL, trade_price REAL, proceeds REAL, ib_commission REAL,
            net_cash REAL, cost REAL, fifo_pnl_realized REAL, mtm_pnl REAL,
            last_synced_at TEXT
        )""")

        conn.execute("""CREATE TABLE inception_carryin (
            household_id TEXT, account_id TEXT, asset_class TEXT, symbol TEXT,
            conid INTEGER, right TEXT, strike REAL, expiry TEXT, quantity REAL,
            basis_price REAL, as_of_date TEXT, source_broker TEXT, reason TEXT,
            notes TEXT, PRIMARY KEY (account_id, asset_class, conid)
        )""")

        conn.execute("""CREATE TABLE master_log_transfers (
            transaction_id TEXT PRIMARY KEY, account_id TEXT, currency TEXT,
            asset_category TEXT, symbol TEXT, conid INTEGER, direction TEXT,
            quantity REAL, transfer_price REAL, date_time TEXT, report_date TEXT,
            description TEXT, last_synced_at TEXT
        )""")

        conn.execute("CREATE TABLE beta_cache (ticker TEXT PRIMARY KEY, beta REAL DEFAULT 1.0, fetched_ts TEXT DEFAULT (datetime('now')))")
        conn.execute("INSERT INTO beta_cache VALUES ('SPY', 1.0, datetime('now'))")

        conn.execute("CREATE TABLE glide_paths (id INTEGER PRIMARY KEY, household_id TEXT, rule_id TEXT, ticker TEXT, baseline_value REAL, target_value REAL, start_date TEXT, target_date TEXT, pause_conditions TEXT, notes TEXT, accelerator_clause TEXT)")

        conn.execute("""CREATE TABLE el_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household TEXT, timestamp TEXT, excess_liquidity REAL,
            nlv REAL, buying_power REAL, source TEXT DEFAULT 'test',
            account_id TEXT, client_id TEXT
        )""")

        conn.execute("""CREATE TABLE bucket3_dynamic_exit_log (
            audit_id TEXT PRIMARY KEY, trade_date TEXT, ticker TEXT,
            household TEXT,
            action_type TEXT DEFAULT 'CC', household_nlv REAL DEFAULT 0,
            underlying_spot_at_render REAL DEFAULT 0,
            final_status TEXT DEFAULT 'STAGED',
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        # Tables queried by rule_engine via conn (graceful fallback on missing)
        conn.execute("""CREATE TABLE red_alert_state (
            household TEXT PRIMARY KEY, active INTEGER DEFAULT 0,
            activated_at TEXT, deactivated_at TEXT
        )""")

        conn.commit()
        conn.close()

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_main_runs_without_raising(self):
        import scripts.dump_rules as dump_rules
        from agt_equities import trade_repo
        from pathlib import Path

        # Monkeypatch DB path + trade_repo
        orig_db = dump_rules.DB_PATH
        dump_rules.DB_PATH = Path(self.db_path)
        dump_rules._gaps.clear()

        captured = StringIO()
        try:
            with patch("sys.stdout", captured), \
                 patch("scripts.dump_rules._fetch_spots_yfinance", return_value={"SPY": 520.0}), \
                 patch("scripts.dump_rules._fetch_vix", return_value=18.5):
                dump_rules.main()
        finally:
            dump_rules.DB_PATH = orig_db

        output = captured.getvalue()
        # Validate key sections present
        self.assertIn("HOUSEHOLD:", output)
        # R1/R3 may be absent with 0 cycles (returns empty list). Check always-present rules.
        self.assertIn("[R2]", output)
        self.assertIn("[R11]", output)
        self.assertIn("GLIDE PATHS", output)
        self.assertIn("DATA GAPS", output)


if __name__ == "__main__":
    unittest.main()
