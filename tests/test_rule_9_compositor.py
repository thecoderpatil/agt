"""Sprint B Unit 1: Tests for R9 Red Alert Compositor wiring."""
import sqlite3
import unittest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agt_equities.rule_engine import (
    RuleEvaluation, evaluate_rule_9, evaluate_rule_9_composite,
)


def _create_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS red_alert_state (
            household TEXT PRIMARY KEY,
            is_fired INTEGER NOT NULL DEFAULT 0,
            fired_at TEXT,
            cleared_at TEXT,
            conditions_json TEXT
        )
    """)
    conn.commit()


class TestR9CompositorWiring(unittest.TestCase):
    """R9 must fire when 2+ simultaneous RED conditions are present."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _create_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_r9_fires_on_two_red_conditions(self):
        """2 RED conditions (A + B) → R9 should fire RED."""
        # Condition A: 3+ R1 RED evals (concentration breaches)
        # Condition B: R2 RED (EL below minimum)
        softened_evals = [
            RuleEvaluation("rule_1", "Concentration", "Yash_Household", "ADBE", 35.0, "RED", "35% > 20%"),
            RuleEvaluation("rule_1", "Concentration", "Yash_Household", "MSFT", 28.0, "RED", "28% > 20%"),
            RuleEvaluation("rule_1", "Concentration", "Yash_Household", "PYPL", 25.0, "RED", "25% > 20%"),
            RuleEvaluation("rule_2", "EL Retention", "Yash_Household", None, 0.3, "RED", "30% < 70% required"),
            RuleEvaluation("rule_11", "Leverage", "Yash_Household", None, 1.6, "GREEN", "1.6x < 1.5x limit"),
        ]

        result = evaluate_rule_9(
            ps=None,  # not used by compositor path
            household="Yash_Household",
            prior_evals=softened_evals,
            conn=self.conn,
        )

        self.assertEqual(result.rule_id, "rule_9")
        self.assertEqual(result.status, "RED", f"R9 should fire RED with 2 conditions, got: {result.status}")
        self.assertNotIn("NOT IMPLEMENTED", result.message)
        self.assertNotIn("stub", result.message.lower())

    def test_r9_green_on_single_red(self):
        """Only 1 RED condition → R9 should NOT fire (need 2+)."""
        softened_evals = [
            RuleEvaluation("rule_1", "Concentration", "Yash_Household", "ADBE", 35.0, "RED", "35% > 20%"),
            RuleEvaluation("rule_1", "Concentration", "Yash_Household", "MSFT", 28.0, "RED", "28% > 20%"),
            RuleEvaluation("rule_1", "Concentration", "Yash_Household", "PYPL", 25.0, "RED", "25% > 20%"),
            # Only condition A met (3x R1 RED). B and C are GREEN.
            RuleEvaluation("rule_2", "EL Retention", "Yash_Household", None, 0.8, "GREEN", "80% >= 70%"),
        ]

        result = evaluate_rule_9(
            ps=None,
            household="Yash_Household",
            prior_evals=softened_evals,
            conn=self.conn,
        )

        self.assertEqual(result.rule_id, "rule_9")
        # Should NOT be RED (only 1 condition met, need 2)
        self.assertNotEqual(result.status, "RED",
                           f"R9 should not fire with only 1 condition, got: {result.status}")

    def test_r9_not_stub(self):
        """R9 with conn+evals must NOT return stub sentinel."""
        evals = [
            RuleEvaluation("rule_1", "Concentration", "Yash_Household", "ADBE", 10.0, "GREEN", "ok"),
        ]
        result = evaluate_rule_9(
            ps=None,
            household="Yash_Household",
            prior_evals=evals,
            conn=self.conn,
        )
        self.assertNotIn("NOT IMPLEMENTED", result.message)

    def test_r9_falls_back_to_stub_without_conn(self):
        """Without conn, R9 returns stub (backward compat)."""
        result = evaluate_rule_9(ps=None, household="Yash_Household")
        self.assertIn("no prior_evals", result.message)


if __name__ == "__main__":
    unittest.main()
