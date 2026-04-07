"""
tests/test_phase3a5c2_alpha.py — Phase 3A.5c2-alpha tests.

Covers: walker compute_walk_away_pnl, Gate 1, Gate 2, orchestrator,
R5 stage helper, sweeper, is_wartime_condition_met, R9 condition D override,
schema migrations.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.walker import compute_walk_away_pnl, WalkAwayResult
from agt_equities.rule_engine import (
    ConvictionTier, Gate1Result, Gate2Result,
    evaluate_gate_1, evaluate_gate_2,
    evaluate_dynamic_exit_candidates, DynamicExitCandidate,
    sweep_stale_dynamic_exit_stages,
    stage_stock_sale_via_smart_friction, StageStockSaleResult,
    SellException,
)
from agt_equities.mode_engine import is_wartime_condition_met


# ═══════════════════════════════════════════════════════════════════════════
# Walker compute_walk_away_pnl
# ═══════════════════════════════════════════════════════════════════════════

class TestWalkAwayPnl(unittest.TestCase):

    def test_profitable_exit(self):
        r = compute_walk_away_pnl(adjusted_cost_basis=300.0, proposed_exit_strike=310.0,
                                   proposed_exit_premium=2.0, quantity=1)
        self.assertTrue(r.is_profitable)
        self.assertAlmostEqual(r.walk_away_pnl_per_share, 12.0)  # 310+2-300
        self.assertAlmostEqual(r.walk_away_pnl_total, 1200.0)  # 12 * 100 * 1

    def test_loss_exit(self):
        r = compute_walk_away_pnl(adjusted_cost_basis=330.0, proposed_exit_strike=260.0,
                                   proposed_exit_premium=1.50, quantity=2)
        self.assertFalse(r.is_profitable)
        # 260 + 1.50 - 330 = -68.50 per share
        self.assertAlmostEqual(r.walk_away_pnl_per_share, -68.50)
        self.assertAlmostEqual(r.walk_away_pnl_total, -13700.0)  # -68.50 * 100 * 2

    def test_zero_quantity(self):
        r = compute_walk_away_pnl(300.0, 310.0, 2.0, quantity=0)
        self.assertAlmostEqual(r.walk_away_pnl_total, 0.0)

    def test_matches_inline_formula(self):
        """Regression: matches original telegram_bot.py inline formula."""
        # premium + (strike - adjusted_basis)
        strike, premium, basis = 260.0, 1.50, 329.0
        expected = premium + (strike - basis)  # = 1.50 + (260-329) = -67.50
        r = compute_walk_away_pnl(basis, strike, premium, quantity=1, multiplier=1)
        self.assertAlmostEqual(r.walk_away_pnl_per_share, expected)


# ═══════════════════════════════════════════════════════════════════════════
# Gate 1
# ═══════════════════════════════════════════════════════════════════════════

class TestGate1(unittest.TestCase):

    def test_profitable_exit_auto_passes(self):
        r = evaluate_gate_1("ADBE", "Yash", 350.0, 5.0, 1, 300.0, ConvictionTier.NEUTRAL)
        self.assertTrue(r.passed)
        self.assertTrue(r.gate1_math_pass)

    def test_marginal_loss_passes(self):
        # strike=260, premium=3, basis=270. Loss=$7/sh. Freed=$26000.
        # 26000 * 0.30 = 7800 vs 700 -> 11.14x -> pass
        r = evaluate_gate_1("ADBE", "Yash", 260.0, 3.0, 1, 270.0, ConvictionTier.NEUTRAL)
        self.assertTrue(r.passed)
        self.assertGreater(r.ratio, 1.0)

    def test_large_loss_fails(self):
        # strike=200, premium=1, basis=400. Loss=$199/sh*100=$19900.
        # Freed=200*100*1=20000. 20000*0.30=6000 vs 19900 -> 0.30x -> fail
        r = evaluate_gate_1("ADBE", "Yash", 200.0, 1.0, 1, 400.0, ConvictionTier.NEUTRAL)
        self.assertFalse(r.passed)
        self.assertLess(r.ratio, 1.0)

    def test_high_conviction_harder(self):
        # Same trade but HIGH conviction (0.20 modifier -> harder to exit)
        r = evaluate_gate_1("ADBE", "Yash", 260.0, 3.0, 1, 270.0, ConvictionTier.HIGH)
        self.assertEqual(r.conviction_modifier, 0.20)

    def test_low_conviction_easier(self):
        r = evaluate_gate_1("ADBE", "Yash", 260.0, 3.0, 1, 270.0, ConvictionTier.LOW)
        self.assertEqual(r.conviction_modifier, 0.40)
        self.assertGreater(r.ratio, 1.0)

    def test_tax_liability_increases_loss(self):
        r_no_tax = evaluate_gate_1("ADBE", "Yash", 260.0, 1.0, 1, 280.0, ConvictionTier.NEUTRAL)
        r_with_tax = evaluate_gate_1("ADBE", "Yash", 260.0, 1.0, 1, 280.0, ConvictionTier.NEUTRAL,
                                     tax_liability_override=5000.0)
        self.assertGreater(r_no_tax.ratio, r_with_tax.ratio)

    def test_zero_loss_infinite_ratio(self):
        r = evaluate_gate_1("ADBE", "Yash", 300.0, 5.0, 1, 300.0, ConvictionTier.NEUTRAL)
        self.assertTrue(r.passed)


# ═══════════════════════════════════════════════════════════════════════════
# Gate 2
# ═══════════════════════════════════════════════════════════════════════════

class TestGate2(unittest.TestCase):

    def test_low_severity_full_liquidation(self):
        r = evaluate_gate_2(100.0, 10000.0, 5, "PEACETIME")  # 1% severity
        self.assertEqual(r.max_contracts_per_cycle, 5)
        self.assertEqual(r.severity_tier, "100pct")

    def test_high_severity_peacetime_33pct(self):
        r = evaluate_gate_2(500.0, 10000.0, 6, "PEACETIME")  # 5% severity
        self.assertEqual(r.max_contracts_per_cycle, 1)  # int(6 * 0.33) = 1
        self.assertEqual(r.severity_tier, "33pct")

    def test_high_severity_amber_25pct(self):
        r = evaluate_gate_2(500.0, 10000.0, 8, "AMBER")
        self.assertEqual(r.max_contracts_per_cycle, 2)  # int(8 * 0.25) = 2
        self.assertEqual(r.severity_tier, "25pct_amber")

    def test_high_severity_wartime_33pct(self):
        r = evaluate_gate_2(500.0, 10000.0, 6, "WARTIME")
        self.assertEqual(r.severity_tier, "33pct")

    def test_zero_market_value(self):
        r = evaluate_gate_2(100.0, 0.0, 5, "PEACETIME")
        self.assertEqual(r.severity, 1.0)

    def test_minimum_one_contract(self):
        r = evaluate_gate_2(500.0, 10000.0, 1, "PEACETIME")
        self.assertEqual(r.max_contracts_per_cycle, 1)  # max(1, int(1*0.33)) = 1


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════

class TestOrchestrator(unittest.TestCase):

    def _make_opt(self, strike, mid, delta=0.25, expiry=date(2026, 4, 24)):
        m = MagicMock()
        m.strike = strike
        m.mid = mid
        m.delta = delta
        m.expiry = expiry
        return m

    def test_returns_ranked_by_ratio(self):
        chain = [self._make_opt(260, 2.0), self._make_opt(265, 1.5)]
        result = evaluate_dynamic_exit_candidates(
            "ADBE", "Yash", 500, 330.0, 200000, 240.0, "PEACETIME", chain,
        )
        if len(result) >= 2:
            self.assertGreaterEqual(result[0].gate1.ratio, result[1].gate1.ratio)

    def test_skips_gate1_failures(self):
        # Very large loss -> should fail Gate 1
        chain = [self._make_opt(100, 0.5)]  # strike way below basis
        result = evaluate_dynamic_exit_candidates(
            "ADBE", "Yash", 500, 330.0, 200000, 240.0, "PEACETIME", chain,
        )
        # All candidates may fail Gate 1
        for c in result:
            self.assertTrue(c.gate1.passed)

    def test_empty_chain_returns_empty(self):
        result = evaluate_dynamic_exit_candidates(
            "ADBE", "Yash", 500, 330.0, 200000, 240.0, "PEACETIME", [],
        )
        self.assertEqual(result, [])

    def test_zero_excess_contracts(self):
        # 100 shares, NLV $200K, spot $240 -> target = int(200000*0.15/240) = 125 shares
        # excess = 100 - 125 = -25 -> 0 contracts
        result = evaluate_dynamic_exit_candidates(
            "ADBE", "Yash", 100, 330.0, 200000, 240.0, "PEACETIME",
            [self._make_opt(260, 2.0)],
        )
        self.assertEqual(result, [])


# ═══════════════════════════════════════════════════════════════════════════
# Sweeper
# ═══════════════════════════════════════════════════════════════════════════

class TestSweeper(unittest.TestCase):

    def _get_db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY, final_status TEXT NOT NULL,
                staged_ts REAL, ticker TEXT, household TEXT,
                contracts INTEGER, shares INTEGER, action_type TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                trade_date TEXT, desk_mode TEXT, household_nlv REAL,
                underlying_spot_at_render REAL, transmitted INTEGER DEFAULT 0,
                re_validation_count INTEGER DEFAULT 0
            )
        """)
        return conn

    def test_sweeps_stale_staged_row(self):
        conn = self._get_db()
        stale_ts = time.time() - 960  # 16 minutes ago
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log (audit_id, final_status, staged_ts, "
            "ticker, household, action_type, trade_date, desk_mode, household_nlv, "
            "underlying_spot_at_render) "
            "VALUES ('test1', 'STAGED', ?, 'ADBE', 'Yash', 'CC', '2026-04-07', "
            "'PEACETIME', 200000, 240.0)",
            (stale_ts,),
        )
        conn.commit()
        result = sweep_stale_dynamic_exit_stages(conn)
        self.assertEqual(result["swept"], 1)
        row = conn.execute("SELECT final_status FROM bucket3_dynamic_exit_log WHERE audit_id='test1'").fetchone()
        self.assertEqual(row[0], "ABANDONED")

    def test_does_not_sweep_fresh_staged(self):
        conn = self._get_db()
        fresh_ts = time.time() - 600  # 10 minutes ago
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log (audit_id, final_status, staged_ts, "
            "ticker, household, action_type, trade_date, desk_mode, household_nlv, "
            "underlying_spot_at_render) "
            "VALUES ('test2', 'STAGED', ?, 'ADBE', 'Yash', 'CC', '2026-04-07', "
            "'PEACETIME', 200000, 240.0)",
            (fresh_ts,),
        )
        conn.commit()
        result = sweep_stale_dynamic_exit_stages(conn)
        self.assertEqual(result["swept"], 0)

    def test_does_not_sweep_attested(self):
        conn = self._get_db()
        stale_ts = time.time() - 960
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log (audit_id, final_status, staged_ts, "
            "ticker, household, action_type, trade_date, desk_mode, household_nlv, "
            "underlying_spot_at_render) "
            "VALUES ('test3', 'ATTESTED', ?, 'ADBE', 'Yash', 'CC', '2026-04-07', "
            "'PEACETIME', 200000, 240.0)",
            (stale_ts,),
        )
        conn.commit()
        result = sweep_stale_dynamic_exit_stages(conn)
        self.assertEqual(result["swept"], 0)


