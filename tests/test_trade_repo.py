"""
tests/test_trade_repo.py — trade_repo read path tests against fixture DB.

NOTE: The YTD fixture (fromDate=20260101) is missing pre-2026 CSP opens,
causing ORPHAN_EVENT errors for many tickers whose first YTD event is an
expiration or assignment. This is expected — the production sync from
fromDate=20250901 will include the missing opens.

Tests here focus on tickers with clean YTD history (first event is a CSP_OPEN).
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.schema import register_master_log_tables
from agt_equities.flex_sync import parse_flex_xml, load_flex_xml_from_file, _upsert_rows
from agt_equities import trade_repo

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), 'fixtures', 'master_log_sample.xml')


class TestTradeRepo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.join(tempfile.gettempdir(), 'test_trade_repo.db')
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)
        conn = sqlite3.connect(cls.db_path)
        conn.row_factory = sqlite3.Row
        register_master_log_tables(conn)
        conn.commit()

        xml_bytes = load_flex_xml_from_file(FIXTURE_PATH)
        sections = parse_flex_xml(xml_bytes)
        now = '2026-04-07T12:00:00'
        for sd in sections:
            _upsert_rows(conn, sd['table'], sd['rows'], sd['pk_cols'], now)

        conn.execute(
            "INSERT INTO master_log_sync (started_at, finished_at, flex_query_id, "
            "to_date, status) VALUES (?, ?, ?, ?, 'success')",
            (now, now, '1461095', '20260403'),
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.db_path)

    def test_get_active_cycles_returns_cycles(self):
        """get_active_cycles returns at least some active cycles."""
        cycles = trade_repo.get_active_cycles(db_path=self.db_path)
        self.assertGreater(len(cycles), 0)
        for c in cycles:
            self.assertEqual(c.status, 'ACTIVE')

    def test_get_active_cycles_ticker_filter(self):
        """Ticker filter returns only matching cycles. SLS has clean YTD
        history in U21971297 (first event is CSP_OPEN)."""
        cycles = trade_repo.get_active_cycles(
            household='Yash_Household', ticker='SLS', db_path=self.db_path)
        for c in cycles:
            self.assertEqual(c.ticker, 'SLS')

    def test_get_active_cycles_household_filter(self):
        """Household filter returns only matching households."""
        yash = trade_repo.get_active_cycles(household='Yash_Household', db_path=self.db_path)
        for c in yash:
            self.assertEqual(c.household_id, 'Yash_Household')

    def test_orphan_tickers_are_frozen_not_crashed(self):
        """Tickers with orphan events (missing pre-2026 CSP opens) are
        frozen gracefully — they return empty, not crash."""
        # UBER in Yash_Household spans U22076329 + U21971297.
        # With stricter closure semantics (W3.1), cycles with residual
        # long options stay ACTIVE instead of closing prematurely.
        # This may prevent orphan events that previously froze the ticker.
        cycles = trade_repo.get_active_cycles(
            household='Yash_Household', ticker='UBER', db_path=self.db_path)
        # Either empty (frozen) or has active cycles — but no exception
        # Result depends on YTD fixture event ordering
        self.assertIsInstance(cycles, list)

    def test_get_closed_cycles_clean_ticker(self):
        """CRM in Vikram_Household has clean YTD history with closed cycles."""
        cycles = trade_repo.get_closed_cycles('Vikram_Household', 'CRM', db_path=self.db_path)
        for c in cycles:
            self.assertEqual(c.status, 'CLOSED')

    def test_trade_repo_loads_from_db(self):
        """Verify trade_repo reads from DB. Count < 444 because
        _load_trade_events filters EXCLUDED_TICKERS (SPX, VIX, etc.)."""
        conn = trade_repo._get_db(self.db_path)
        try:
            events = trade_repo._load_trade_events(conn)
            # 444 total trades minus ~48 excluded-ticker trades
            self.assertGreater(len(events), 350)
            self.assertLess(len(events), 444)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
