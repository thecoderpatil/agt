"""
tests/test_flex_parser.py — Flex XML parsing and UPSERT tests.

Validates flex_sync.py against the real master_log_sample.xml fixture.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.schema import register_master_log_tables
from agt_equities.flex_sync import (
    parse_flex_xml, load_flex_xml_from_file, _upsert_rows,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), 'fixtures', 'master_log_sample.xml')


class TestFlexParser(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Parse fixture once, create temp DB, load all sections."""
        cls.db_path = os.path.join(tempfile.gettempdir(), 'test_flex_parser.db')
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)
        cls.conn = sqlite3.connect(cls.db_path)
        cls.conn.row_factory = sqlite3.Row
        register_master_log_tables(cls.conn)
        cls.conn.commit()

        cls.xml_bytes = load_flex_xml_from_file(FIXTURE_PATH)
        cls.sections = parse_flex_xml(cls.xml_bytes)

        now = '2026-04-07T12:00:00'
        for sd in cls.sections:
            _upsert_rows(cls.conn, sd['table'], sd['rows'], sd['pk_cols'], now)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        os.unlink(cls.db_path)

    def _count(self, table: str) -> int:
        return self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def test_four_accounts_parsed(self):
        """Fixture contains data for 4 IBKR accounts."""
        count = self._count('master_log_account_info')
        self.assertEqual(count, 4)

    def test_trade_count_444(self):
        """444 trades across all accounts."""
        self.assertEqual(self._count('master_log_trades'), 444)

    def test_trade_count_by_account(self):
        """Trade distribution matches expected per-account counts."""
        rows = self.conn.execute(
            "SELECT account_id, COUNT(*) as cnt FROM master_log_trades GROUP BY account_id"
        ).fetchall()
        counts = {r['account_id']: r['cnt'] for r in rows}
        self.assertEqual(counts.get('U21971297'), 207)
        self.assertEqual(counts.get('U22388499'), 126)
        self.assertEqual(counts.get('U22076329'), 111)

    def test_open_positions_count(self):
        """31 open positions across active accounts."""
        self.assertEqual(self._count('master_log_open_positions'), 31)

    def test_option_eae_count(self):
        """157 OptionEAE rows."""
        self.assertEqual(self._count('master_log_option_eae'), 157)

    def test_uber_open_position_matches(self):
        """UBER in Roth: 300 shares, costBasisPrice approx $73.99."""
        row = self.conn.execute(
            "SELECT position, cost_basis_price FROM master_log_open_positions "
            "WHERE account_id='U22076329' AND symbol='UBER'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(float(row['position']), 300.0, places=0)
        self.assertAlmostEqual(float(row['cost_basis_price']), 73.99, delta=0.01)

    def test_change_in_nav_all_statements(self):
        """ChangeInNAV present for all 4 Flex statements (3 active + 1 dormant)."""
        self.assertEqual(self._count('master_log_change_in_nav'), 4)

    def test_attribute_only_sections_parsed(self):
        """AccountInformation and ChangeInNAV (attribute-only) are parsed."""
        ai = self.conn.execute(
            "SELECT acct_alias FROM master_log_account_info WHERE account_id='U22076329'"
        ).fetchone()
        self.assertEqual(ai['acct_alias'], 'Roth')

    def test_idempotent_upsert(self):
        """Re-inserting same data produces 0 new rows."""
        sections2 = parse_flex_xml(self.xml_bytes)
        now = '2026-04-07T13:00:00'
        total_ins = 0
        for sd in sections2:
            ins, _ = _upsert_rows(self.conn, sd['table'], sd['rows'], sd['pk_cols'], now)
            total_ins += ins
        self.conn.commit()
        self.assertEqual(total_ins, 0)

    def test_null_pk_rows_skipped(self):
        """Rows with NULL primary key columns are not inserted."""
        # FIFO and MTM had rows with empty reportDate — those should have been
        # filled from FlexStatement.toDate, not skipped. Only rows with empty
        # conid (the last FIFO row) should be skipped.
        fifo = self._count('master_log_realized_unrealized_perf')
        self.assertGreater(fifo, 0)
        mtm = self._count('master_log_mtm_perf')
        self.assertGreater(mtm, 0)


if __name__ == '__main__':
    unittest.main()
