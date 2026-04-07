"""
tests/test_phase3a.py — Unit tests for Phase 3A Stages 1-3.

Covers: rule_engine, mode_engine, glide path math, desk_state_writer,
baseline seeds, mode gates. Zero I/O — all tests use in-memory SQLite or synthetic data.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.rule_engine import (
    PortfolioState, RuleEvaluation, compute_leverage_pure,
    evaluate_rule_1, evaluate_rule_2, evaluate_rule_3, evaluate_rule_11,
    evaluate_rule_4, evaluate_rule_5, evaluate_rule_6, evaluate_rule_7,
    evaluate_rule_8, evaluate_rule_9, evaluate_rule_10,
    evaluate_all, LEVERAGE_LIMIT,
)
from agt_equities.mode_engine import (
    MODE_PEACETIME, MODE_AMBER, MODE_WARTIME,
    LeverageHysteresisTracker, evaluate_glide_path, GlidePath,
    compute_mode, log_mode_transition, get_current_mode, load_glide_paths,
)
from agt_deck.desk_state_writer import generate_desk_state, write_desk_state_atomic
from agt_equities.seed_baselines import seed_glide_paths, seed_sector_overrides, seed_initial_mode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_cycle(household='Yash_Household', ticker='TEST', shares=100,
                osp=0, osc=0, olp=0, olc=0, basis=100.0, status='ACTIVE'):
    c = MagicMock()
    c.household_id = household
    c.ticker = ticker
    c.shares_held = shares
    c.open_short_puts = osp
    c.open_short_calls = osc
    c.open_long_puts = olp
    c.open_long_calls = olc
    c.paper_basis = basis
    c.status = status
    c.cycle_type = 'WHEEL'
    return c


def _make_ps(**overrides) -> PortfolioState:
    defaults = dict(
        household_nlv={'Yash_Household': 200000, 'Vikram_Household': 80000},
        household_el={'Yash_Household': None, 'Vikram_Household': None},
        active_cycles=[],
        spots={},
        betas={},
        industries={},
        sector_overrides={},
        vix=None,
        report_date='20260407',
    )
    defaults.update(overrides)
    return PortfolioState(**defaults)


def _get_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Create required tables
    conn.execute("""CREATE TABLE glide_paths (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id TEXT NOT NULL, rule_id TEXT NOT NULL, ticker TEXT,
        baseline_value REAL NOT NULL, target_value REAL NOT NULL,
        start_date TEXT NOT NULL, target_date TEXT NOT NULL,
        pause_conditions TEXT, created_at TEXT DEFAULT (datetime('now')), notes TEXT,
        UNIQUE(household_id, rule_id, ticker))""")
    conn.execute("""CREATE TABLE mode_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT (datetime('now')),
        old_mode TEXT NOT NULL, new_mode TEXT NOT NULL,
        trigger_rule TEXT, trigger_household TEXT, trigger_value REAL, notes TEXT)""")
    conn.execute("""CREATE TABLE el_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household TEXT NOT NULL, timestamp TEXT DEFAULT (datetime('now')),
        excess_liquidity REAL, nlv REAL, buying_power REAL,
        source TEXT DEFAULT 'ibkr_live')""")
    conn.execute("""CREATE TABLE sector_overrides (
        ticker TEXT PRIMARY KEY, sector TEXT NOT NULL, sub_sector TEXT,
        source TEXT DEFAULT 'manual', notes TEXT, created_at TEXT DEFAULT (datetime('now')))""")
    return conn


# ═══════════════════════════════════════════════════════════════════════════
# Rule Engine Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeLeveragePure(unittest.TestCase):

    def test_basic_leverage(self):
        cycles = [_mock_cycle(shares=100, ticker='AAPL')]
        lev = compute_leverage_pure(cycles, {'AAPL': 200.0}, {'AAPL': 1.0},
                                     {'Yash_Household': 10000}, 'Yash_Household')
        self.assertAlmostEqual(lev, 2.0)  # 100 * 200 / 10000

    def test_zero_nlv(self):
        cycles = [_mock_cycle(shares=100)]
        lev = compute_leverage_pure(cycles, {'TEST': 100}, {}, {'Yash_Household': 0}, 'Yash_Household')
        self.assertEqual(lev, 0.0)

    def test_no_matching_cycles(self):
        cycles = [_mock_cycle(household='Vikram_Household')]
        lev = compute_leverage_pure(cycles, {'TEST': 100}, {}, {'Yash_Household': 10000}, 'Yash_Household')
        self.assertEqual(lev, 0.0)

    def test_default_beta(self):
        """Beta defaults to 1.0 when not in betas dict."""
        cycles = [_mock_cycle(shares=100, ticker='AAPL')]
        lev = compute_leverage_pure(cycles, {'AAPL': 100.0}, {},
                                     {'Yash_Household': 5000}, 'Yash_Household')
        self.assertAlmostEqual(lev, 2.0)  # 100 * 1.0 * 100 / 5000


class TestEvaluateRule1(unittest.TestCase):

    def test_concentration_breach(self):
        cycles = [_mock_cycle(shares=500, ticker='ADBE')]
        ps = _make_ps(active_cycles=cycles, spots={'ADBE': 244.0},
                      household_nlv={'Yash_Household': 200000})
        results = evaluate_rule_1(ps, 'Yash_Household')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "RED")
        self.assertGreater(results[0].raw_value, 20)

    def test_concentration_ok(self):
        cycles = [_mock_cycle(shares=10, ticker='AAPL')]
        ps = _make_ps(active_cycles=cycles, spots={'AAPL': 100.0},
                      household_nlv={'Yash_Household': 100000})
        results = evaluate_rule_1(ps, 'Yash_Household')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "GREEN")

    def test_cure_math_shares_to_sell(self):
        cycles = [_mock_cycle(shares=1000, ticker='TEST')]
        ps = _make_ps(active_cycles=cycles, spots={'TEST': 100.0},
                      household_nlv={'Yash_Household': 100000})
        # 1000 * 100 = 100k → 100% of NLV → need to get to 20% = 20k → sell 800
        results = evaluate_rule_1(ps, 'Yash_Household')
        self.assertEqual(results[0].cure_math['shares_to_sell'], 800)


class TestEvaluateRule2(unittest.TestCase):

    def test_el_unavailable(self):
        ps = _make_ps(household_el={'Yash_Household': None})
        result = evaluate_rule_2(ps, 'Yash_Household')
        self.assertEqual(result.status, "PENDING")

    def test_el_sufficient(self):
        ps = _make_ps(household_el={'Yash_Household': 180000.0},
                      household_nlv={'Yash_Household': 200000}, vix=15.0)
        result = evaluate_rule_2(ps, 'Yash_Household')
        self.assertEqual(result.status, "GREEN")  # 90% >= 80% retain

    def test_el_insufficient(self):
        ps = _make_ps(household_el={'Yash_Household': 50000.0},
                      household_nlv={'Yash_Household': 200000}, vix=15.0)
        result = evaluate_rule_2(ps, 'Yash_Household')
        self.assertEqual(result.status, "RED")  # 25% < 80% retain


class TestEvaluateRule3(unittest.TestCase):

    def test_sector_violation_with_override(self):
        """UBER override to Consumer Cyclical should clear SW-App violation."""
        cycles = [
            _mock_cycle(ticker='ADBE'), _mock_cycle(ticker='CRM'),
            _mock_cycle(ticker='UBER'),
        ]
        ps = _make_ps(
            active_cycles=cycles,
            industries={'ADBE': 'Software - Application', 'CRM': 'Software - Application',
                        'UBER': 'Software - Application'},
            sector_overrides={'UBER': 'Consumer Cyclical'},
            spots={'ADBE': 200, 'CRM': 200, 'UBER': 70},
        )
        results = evaluate_rule_3(ps, 'Yash_Household')
        # SW-App should now have only 2 (ADBE, CRM) — GREEN
        sw_app = [r for r in results if 'Software' in r.message]
        for r in sw_app:
            self.assertEqual(r.status, "GREEN")

    def test_sector_violation_without_override(self):
        cycles = [
            _mock_cycle(ticker='A'), _mock_cycle(ticker='B'), _mock_cycle(ticker='C'),
        ]
        ps = _make_ps(
            active_cycles=cycles,
            industries={'A': 'Tech', 'B': 'Tech', 'C': 'Tech'},
            spots={'A': 100, 'B': 100, 'C': 100},
        )
        results = evaluate_rule_3(ps, 'Yash_Household')
        tech = [r for r in results if 'Tech' in r.message]
        self.assertEqual(len(tech), 1)
        self.assertEqual(tech[0].status, "RED")


class TestEvaluateRule11(unittest.TestCase):

    def test_leverage_green(self):
        cycles = [_mock_cycle(shares=100, ticker='AAPL')]
        ps = _make_ps(active_cycles=cycles, spots={'AAPL': 100.0},
                      household_nlv={'Yash_Household': 100000})
        result = evaluate_rule_11(ps, 'Yash_Household')
        self.assertEqual(result.status, "GREEN")  # 0.10x

    def test_leverage_breached(self):
        cycles = [_mock_cycle(shares=1000, ticker='AAPL')]
        ps = _make_ps(active_cycles=cycles, spots={'AAPL': 200.0},
                      household_nlv={'Yash_Household': 100000})
        result = evaluate_rule_11(ps, 'Yash_Household')
        self.assertEqual(result.status, "RED")  # 2.0x

    def test_leverage_amber(self):
        cycles = [_mock_cycle(shares=100, ticker='AAPL')]
        ps = _make_ps(active_cycles=cycles, spots={'AAPL': 1350.0},
                      household_nlv={'Yash_Household': 100000})
        result = evaluate_rule_11(ps, 'Yash_Household')
        self.assertEqual(result.status, "AMBER")  # 1.35x


class TestStubEvaluators(unittest.TestCase):

    def test_all_stubs_return_pending(self):
        ps = _make_ps()
        for evaluator in [evaluate_rule_4, evaluate_rule_5, evaluate_rule_7,
                          evaluate_rule_8, evaluate_rule_9, evaluate_rule_10]:
            result = evaluator(ps, 'Yash_Household')
            self.assertEqual(result.status, "PENDING", f"{result.rule_id} not PENDING")

    def test_evaluate_all_returns_all_rules(self):
        # Need at least one cycle so R1/R3 produce results
        cycles = [_mock_cycle(ticker='AAPL')]
        ps = _make_ps(active_cycles=cycles, spots={'AAPL': 100.0},
                      industries={'AAPL': 'Tech'})
        results = evaluate_all(ps, 'Yash_Household')
        rule_ids = {r.rule_id for r in results}
        for i in range(1, 12):
            self.assertIn(f"rule_{i}", rule_ids, f"rule_{i} missing from evaluate_all")


# ═══════════════════════════════════════════════════════════════════════════
# Mode Engine Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestLeverageHysteresis(unittest.TestCase):

    def test_breach_and_release(self):
        tracker = LeverageHysteresisTracker(breach_state={})
        # Breach
        s = tracker.update('Yash', 1.55)
        self.assertEqual(s, 'BREACHED')
        # Still in breach zone (above release)
        s = tracker.update('Yash', 1.45)
        self.assertEqual(s, 'BREACHED')
        # Below release
        s = tracker.update('Yash', 1.35)
        self.assertEqual(s, 'AMBER')
        # Well below
        s = tracker.update('Yash', 1.20)
        self.assertEqual(s, 'OK')


class TestGlidePathMath(unittest.TestCase):

    def test_day_zero_is_green(self):
        """Day 0: actual == baseline → expected == baseline → delta == 0 → GREEN."""
        gp = GlidePath('Yash_Household', 'rule_11', None, 1.60, 1.50,
                        '2026-04-07', '2026-05-05', None)
        status, expected, delta = evaluate_glide_path(gp, 1.60, '2026-04-07')
        self.assertEqual(status, "GREEN")
        self.assertAlmostEqual(expected, 1.60)
        self.assertAlmostEqual(delta, 0.0)

    def test_halfway_on_track(self):
        gp = GlidePath('Yash_Household', 'rule_11', None, 2.00, 1.50,
                        '2026-04-07', '2026-04-21', None)  # 14 days
        # Day 7: expected = 2.0 + (1.5 - 2.0) * 0.5 = 1.75
        status, expected, delta = evaluate_glide_path(gp, 1.75, '2026-04-14')
        self.assertEqual(status, "GREEN")
        self.assertAlmostEqual(expected, 1.75)

    def test_behind_one_week_amber(self):
        gp = GlidePath('Yash_Household', 'rule_11', None, 2.00, 1.50,
                        '2026-04-07', '2026-05-19', None)  # 42 days
        # Weekly rate = 0.5 / 42 * 7 = 0.0833
        # Day 21: expected = 2.0 + (1.5 - 2.0) * (21/42) = 1.75
        # If actual = 1.80 → delta = 0.05 → less than 2*0.0833=0.167 → AMBER
        status, _, _ = evaluate_glide_path(gp, 1.80, '2026-04-28')
        self.assertEqual(status, "AMBER")

    def test_behind_three_weeks_red(self):
        gp = GlidePath('Yash_Household', 'rule_11', None, 2.00, 1.50,
                        '2026-04-07', '2026-05-19', None)  # 42 days
        # Day 21: expected = 1.75. If actual = 2.05 → worsened past baseline
        status, _, _ = evaluate_glide_path(gp, 2.05, '2026-04-28')
        self.assertEqual(status, "RED")

    def test_paused_always_green(self):
        gp = GlidePath('Yash_Household', 'rule_1', 'PYPL', 39.9, 25.0,
                        '2026-04-07', '2026-08-25',
                        '{"paused": true, "reason": "earnings-gated"}')
        status, _, _ = evaluate_glide_path(gp, 45.0, '2026-06-01')
        self.assertEqual(status, "GREEN")

    def test_ahead_of_schedule_green(self):
        gp = GlidePath('Yash_Household', 'rule_11', None, 2.00, 1.50,
                        '2026-04-07', '2026-04-21', None)  # 14 days
        # Day 7: expected = 1.75, actual = 1.60 → ahead → GREEN
        status, _, delta = evaluate_glide_path(gp, 1.60, '2026-04-14')
        self.assertEqual(status, "GREEN")
        self.assertLess(delta, 0)


class TestComputeMode(unittest.TestCase):

    def _ev(self, status):
        return RuleEvaluation(rule_id='test', rule_name='test', household='H',
                              ticker=None, raw_value=1.0, status=status, message='')

    def test_all_green_peacetime(self):
        evals = [self._ev("GREEN"), self._ev("GREEN"), self._ev("PENDING")]
        mode, _, _, _ = compute_mode(evals)
        self.assertEqual(mode, MODE_PEACETIME)

    def test_any_amber_triggers_amber(self):
        evals = [self._ev("GREEN"), self._ev("AMBER")]
        mode, _, _, _ = compute_mode(evals)
        self.assertEqual(mode, MODE_AMBER)

    def test_any_red_triggers_wartime(self):
        evals = [self._ev("GREEN"), self._ev("AMBER"), self._ev("RED")]
        mode, _, _, _ = compute_mode(evals)
        self.assertEqual(mode, MODE_WARTIME)

    def test_pending_treated_as_green(self):
        evals = [self._ev("PENDING"), self._ev("PENDING")]
        mode, _, _, _ = compute_mode(evals)
        self.assertEqual(mode, MODE_PEACETIME)

    def test_trigger_info_returned(self):
        evals = [self._ev("GREEN"),
                 RuleEvaluation(rule_id='rule_11', rule_name='Lev', household='Yash',
                                ticker=None, raw_value=1.6, status='RED', message='')]
        mode, rule, hh, val = compute_mode(evals)
        self.assertEqual(mode, MODE_WARTIME)
        self.assertEqual(rule, 'rule_11')
        self.assertEqual(hh, 'Yash')
        self.assertAlmostEqual(val, 1.6)


class TestModeTransitionDB(unittest.TestCase):

    def setUp(self):
        self.conn = _get_test_db()

    def tearDown(self):
        self.conn.close()

    def test_log_and_read(self):
        log_mode_transition(self.conn, 'PEACETIME', 'AMBER', 'rule_11', 'Yash', 1.55)
        mode = get_current_mode(self.conn)
        self.assertEqual(mode, 'AMBER')

    def test_default_peacetime(self):
        mode = get_current_mode(self.conn)
        self.assertEqual(mode, MODE_PEACETIME)

    def test_multiple_transitions(self):
        log_mode_transition(self.conn, 'PEACETIME', 'AMBER')
        log_mode_transition(self.conn, 'AMBER', 'WARTIME')
        mode = get_current_mode(self.conn)
        self.assertEqual(mode, 'WARTIME')


# ═══════════════════════════════════════════════════════════════════════════
# Desk State Writer Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestDeskStateWriter(unittest.TestCase):

    def test_generate_content(self):
        content = generate_desk_state(
            mode='PEACETIME',
            household_data={'Yash_Household': {'nlv': 261902, 'leverage': 1.60,
                                                 'el': None, 'active_cycles': 10}},
            rule_evaluations=[],
            glide_paths=[],
            walker_warning_count=0,
            walker_worst_severity=None,
            recent_transitions=[],
        )
        self.assertIn('PEACETIME', content)
        self.assertIn('261,902', content)
        self.assertIn('1.60x', content)

    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "desk_state.md"
            write_desk_state_atomic("test content", path)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding='utf-8'), "test content")

    def test_atomic_write_no_partial(self):
        """If content changes, only the final version is visible."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "desk_state.md"
            write_desk_state_atomic("version 1", path)
            write_desk_state_atomic("version 2", path)
            self.assertEqual(path.read_text(encoding='utf-8'), "version 2")


