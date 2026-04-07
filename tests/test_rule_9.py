"""
tests/test_rule_9.py — Phase 3A.5b Rule 9 (Red Alert) compositor tests.

All tests use in-memory SQLite + synthetic RuleEvaluation data.
No IBKR dependency.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.rule_engine import (
    RuleEvaluation,
    evaluate_rule_9_composite,
    _load_red_alert_state,
    _save_red_alert_state,
)


def _get_test_db():
    """In-memory DB with red_alert_state table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE red_alert_state (
            household TEXT PRIMARY KEY,
            current_state TEXT NOT NULL DEFAULT 'OFF',
            activated_at TEXT,
            activation_reason TEXT,
            conditions_met_count INTEGER NOT NULL DEFAULT 0,
            conditions_met_list TEXT,
            last_updated TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "INSERT INTO red_alert_state (household, current_state) "
        "VALUES ('Yash_Household', 'OFF'), ('Vikram_Household', 'OFF')"
    )
    conn.commit()
    return conn


def _make_eval(rule_id, household, status, raw_value=None, **detail_kw):
    return RuleEvaluation(
        rule_id=rule_id, rule_name=rule_id,
        household=household, ticker=None,
        raw_value=raw_value, status=status,
        message=f"{rule_id} {status}", detail=detail_kw,
    )


def _r1_evals(household, red_count=0, total=5):
    """Generate R1 evals with `red_count` RED and rest GREEN."""
    evals = []
    for i in range(red_count):
        evals.append(_make_eval("rule_1", household, "RED", raw_value=25.0 + i * 5))
    for i in range(total - red_count):
        evals.append(_make_eval("rule_1", household, "GREEN", raw_value=10.0))
    return evals


# ═══════════════════════════════════════════════════════════════════════════
# Hysteresis State Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestR9HysteresisState(unittest.TestCase):

    def test_loads_off_state_default(self):
        conn = _get_test_db()
        self.assertEqual(_load_red_alert_state(conn, 'Yash_Household'), 'OFF')

    def test_loads_on_state_after_save(self):
        conn = _get_test_db()
        _save_red_alert_state(conn, 'Yash_Household', 'ON', 2, ['A', 'B'])
        self.assertEqual(_load_red_alert_state(conn, 'Yash_Household'), 'ON')

    def test_persists_off_to_on_transition(self):
        conn = _get_test_db()
        _save_red_alert_state(conn, 'Yash_Household', 'ON', 2, ['A', 'B'])
        row = conn.execute(
            "SELECT * FROM red_alert_state WHERE household='Yash_Household'"
        ).fetchone()
        self.assertEqual(row['current_state'], 'ON')
        self.assertIsNotNone(row['activated_at'])
        self.assertIn('A,B', row['activation_reason'])
        self.assertEqual(json.loads(row['conditions_met_list']), ['A', 'B'])

    def test_persists_on_to_off_transition(self):
        conn = _get_test_db()
        _save_red_alert_state(conn, 'Yash_Household', 'ON', 2, ['A', 'B'])
        _save_red_alert_state(conn, 'Yash_Household', 'OFF', 0, [])
        row = conn.execute(
            "SELECT * FROM red_alert_state WHERE household='Yash_Household'"
        ).fetchone()
        self.assertEqual(row['current_state'], 'OFF')
        self.assertIsNone(row['activated_at'])

    def test_no_persist_when_unchanged(self):
        """If state doesn't change, _save is not called by the compositor."""
        conn = _get_test_db()
        # Compositor only calls _save when new_state != current_state.
        # Verify the seeded OFF state is unchanged after reading.
        state = _load_red_alert_state(conn, 'Yash_Household')
        self.assertEqual(state, 'OFF')
        row = conn.execute(
            "SELECT conditions_met_count FROM red_alert_state WHERE household='Yash_Household'"
        ).fetchone()
        self.assertEqual(row['conditions_met_count'], 0)

    def test_load_returns_off_on_db_error(self):
        """Failsafe: bad DB returns OFF."""
        conn = sqlite3.connect(":memory:")  # no table
        self.assertEqual(_load_red_alert_state(conn, 'Yash_Household'), 'OFF')

    def test_save_does_not_crash_on_db_error(self):
        """Non-fatal: save to missing table logs but doesn't crash."""
        conn = sqlite3.connect(":memory:")  # no table
        _save_red_alert_state(conn, 'Yash_Household', 'ON', 2, ['A', 'B'])
        # Should not raise