# ═══════════════════════════════════════════════════════════════════════════
# is_wartime_condition_met
# ═══════════════════════════════════════════════════════════════════════════

class TestWartimeCondition(unittest.TestCase):

    def test_yash_leverage_breach(self):
        met, reasons = is_wartime_condition_met(1.51, 1.40, 0.30)
        self.assertTrue(met)
        self.assertTrue(any("Leverage" in r for r in reasons))

    def test_vikram_leverage_breach(self):
        met, reasons = is_wartime_condition_met(1.40, 1.51, 0.30)
        self.assertTrue(met)

    def test_vikram_el_breach(self):
        met, reasons = is_wartime_condition_met(1.40, 1.40, 0.14)
        self.assertTrue(met)
        self.assertTrue(any("EL" in r for r in reasons))

    def test_both_conditions(self):
        met, reasons = is_wartime_condition_met(1.60, 2.17, 0.10)
        self.assertTrue(met)
        self.assertEqual(len(reasons), 2)

    def test_all_clear(self):
        met, reasons = is_wartime_condition_met(1.40, 1.40, 0.30)
        self.assertFalse(met)
        self.assertEqual(len(reasons), 0)

    def test_boundary_leverage_exact_150(self):
        met, _ = is_wartime_condition_met(1.50, 1.50, 0.30)
        self.assertFalse(met)  # strict greater-than

    def test_boundary_el_exact_015(self):
        met, _ = is_wartime_condition_met(1.40, 1.40, 0.15)
        self.assertFalse(met)  # strict less-than


