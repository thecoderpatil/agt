"""
Followup #9 PR3 — Non-telegram_bot module fixes verification.

Covers:
  T1: vrp_veto write persistence (closing + with conn)
  T2: ib_chains _log_fetch persistence (closing + with conn)
  T3: pxo_scanner read returns correct data (closing-only)
  T4: telegram_dashboard_integration write persistence (closing + with conn)
  T5: Exception-path test — force exception mid-block, verify conn
      closed and transaction rolled back
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ═══════════════════════════════════════════════════════════════════════════
# T1: vrp_veto write persistence
# ═══════════════════════════════════════════════════════════════════════════

class TestVrpVetoWritePersistence(unittest.TestCase):
    """Verify vrp_veto's closing() + with conn: pattern persists writes."""

    def test_vrp_write_persists(self):
        db = os.path.join(tempfile.gettempdir(), "pr3_vrp.db")
        try:
            # Replicate vrp_veto init_vrp_db pattern
            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS vrp_daily (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            run_date TEXT NOT NULL,
                            ticker TEXT NOT NULL,
                            signal TEXT NOT NULL
                        )
                    """)

            # Replicate write_vrp_results pattern
            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    conn.executemany(
                        "INSERT INTO vrp_daily (run_date, ticker, signal) VALUES (?, ?, ?)",
                        [("2026-04-08", "AAPL", "SELL"), ("2026-04-08", "MSFT", "HOLD")],
                    )

            # Re-open and verify
            check = sqlite3.connect(db)
            rows = check.execute("SELECT * FROM vrp_daily").fetchall()
            check.close()
            self.assertEqual(len(rows), 2, "Both VRP rows must persist")
        finally:
            try: os.unlink(db)
            except: pass


# ═══════════════════════════════════════════════════════════════════════════
# T2: ib_chains _log_fetch persistence
# ═══════════════════════════════════════════════════════════════════════════

class TestIbChainsLogFetchPersistence(unittest.TestCase):
    """Verify ib_chains _log_fetch closing() + with conn: persists."""

    def test_log_fetch_persists(self):
        db = os.path.join(tempfile.gettempdir(), "pr3_chains.db")
        try:
            setup = sqlite3.connect(db)
            setup.execute("""
                CREATE TABLE market_data_log (
                    timestamp TEXT, ticker TEXT, source TEXT,
                    latency_ms REAL, success INTEGER, error_class TEXT
                )
            """)
            setup.commit()
            setup.close()

            # Replicate _log_fetch pattern
            with closing(sqlite3.connect(db, timeout=5.0)) as conn:
                with conn:
                    conn.execute(
                        "INSERT INTO market_data_log "
                        "(timestamp, ticker, source, latency_ms, success, error_class) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        ("2026-04-08T12:00:00", "AAPL", "ibkr", 45.2, 1, ""),
                    )

            check = sqlite3.connect(db)
            rows = check.execute("SELECT * FROM market_data_log").fetchall()
            check.close()
            self.assertEqual(len(rows), 1, "Audit log row must persist")
            self.assertEqual(rows[0][1], "AAPL")
        finally:
            try: os.unlink(db)
            except: pass


# ═══════════════════════════════════════════════════════════════════════════
# T3: pxo_scanner read returns correct data
# ═══════════════════════════════════════════════════════════════════════════

class TestPxoScannerReadRegression(unittest.TestCase):
    """Verify pxo_scanner read with closing() returns correct data."""

    def test_scan_universe_read(self):
        db = os.path.join(tempfile.gettempdir(), "pr3_scanner.db")
        try:
            setup = sqlite3.connect(db)
            setup.execute("""
                CREATE TABLE ticker_universe (
                    ticker TEXT, gics_industry_group TEXT
                )
            """)
            setup.execute(
                "INSERT INTO ticker_universe VALUES ('AAPL', 'Technology Hardware')"
            )
            setup.commit()
            setup.close()

            # Replicate _load_scan_universe pattern
            with closing(sqlite3.connect(db, timeout=10.0)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT ticker, gics_industry_group AS sector "
                    "FROM ticker_universe WHERE gics_industry_group IS NOT NULL"
                ).fetchall()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ticker"], "AAPL")
            self.assertEqual(rows[0]["sector"], "Technology Hardware")
        finally:
            try: os.unlink(db)
            except: pass


# ═══════════════════════════════════════════════════════════════════════════
# T4: telegram_dashboard_integration write persistence
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardIntegrationWritePersistence(unittest.TestCase):
    """Verify _record_fill_to_trade_ledger closing() + with conn: persists."""

    def test_trade_ledger_insert_persists(self):
        db = os.path.join(tempfile.gettempdir(), "pr3_dash.db")
        try:
            setup = sqlite3.connect(db)
            setup.execute("""
                CREATE TABLE trade_ledger (
                    account_id TEXT, household_id TEXT,
                    trade_date TEXT, trade_datetime TEXT,
                    symbol TEXT, underlying TEXT,
                    asset_category TEXT, trade_type TEXT,
                    quantity REAL, price REAL, proceeds REAL,
                    realized_pnl REAL, return_category TEXT,
                    source TEXT
                )
            """)
            setup.commit()
            setup.close()

            # Replicate _record_fill_to_trade_ledger pattern
            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    conn.execute(
                        "INSERT INTO trade_ledger "
                        "(account_id, household_id, trade_date, trade_datetime, "
                        "symbol, underlying, asset_category, trade_type, "
                        "quantity, price, proceeds, realized_pnl, "
                        "return_category, source) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'LIVE')",
                        ("U21971297", "Yash_Household", "2026-04-08",
                         "2026-04-08 12:00:00", "AAPL", "AAPL",
                         "Stock", "BUY", 100, 150.0, 15000.0, 0.0,
                         "premium_harvest"),
                    )

            check = sqlite3.connect(db)
            rows = check.execute("SELECT * FROM trade_ledger").fetchall()
            check.close()
            self.assertEqual(len(rows), 1, "Trade ledger INSERT must persist")
        finally:
            try: os.unlink(db)
            except: pass


# ═══════════════════════════════════════════════════════════════════════════
# T5: Exception-path — conn closed + transaction rolled back
# ═══════════════════════════════════════════════════════════════════════════

class TestExceptionPathCleanup(unittest.TestCase):
    """Force an exception mid-block. Verify conn is closed (no leak)
    and the partial write is rolled back."""

    def test_exception_rolls_back_and_closes(self):
        db = os.path.join(tempfile.gettempdir(), "pr3_exc.db")
        try:
            setup = sqlite3.connect(db)
            setup.execute("CREATE TABLE t (x INTEGER NOT NULL)")
            setup.commit()
            setup.close()

            # Force exception after first write
            try:
                with closing(sqlite3.connect(db)) as conn:
                    with conn:
                        conn.execute("INSERT INTO t VALUES (1)")
                        conn.execute("INSERT INTO t VALUES (NULL)")  # NOT NULL violation
            except sqlite3.IntegrityError:
                pass  # expected

            # Verify: transaction rolled back, row 1 should NOT persist
            check = sqlite3.connect(db)
            count = check.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            check.close()
            self.assertEqual(count, 0,
                             "Exception inside with conn: must rollback the entire transaction")

            # Verify: conn is closed (we can open a new one — no lock)
            verify = sqlite3.connect(db, timeout=1.0)
            verify.execute("INSERT INTO t VALUES (99)")
            verify.commit()
            count2 = verify.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            verify.close()
            self.assertEqual(count2, 1, "DB must be unlocked after exception path")
        finally:
            try: os.unlink(db)
            except: pass


if __name__ == "__main__":
    unittest.main()