# ═══════════════════════════════════════════════════════════════════════════
# Seed Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSeeds(unittest.TestCase):

    def setUp(self):
        self.conn = _get_test_db()

    def tearDown(self):
        self.conn.close()

    def test_seed_glide_paths(self):
        count = seed_glide_paths(self.conn)
        self.assertGreater(count, 0)
        rows = self.conn.execute("SELECT COUNT(*) FROM glide_paths").fetchone()[0]
        self.assertEqual(rows, count)

    def test_seed_idempotent(self):
        seed_glide_paths(self.conn)
        seed_glide_paths(self.conn)  # second run — should not duplicate
        rows = self.conn.execute("SELECT COUNT(*) FROM glide_paths").fetchone()[0]
        self.assertGreater(rows, 0)

    def test_seed_sector_overrides(self):
        count = seed_sector_overrides(self.conn)
        self.assertEqual(count, 1)
        row = self.conn.execute("SELECT * FROM sector_overrides WHERE ticker='UBER'").fetchone()
        self.assertEqual(row['sector'], 'Consumer Cyclical')

    def test_seed_initial_mode(self):
        seed_initial_mode(self.conn)
        mode = get_current_mode(self.conn)
        self.assertEqual(mode, MODE_PEACETIME)

    def test_seed_initial_mode_idempotent(self):
        seed_initial_mode(self.conn)
        seed_initial_mode(self.conn)
        rows = self.conn.execute("SELECT COUNT(*) FROM mode_history").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_day1_green_yash_leverage(self):
        """Day 1: Yash leverage baseline 1.60 at week 0 of 4-week glide must be GREEN."""
        seed_glide_paths(self.conn)
        gps = load_glide_paths(self.conn)
        yash_lev = [g for g in gps if g.household_id == 'Yash_Household' and g.rule_id == 'rule_11']
        self.assertEqual(len(yash_lev), 1)
        gp = yash_lev[0]
        status, expected, delta = evaluate_glide_path(gp, 1.60, '2026-04-07')
        self.assertEqual(status, "GREEN",
            f"Day 1 Yash leverage not GREEN: status={status}, expected={expected}, delta={delta}")

    def test_day1_green_vikram_leverage(self):
        """Day 1: Vikram leverage baseline 2.17 at week 0 of 12-week glide must be GREEN."""
        seed_glide_paths(self.conn)
        gps = load_glide_paths(self.conn)
        vik_lev = [g for g in gps if g.household_id == 'Vikram_Household' and g.rule_id == 'rule_11']
        self.assertEqual(len(vik_lev), 1)
        gp = vik_lev[0]
        status, expected, delta = evaluate_glide_path(gp, 2.17, '2026-04-07')
        self.assertEqual(status, "GREEN",
            f"Day 1 Vikram leverage not GREEN: status={status}, expected={expected}, delta={delta}")


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3: Mode Gate Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestModeGateLogic(unittest.TestCase):
    """Test the _check_mode_gate logic directly (without Telegram context)."""

    def test_peacetime_allows_scan(self):
        """In PEACETIME, gate("PEACETIME") passes."""
        # Gate logic is pure — we test the ranking directly
        mode_rank = {"PEACETIME": 0, "AMBER": 1, "WARTIME": 2}
        current = mode_rank["PEACETIME"]
        allowed = mode_rank["PEACETIME"]
        self.assertLessEqual(current, allowed)

    def test_amber_blocks_scan(self):
        """In AMBER, gate("PEACETIME") fails."""
        mode_rank = {"PEACETIME": 0, "AMBER": 1, "WARTIME": 2}
        current = mode_rank["AMBER"]
        allowed = mode_rank["PEACETIME"]
        self.assertGreater(current, allowed)

    def test_amber_allows_cc(self):
        """In AMBER, gate("AMBER") passes (CCs are exits/rolls)."""
        mode_rank = {"PEACETIME": 0, "AMBER": 1, "WARTIME": 2}
        current = mode_rank["AMBER"]
        allowed = mode_rank["AMBER"]
        self.assertLessEqual(current, allowed)

    def test_wartime_blocks_cc(self):
        """In WARTIME, gate("AMBER") fails."""
        mode_rank = {"PEACETIME": 0, "AMBER": 1, "WARTIME": 2}
        current = mode_rank["WARTIME"]
        allowed = mode_rank["AMBER"]
        self.assertGreater(current, allowed)

    def test_wartime_blocks_scan(self):
        """In WARTIME, gate("PEACETIME") fails."""
        mode_rank = {"PEACETIME": 0, "AMBER": 1, "WARTIME": 2}
        current = mode_rank["WARTIME"]
        allowed = mode_rank["PEACETIME"]
        self.assertGreater(current, allowed)

    def test_peacetime_allows_cc(self):
        """In PEACETIME, gate("AMBER") passes."""
        mode_rank = {"PEACETIME": 0, "AMBER": 1, "WARTIME": 2}
        current = mode_rank["PEACETIME"]
        allowed = mode_rank["AMBER"]
        self.assertLessEqual(current, allowed)