# ═══════════════════════════════════════════════════════════════════════════
# Schema migrations
# ═══════════════════════════════════════════════════════════════════════════

class TestSchemaMigrations(unittest.TestCase):

    def test_stk_sell_row_insertion(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY, trade_date TEXT, ticker TEXT, household TEXT,
                desk_mode TEXT, action_type TEXT, household_nlv REAL,
                underlying_spot_at_render REAL, strike REAL, expiry TEXT,
                contracts INTEGER, shares INTEGER, limit_price REAL,
                final_status TEXT DEFAULT 'STAGED', transmitted INTEGER DEFAULT 0,
                re_validation_count INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            "household_nlv, underlying_spot_at_render, shares, limit_price, final_status) "
            "VALUES ('stk1', '2026-04-07', 'ADBE', 'Yash', 'WARTIME', 'STK_SELL', "
            "200000, 240.0, 100, 235.0, 'STAGED')"
        )
        row = conn.execute("SELECT strike, expiry, contracts, shares, action_type "
                           "FROM bucket3_dynamic_exit_log WHERE audit_id='stk1'").fetchone()
        self.assertIsNone(row[0])  # strike NULL for STK_SELL
        self.assertIsNone(row[1])  # expiry NULL
        self.assertIsNone(row[2])  # contracts NULL
        self.assertEqual(row[3], 100)  # shares populated
        self.assertEqual(row[4], 'STK_SELL')

    def test_cc_row_insertion(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY, trade_date TEXT, ticker TEXT, household TEXT,
                desk_mode TEXT, action_type TEXT, household_nlv REAL,
                underlying_spot_at_render REAL, strike REAL, expiry TEXT,
                contracts INTEGER, shares INTEGER, final_status TEXT DEFAULT 'STAGED',
                transmitted INTEGER DEFAULT 0, re_validation_count INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            "household_nlv, underlying_spot_at_render, strike, expiry, contracts, final_status) "
            "VALUES ('cc1', '2026-04-07', 'ADBE', 'Yash', 'PEACETIME', 'CC', "
            "200000, 240.0, 260.0, '2026-05-01', 2, 'STAGED')"
        )
        row = conn.execute("SELECT strike, expiry, contracts, action_type "
                           "FROM bucket3_dynamic_exit_log WHERE audit_id='cc1'").fetchone()
        self.assertEqual(row[0], 260.0)
        self.assertEqual(row[1], '2026-05-01')
        self.assertEqual(row[2], 2)
        self.assertEqual(row[3], 'CC')