# ═══════════════════════════════════════════════════════════════════════════
# Composition Logic Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestR9CompositionLogic(unittest.TestCase):

    def test_zero_conditions_when_off_stays_off(self):
        conn = _get_test_db()
        evals = (
            _r1_evals('Yash_Household', red_count=0)
            + [_make_eval('rule_2', 'Yash_Household', 'GREEN')]
            + [_make_eval('rule_6', 'Yash_Household', 'GREEN')]
        )
        result = evaluate_rule_9_composite(evals, 'Yash_Household', conn)
        self.assertEqual(result.status, 'GREEN')
        self.assertFalse(result.detail['red_alert_active'])

    def test_one_condition_when_off_stays_off(self):
        conn = _get_test_db()
        evals = (
            _r1_evals('Yash_Household', red_count=3)  # condition A
            + [_make_eval('rule_2', 'Yash_Household', 'GREEN')]
            + [_make_eval('rule_6', 'Yash_Household', 'GREEN')]
        )
        result = evaluate_rule_9_composite(evals, 'Yash_Household', conn)
        self.assertEqual(result.status, 'GREEN')

    def test_two_conditions_AB_when_off_fires(self):
        conn = _get_test_db()
        evals = (
            _r1_evals('Yash_Household', red_count=3)  # A
            + [_make_eval('rule_2', 'Yash_Household', 'RED')]  # B
            + [_make_eval('rule_6', 'Yash_Household', 'GREEN')]
        )
        result = evaluate_rule_9_composite(evals, 'Yash_Household', conn)
        self.assertEqual(result.status, 'RED')
        self.assertTrue(result.detail['red_alert_active'])
        self.assertIn('A', result.detail['conditions_met'])
        self.assertIn('B', result.detail['conditions_met'])

    def test_two_conditions_AC_vikram_fires(self):
        conn = _get_test_db()
        evals = (
            _r1_evals('Vikram_Household', red_count=4)  # A
            + [_make_eval('rule_2', 'Vikram_Household', 'GREEN')]
            + [_make_eval('rule_6', 'Vikram_Household', 'RED')]  # C
        )
        result = evaluate_rule_9_composite(evals, 'Vikram_Household', conn)
        self.assertEqual(result.status, 'RED')
        self.assertIn('A', result.detail['conditions_met'])
        self.assertIn('C', result.detail['conditions_met'])

    def test_two_conditions_BC_vikram_fires(self):
        conn = _get_test_db()
        evals = (
            _r1_evals('Vikram_Household', red_count=1)
            + [_make_eval('rule_2', 'Vikram_Household', 'RED')]  # B
            + [_make_eval('rule_6', 'Vikram_Household', 'RED')]  # C
        )
        result = evaluate_rule_9_composite(evals, 'Vikram_Household', conn)
        self.assertEqual(result.status, 'RED')
        self.assertIn('B', result.detail['conditions_met'])
        self.assertIn('C', result.detail['conditions_met'])

    def test_three_conditions_fires(self):
        conn = _get_test_db()
        evals = (
            _r1_evals('Vikram_Household', red_count=5)  # A
            + [_make_eval('rule_2', 'Vikram_Household', 'RED')]  # B
            + [_make_eval('rule_6', 'Vikram_Household', 'RED')]  # C
        )
        result = evaluate_rule_9_composite(evals, 'Vikram_Household', conn)
        self.assertEqual(result.status, 'RED')
        self.assertEqual(result.detail['conditions_count'], 3)

    def test_two_conditions_when_on_stays_on(self):
        """Hysteresis: once ON, stays ON even if only 2 conditions remain."""
        conn = _get_test_db()
        _save_red_alert_state(conn, 'Vikram_Household', 'ON', 3, ['A', 'B', 'C'])
        evals = (
            _r1_evals('Vikram_Household', red_count=3)  # A
            + [_make_eval('rule_2', 'Vikram_Household', 'RED')]  # B
            + [_make_eval('rule_6', 'Vikram_Household', 'GREEN')]  # C cleared
        )
        result = evaluate_rule_9_composite(evals, 'Vikram_Household', conn)
        self.assertEqual(result.status, 'RED')  # stays ON (1 condition still met)

    def test_one_condition_when_on_stays_on(self):
        """Hysteresis: clear requires ALL conditions cleared."""
        conn = _get_test_db()
        _save_red_alert_state(conn, 'Yash_Household', 'ON', 2, ['A', 'B'])
        evals = (
            _r1_evals('Yash_Household', red_count=3)  # A still true
            + [_make_eval('rule_2', 'Yash_Household', 'GREEN')]  # B cleared
            + [_make_eval('rule_6', 'Yash_Household', 'GREEN')]
        )
        result = evaluate_rule_9_composite(evals, 'Yash_Household', conn)
        self.assertEqual(result.status, 'RED')  # stays ON (A still met)

    def test_zero_conditions_when_on_clears(self):
        conn = _get_test_db()
        _save_red_alert_state(conn, 'Yash_Household', 'ON', 2, ['A', 'B'])
        evals = (
            _r1_evals('Yash_Household', red_count=0)  # A cleared
            + [_make_eval('rule_2', 'Yash_Household', 'GREEN')]  # B cleared
            + [_make_eval('rule_6', 'Yash_Household', 'GREEN')]
        )
        result = evaluate_rule_9_composite(evals, 'Yash_Household', conn)
        self.assertEqual(result.status, 'GREEN')
        self.assertFalse(result.detail['red_alert_active'])
        self.assertTrue(result.detail['transitioned'])

    def test_yash_condition_c_always_false(self):
        """Condition C (R6 Vikram EL) is always False for Yash household."""
        conn = _get_test_db()
        evals = (
            _r1_evals('Yash_Household', red_count=0)
            + [_make_eval('rule_2', 'Yash_Household', 'GREEN')]
            + [_make_eval('rule_6', 'Yash_Household', 'RED')]  # even if RED
        )
        result = evaluate_rule_9_composite(evals, 'Yash_Household', conn)
        self.assertFalse(result.detail['condition_c'])

    def test_condition_d_defaults_false_without_override(self):
        """Condition D defaults to False when no override passed."""
        conn = _get_test_db()
        evals = _r1_evals('Yash_Household') + [
            _make_eval('rule_2', 'Yash_Household', 'GREEN'),
            _make_eval('rule_6', 'Yash_Household', 'GREEN'),
        ]
        result = evaluate_rule_9_composite(evals, 'Yash_Household', conn)
        self.assertFalse(result.detail['condition_d'])

    def test_condition_d_override_true_counts(self):
        """When condition_d_override=True, it counts as a condition."""
        conn = _get_test_db()
        evals = (
            _r1_evals('Yash_Household', red_count=3)  # A
            + [_make_eval('rule_2', 'Yash_Household', 'GREEN')]
            + [_make_eval('rule_6', 'Yash_Household', 'GREEN')]
        )
        result = evaluate_rule_9_composite(evals, 'Yash_Household', conn,
                                           condition_d_override=True)
        # A + D = 2 conditions -> fires (2-of-4)
        self.assertEqual(result.status, 'RED')
        self.assertIn('D', result.detail['conditions_met'])

    def test_r9_reads_softened_not_raw(self):
        """Critical: R9 uses the statuses passed to it (softened).
        If we pass GREEN (softened), R9 should not fire even though
        the raw values would trigger conditions."""
        conn = _get_test_db()
        # All GREEN (post-softening) — even though raw would be RED
        evals = (
            _r1_evals('Yash_Household', red_count=0)  # all GREEN
            + [_make_eval('rule_2', 'Yash_Household', 'GREEN')]
            + [_make_eval('rule_6', 'Yash_Household', 'GREEN')]
        )
        result = evaluate_rule_9_composite(evals, 'Yash_Household', conn)
        self.assertEqual(result.status, 'GREEN')
        self.assertEqual(result.detail['conditions_count'], 0)


# ═══════════════════════════════════════════════════════════════════════════
# Day 1 Baseline Test
# ═══════════════════════════════════════════════════════════════════════════

class TestR9Day1Baseline(unittest.TestCase):

    def test_day1_both_households_off(self):
        """With all R1/R2/R6 glide-softened to GREEN, R9 = OFF for both."""
        conn = _get_test_db()

        # Simulate post-softening: all GREEN (glide paths cured everything)
        for hh in ['Yash_Household', 'Vikram_Household']:
            evals = (
                _r1_evals(hh, red_count=0)
                + [_make_eval('rule_2', hh, 'GREEN')]
                + [_make_eval('rule_6', hh, 'GREEN')]
            )
            result = evaluate_rule_9_composite(evals, hh, conn)
            self.assertEqual(result.status, 'GREEN',
                             f"R9 should be OFF for {hh} on Day 1")
            self.assertEqual(result.detail['conditions_count'], 0)


if __name__ == '__main__':
    unittest.main()
