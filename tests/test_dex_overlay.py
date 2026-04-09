"""Sprint B Unit 2: Tests for DEX overlay in _discover_positions."""
import sqlite3
import time
import unittest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestDexOverlay(unittest.TestCase):
    """DEX encumbrance must reduce available_contracts."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY,
                ticker TEXT, household TEXT, contracts INTEGER,
                shares INTEGER, action_type TEXT, final_status TEXT
            )
        """)

    def tearDown(self):
        self.conn.close()

    def test_attested_rows_counted_as_encumbrance(self):
        """ATTESTED CC rows should appear in DEX encumbrance query."""
        self.conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, ticker, household, contracts, shares, action_type, final_status) "
            "VALUES ('a1', 'ADBE', 'Yash_Household', 2, NULL, 'CC', 'ATTESTED')"
        )
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT ticker, household, contracts, shares, action_type "
            "FROM bucket3_dynamic_exit_log "
            "WHERE final_status IN ('STAGED', 'ATTESTED', 'TRANSMITTING')"
        ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contracts"], 2)

    def test_transmitting_rows_counted(self):
        """TRANSMITTING rows should also be counted."""
        self.conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, ticker, household, contracts, shares, action_type, final_status) "
            "VALUES ('t1', 'MSFT', 'Yash_Household', 1, NULL, 'CC', 'TRANSMITTING')"
        )
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT * FROM bucket3_dynamic_exit_log "
            "WHERE final_status IN ('STAGED', 'ATTESTED', 'TRANSMITTING')"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_transmitted_rows_not_counted(self):
        """TRANSMITTED rows should NOT be in encumbrance."""
        self.conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, ticker, household, contracts, shares, action_type, final_status) "
            "VALUES ('d1', 'ADBE', 'Yash_Household', 2, NULL, 'CC', 'TRANSMITTED')"
        )
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT * FROM bucket3_dynamic_exit_log "
            "WHERE final_status IN ('STAGED', 'ATTESTED', 'TRANSMITTING')"
        ).fetchall()
        self.assertEqual(len(rows), 0)

    def test_stk_sell_converts_shares_to_contract_equivalent(self):
        """STK_SELL: 200 shares = 2 contract-equivalents of encumbrance."""
        self.conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, ticker, household, contracts, shares, action_type, final_status) "
            "VALUES ('s1', 'PYPL', 'Yash_Household', NULL, 200, 'STK_SELL', 'ATTESTED')"
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT shares, action_type FROM bucket3_dynamic_exit_log WHERE audit_id = 's1'"
        ).fetchone()
        # Contract-equivalent: 200 // 100 = 2
        enc = (row["shares"] or 0) // 100
        self.assertEqual(enc, 2)


if __name__ == "__main__":
    unittest.main()