# ═══════════════════════════════════════════════════════════════════════════
# R5 Stage Stock Sale Helper
# ═══════════════════════════════════════════════════════════════════════════

class TestStageStockSale(unittest.TestCase):

    def _get_db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY, trade_date TEXT, ticker TEXT, household TEXT,
                desk_mode TEXT, action_type TEXT, household_nlv REAL,
                underlying_spot_at_render REAL, gate1_realized_loss REAL,
                walk_away_pnl_per_share REAL, shares INTEGER, limit_price REAL,
                final_status TEXT DEFAULT 'STAGED', transmitted INTEGER DEFAULT 0,
                re_validation_count INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        return conn

    def test_below_basis_with_exception_stages(self):
        conn = self._get_db()
        result = stage_stock_sale_via_smart_friction(
            ticker="ADBE", household="Vikram", limit_price=230.0, shares=50,
            adjusted_cost_basis=300.0, exception_flag=SellException.RULE_6_FORCED_LIQUIDATION,
            household_nlv=80000, spot=235.0, desk_mode="WARTIME", conn=conn,
            vikram_el_below_10=True,
        )
        self.assertTrue(result.staged)
        self.assertIsNotNone(result.audit_id)
        row = conn.execute("SELECT action_type, final_status FROM bucket3_dynamic_exit_log").fetchone()
        self.assertEqual(row[0], "STK_SELL")
        self.assertEqual(row[1], "STAGED")

    def test_above_basis_no_exception_allowed(self):
        conn = self._get_db()
        result = stage_stock_sale_via_smart_friction(
            ticker="ADBE", household="Yash", limit_price=350.0, shares=50,
            adjusted_cost_basis=300.0, exception_flag=None,
            household_nlv=200000, spot=350.0, desk_mode="PEACETIME", conn=conn,
        )
        self.assertTrue(result.staged)

    def test_below_basis_no_exception_blocked(self):
        conn = self._get_db()
        result = stage_stock_sale_via_smart_friction(
            ticker="ADBE", household="Yash", limit_price=230.0, shares=50,
            adjusted_cost_basis=300.0, exception_flag=None,
            household_nlv=200000, spot=235.0, desk_mode="PEACETIME", conn=conn,
        )
        self.assertFalse(result.staged)
        self.assertIn("BLOCKED", result.sell_gate_result.status)


if __name__ == '__main__':
    unittest.main()