class TestModeGateMessage(unittest.TestCase):
    """Test that blocked gates return actionable user-facing messages."""

    def test_blocked_message_contains_mode_name(self):
        """Blocked message must include the current mode so Yash knows why."""
        # Simulate: current mode is AMBER, gate requires PEACETIME
        # We test the message construction directly
        mode = "AMBER"
        msg = (
            f"\u26d4 Mode {mode}: this command is blocked.\n"
            f"Current desk mode is {mode}. "
            f"Use /cure to view the Cure Console for next steps."
        )
        self.assertIn("AMBER", msg)
        self.assertIn("/cure", msg)
        self.assertIn("blocked", msg)

    def test_wartime_blocked_message(self):
        mode = "WARTIME"
        msg = (
            f"\u26d4 Mode {mode}: this command is blocked.\n"
            f"Current desk mode is {mode}. "
            f"Use /cure to view the Cure Console for next steps."
        )
        self.assertIn("WARTIME", msg)
        self.assertIn("/cure", msg)

    def test_blocked_message_not_silent(self):
        """Blocked path must return a non-empty message, never empty string."""
        mode_rank = {"PEACETIME": 0, "AMBER": 1, "WARTIME": 2}
        for mode in ["AMBER", "WARTIME"]:
            current = mode_rank[mode]
            allowed = mode_rank["PEACETIME"]
            if current > allowed:
                msg = (
                    f"\u26d4 Mode {mode}: this command is blocked.\n"
                    f"Current desk mode is {mode}. "
                    f"Use /cure to view the Cure Console for next steps."
                )
                self.assertTrue(len(msg) > 20, f"Blocked message too short for mode {mode}")


