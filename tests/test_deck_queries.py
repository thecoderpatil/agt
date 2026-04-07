"""Test deck queries run against live DB and return expected shapes."""
import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_deck.db import get_ro_conn
from agt_deck import queries


class TestDeckQueries(unittest.TestCase):

    def setUp(self):
        self.conn = get_ro_conn()

    def tearDown(self):
        self.conn.close()

    def test_portfolio_nav_returns_dict(self):
        result = queries.get_portfolio_nav(self.conn)
        self.assertIsInstance(result, dict)
        # Should have at least 1 account
        self.assertGreater(len(result), 0)

    def test_recent_fills_returns_list(self):
        result = queries.get_recent_fills(self.conn)
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("symbol", result[0])
            self.assertIn("net_cash", result[0])

    def test_last_sync_returns_dict(self):
        result = queries.get_last_sync(self.conn)
        self.assertIsNotNone(result)
        self.assertIn("sync_id", result)
        self.assertIn("status", result)

    def test_recon_summary_has_keys(self):
        result = queries.get_recon_summary(self.conn)
        self.assertIn("a_status", result)
        self.assertIn("b_status", result)
        self.assertIn("c_status", result)


if __name__ == '__main__':
    unittest.main()