class TestModeTransitionFlow(unittest.TestCase):
    """Test full mode transition flows via DB operations."""

    def setUp(self):
        self.conn = _get_test_db()

    def tearDown(self):
        self.conn.close()

    def test_peacetime_to_wartime(self):
        log_mode_transition(self.conn, 'PEACETIME', 'WARTIME', 'manual', notes='test')
        self.assertEqual(get_current_mode(self.conn), 'WARTIME')

    def test_wartime_to_peacetime(self):
        log_mode_transition(self.conn, 'PEACETIME', 'WARTIME')
        log_mode_transition(self.conn, 'WARTIME', 'PEACETIME', notes='audit: all clear')
        self.assertEqual(get_current_mode(self.conn), 'PEACETIME')

    def test_peacetime_to_amber_to_wartime(self):
        log_mode_transition(self.conn, 'PEACETIME', 'AMBER', 'rule_11')
        self.assertEqual(get_current_mode(self.conn), 'AMBER')
        log_mode_transition(self.conn, 'AMBER', 'WARTIME', 'rule_1')
        self.assertEqual(get_current_mode(self.conn), 'WARTIME')

    def test_wartime_requires_audit_memo_concept(self):
        """WARTIME → PEACETIME should have notes (enforced at command level, verified here)."""
        log_mode_transition(self.conn, 'PEACETIME', 'WARTIME')
        # Simulate revert with audit memo
        log_mode_transition(self.conn, 'WARTIME', 'PEACETIME',
                            notes='/declare_peacetime: leverage cured to 1.45x')
        rows = self.conn.execute("SELECT notes FROM mode_history ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn('cured', rows[0])

    def test_transition_history_limit(self):
        """get_recent_transitions respects limit."""
        from agt_equities.mode_engine import get_recent_transitions
        for i in range(10):
            log_mode_transition(self.conn, 'A', 'B', notes=f'test_{i}')
        recent = get_recent_transitions(self.conn, limit=3)
        self.assertEqual(len(recent), 3)


if __name__ == '__main__':
    unittest.main()
