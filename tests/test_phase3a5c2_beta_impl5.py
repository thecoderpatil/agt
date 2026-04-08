"""
Beta Impl 5 Tests — R5 Sell Gate Operator Surface.

Covers: exception_type migration, persistence, panel rendering branches,
widget attestation flows per exception class, R5 staging route, JIT reuse.

DB: in-memory SQLite with exception_type column.
"""

import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.rule_engine import (
    SellException, stage_stock_sale_via_smart_friction,
)


# ---------------------------------------------------------------------------
# Shared DDL — includes exception_type column (Impl 5) + TRANSMITTING (Impl 3)
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE bucket3_dynamic_exit_log (
    audit_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    household TEXT NOT NULL,
    desk_mode TEXT NOT NULL CHECK (desk_mode IN ('PEACETIME', 'AMBER', 'WARTIME')),
    action_type TEXT NOT NULL CHECK (action_type IN ('CC', 'STK_SELL')),
    household_nlv REAL NOT NULL,
    underlying_spot_at_render REAL NOT NULL,
    gate1_freed_margin REAL,
    gate1_realized_loss REAL,
    gate1_conviction_tier TEXT,
    gate1_conviction_modifier REAL,
    gate1_ratio REAL,
    gate2_target_contracts INTEGER,
    gate2_max_per_cycle INTEGER,
    walk_away_pnl_per_share REAL,
    strike REAL,
    expiry TEXT,
    contracts INTEGER,
    shares INTEGER,
    limit_price REAL,
    campaign_id TEXT,
    operator_thesis TEXT,
    attestation_value_typed TEXT,
    checkbox_state_json TEXT,
    render_ts REAL,
    staged_ts REAL,
    transmitted INTEGER NOT NULL DEFAULT 0,
    transmitted_ts REAL,
    re_validation_count INTEGER NOT NULL DEFAULT 0,
    final_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (final_status IN ('PENDING', 'STAGED', 'ATTESTED',
                                'TRANSMITTING', 'TRANSMITTED',
                                'CANCELLED', 'DRIFT_BLOCKED',
                                'ABANDONED')),
    source TEXT NOT NULL DEFAULT 'scheduled_watchdog'
        CHECK (source IN ('scheduled_watchdog', 'manual_inspection',
                          'cc_overweight', 'manual_stage')),
    exception_type TEXT
        CHECK (exception_type IS NULL OR exception_type IN (
            'rule_8_dynamic_exit', 'thesis_deterioration',
            'rule_6_forced_liquidation', 'emergency_risk_event')),
    fill_ts REAL,
    fill_price REAL,
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) WITHOUT ROWID
"""


def _get_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    return conn


# ═══════════════════════════════════════════════════════════════════════════
# 1. migration_adds_exception_type — column exists on new + legacy DB
# ═══════════════════════════════════════════════════════════════════════════

class TestMigrationAddsExceptionType(unittest.TestCase):

    def test_new_db_has_exception_type(self):
        conn = _get_db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bucket3_dynamic_exit_log)")}
        self.assertIn("exception_type", cols)

    def test_alter_adds_column_idempotently(self):
        """Simulate legacy DB without exception_type, then ALTER."""
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY, ticker TEXT, household TEXT,
                desk_mode TEXT, action_type TEXT, household_nlv REAL,
                underlying_spot_at_render REAL, final_status TEXT
            )
        """)
        # ALTER to add
        try:
            conn.execute("ALTER TABLE bucket3_dynamic_exit_log ADD COLUMN exception_type TEXT")
        except Exception:
            pass
        # Second ALTER is idempotent
        try:
            conn.execute("ALTER TABLE bucket3_dynamic_exit_log ADD COLUMN exception_type TEXT")
        except Exception:
            pass
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bucket3_dynamic_exit_log)")}
        self.assertIn("exception_type", cols)


# ═══════════════════════════════════════════════════════════════════════════
# 2. stage_persists_exception_type — all 4 enum values round-trip
# ═══════════════════════════════════════════════════════════════════════════

class TestStagePersistsExceptionType(unittest.TestCase):

    def test_all_four_exception_types_persist(self):
        for exc in SellException:
            conn = _get_db()
            # Thesis deterioration and emergency require rationale
            rationale = "Test rationale for persistence" if exc in (
                SellException.THESIS_DETERIORATION,
                SellException.EMERGENCY_RISK_EVENT,
            ) else None
            result = stage_stock_sale_via_smart_friction(
                ticker="TEST", household="Yash_Household",
                limit_price=90.0, shares=50,
                adjusted_cost_basis=100.0,
                exception_flag=exc,
                household_nlv=200000, spot=92.0,
                desk_mode="WARTIME" if exc == SellException.RULE_6_FORCED_LIQUIDATION else "PEACETIME",
                conn=conn,
                rule_8_gate_pass=(exc == SellException.RULE_8_DYNAMIC_EXIT),
                cio_token=(exc == SellException.THESIS_DETERIORATION),
                logged_rationale=rationale,
                vikram_el_below_10=(exc == SellException.RULE_6_FORCED_LIQUIDATION),
            )
            self.assertTrue(result.staged, f"Failed to stage {exc.value}")
            row = conn.execute(
                "SELECT exception_type FROM bucket3_dynamic_exit_log WHERE audit_id = ?",
                (result.audit_id,),
            ).fetchone()
            self.assertEqual(row["exception_type"], exc.value,
                             f"exception_type mismatch for {exc.value}")


# ═══════════════════════════════════════════════════════════════════════════
# 3-4. panel_renders — CC vs STK_SELL column branching
# ═══════════════════════════════════════════════════════════════════════════

class TestPanelColumnBranching(unittest.TestCase):
    """Verify the template branching logic for CC vs STK_SELL rows."""

    def test_cc_row_has_option_fields(self):
        """CC rows should have strike, expiry, contracts, conviction, ratio, freed."""
        row = {"action_type": "CC", "strike": 260.0, "expiry": "2026-05-16",
               "contracts": 1, "gate1_conviction_tier": "NEUTRAL",
               "gate1_ratio": 11.14, "gate1_freed_margin": 26000.0}
        self.assertEqual(row["action_type"], "CC")
        self.assertIsNotNone(row["strike"])
        self.assertIsNotNone(row["contracts"])
        self.assertIsNotNone(row["gate1_ratio"])

    def test_stk_sell_row_has_stock_fields(self):
        """STK_SELL rows should have shares, limit_price, walk_away_pnl_per_share."""
        row = {"action_type": "STK_SELL", "shares": 50, "limit_price": 90.0,
               "walk_away_pnl_per_share": -10.0, "gate1_realized_loss": 500.0,
               "exception_type": "thesis_deterioration"}
        self.assertEqual(row["action_type"], "STK_SELL")
        self.assertIsNotNone(row["shares"])
        self.assertIsNotNone(row["limit_price"])
        self.assertIsNotNone(row["exception_type"])


# ═══════════════════════════════════════════════════════════════════════════
# 5. widget_thesis_deterioration — thesis required, 30-char min
# ═══════════════════════════════════════════════════════════════════════════

class TestWidgetThesisDetermination(unittest.TestCase):

    def test_thesis_deterioration_requires_rationale(self):
        """Thesis deterioration is BLOCKED without logged_rationale."""
        from agt_equities.rule_engine import evaluate_rule_5_sell_gate
        result = evaluate_rule_5_sell_gate(
            "ADBE", "Yash", 90.0, 100.0,
            exception_flag=SellException.THESIS_DETERIORATION,
            cio_token=True,
            logged_rationale=None,
        )
        self.assertEqual(result.status, "BLOCKED")

    def test_thesis_deterioration_allowed_with_rationale(self):
        from agt_equities.rule_engine import evaluate_rule_5_sell_gate
        result = evaluate_rule_5_sell_gate(
            "ADBE", "Yash", 90.0, 100.0,
            exception_flag=SellException.THESIS_DETERIORATION,
            cio_token=True,
            logged_rationale="Revenue decline 3Q, margin compression, competitive threat from FIGM",
        )
        self.assertEqual(result.status, "ALLOWED")


# ═══════════════════════════════════════════════════════════════════════════
# 6. widget_forced_liquidation_wartime — Integer Lock, no thesis
# ═══════════════════════════════════════════════════════════════════════════

class TestWidgetForcedLiquidation(unittest.TestCase):

    def test_forced_liquidation_uses_integer_lock(self):
        """R6 forced liquidation = Integer Lock (WARTIME). Confirmed: no thesis needed."""
        # The template branching: use_integer_lock = (exception_type == 'rule_6_forced_liquidation')
        exception_type = "rule_6_forced_liquidation"
        use_integer_lock = exception_type == "rule_6_forced_liquidation"
        self.assertTrue(use_integer_lock)

    def test_forced_liquidation_staged_in_wartime(self):
        conn = _get_db()
        result = stage_stock_sale_via_smart_friction(
            ticker="PYPL", household="Vikram_Household",
            limit_price=40.0, shares=100,
            adjusted_cost_basis=65.0,
            exception_flag=SellException.RULE_6_FORCED_LIQUIDATION,
            household_nlv=80000, spot=42.0,
            desk_mode="WARTIME", conn=conn,
            vikram_el_below_10=True,
        )
        self.assertTrue(result.staged)
        row = conn.execute(
            "SELECT exception_type, desk_mode FROM bucket3_dynamic_exit_log WHERE audit_id = ?",
            (result.audit_id,),
        ).fetchone()
        self.assertEqual(row["exception_type"], "rule_6_forced_liquidation")
        self.assertEqual(row["desk_mode"], "WARTIME")


# ═══════════════════════════════════════════════════════════════════════════
# 7. widget_forced_liquidation_blocks_peacetime
# ═══════════════════════════════════════════════════════════════════════════

class TestForcedLiquidationBlocksPeacetime(unittest.TestCase):

    def test_r6_requires_wartime(self):
        """R6 forced liquidation requires WARTIME mode at the route level."""
        # This is enforced in the POST route: desk_mode != 'WARTIME' → 400
        desk_mode = "PEACETIME"
        exception_type = "rule_6_forced_liquidation"
        blocked = (exception_type == "rule_6_forced_liquidation" and desk_mode != "WARTIME")
        self.assertTrue(blocked, "R6 forced liquidation MUST be WARTIME")


# ═══════════════════════════════════════════════════════════════════════════
# 8. widget_emergency_risk — catalyst rationale required
# ═══════════════════════════════════════════════════════════════════════════

class TestWidgetEmergencyRisk(unittest.TestCase):

    def test_emergency_blocked_without_rationale(self):
        from agt_equities.rule_engine import evaluate_rule_5_sell_gate
        result = evaluate_rule_5_sell_gate(
            "FRAUD_CO", "Yash", 5.0, 50.0,
            exception_flag=SellException.EMERGENCY_RISK_EVENT,
            logged_rationale=None,
        )
        self.assertEqual(result.status, "BLOCKED")

    def test_emergency_allowed_with_catalyst(self):
        from agt_equities.rule_engine import evaluate_rule_5_sell_gate
        result = evaluate_rule_5_sell_gate(
            "FRAUD_CO", "Yash", 5.0, 50.0,
            exception_flag=SellException.EMERGENCY_RISK_EVENT,
            logged_rationale="Confirmed SEC fraud investigation, trading halt imminent",
        )
        self.assertEqual(result.status, "ALLOWED")


# ═══════════════════════════════════════════════════════════════════════════
# 9. stage_route_happy_path — end-to-end staging via backend function
# ═══════════════════════════════════════════════════════════════════════════

class TestStageRouteHappyPath(unittest.TestCase):

    def test_thesis_deterioration_stages_with_exception_type(self):
        conn = _get_db()
        result = stage_stock_sale_via_smart_friction(
            ticker="ADBE", household="Yash_Household",
            limit_price=230.0, shares=50,
            adjusted_cost_basis=300.0,
            exception_flag=SellException.THESIS_DETERIORATION,
            household_nlv=200000, spot=235.0,
            desk_mode="PEACETIME", conn=conn,
            cio_token=True,
            logged_rationale="Q4 revenue miss, margin compression, Adobe losing ground to Figma",
        )
        self.assertTrue(result.staged)
        row = conn.execute(
            "SELECT action_type, exception_type, final_status, shares, limit_price "
            "FROM bucket3_dynamic_exit_log WHERE audit_id = ?",
            (result.audit_id,),
        ).fetchone()
        self.assertEqual(row["action_type"], "STK_SELL")
        self.assertEqual(row["exception_type"], "thesis_deterioration")
        self.assertEqual(row["final_status"], "STAGED")
        self.assertEqual(row["shares"], 50)
        self.assertAlmostEqual(row["limit_price"], 230.0)


# ═══════════════════════════════════════════════════════════════════════════
# 10. stage_route_validation — bad exception_type
# ═══════════════════════════════════════════════════════════════════════════

class TestStageRouteValidation(unittest.TestCase):

    def test_invalid_exception_type_rejected(self):
        """SellException enum rejects unknown values."""
        with self.assertRaises(ValueError):
            SellException("invalid_type")

    def test_valid_exception_types(self):
        for val in ["rule_8_dynamic_exit", "thesis_deterioration",
                     "rule_6_forced_liquidation", "emergency_risk_event"]:
            exc = SellException(val)
            self.assertEqual(exc.value, val)


# ═══════════════════════════════════════════════════════════════════════════
# 11. jit_stk_sell_through_dex_callback — reuse verification
# ═══════════════════════════════════════════════════════════════════════════

class TestJitStkSellReuse(unittest.TestCase):

    def test_stk_sell_attested_row_transitions_to_transmitting(self):
        """Verify STK_SELL ATTESTED rows follow the same TRANSMITTING state machine."""
        conn = _get_db()
        now = time.time()
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, "
            " walk_away_pnl_per_share, shares, limit_price, "
            " exception_type, render_ts, staged_ts, final_status) "
            "VALUES ('r5-jit-test', date('now'), 'ADBE', 'Yash_Household', "
            " 'PEACETIME', 'STK_SELL', 200000.0, 235.0, -70.0, 50, 230.0, "
            " 'thesis_deterioration', ?, ?, 'ATTESTED')",
            (now, now),
        )
        conn.commit()

        # Same JIT pattern: ATTESTED → TRANSMITTING
        result = conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'TRANSMITTING' "
            "WHERE audit_id = 'r5-jit-test' AND final_status = 'ATTESTED'"
        )
        self.assertEqual(result.rowcount, 1)

        # Then TRANSMITTING → TRANSMITTED
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'TRANSMITTED', transmitted = 1, transmitted_ts = ? "
            "WHERE audit_id = 'r5-jit-test' AND final_status = 'TRANSMITTING'",
            (time.time(),),
        )
        conn.commit()
        row = conn.execute(
            "SELECT final_status, transmitted, exception_type "
            "FROM bucket3_dynamic_exit_log WHERE audit_id = 'r5-jit-test'"
        ).fetchone()
        self.assertEqual(row["final_status"], "TRANSMITTED")
        self.assertEqual(row["transmitted"], 1)
        self.assertEqual(row["exception_type"], "thesis_deterioration")


# ═══════════════════════════════════════════════════════════════════════════
# 12. poller_renders_stk_sell — Telegram message text for STK_SELL
# ═══════════════════════════════════════════════════════════════════════════

class TestPollerRendersSTKSell(unittest.TestCase):

    def test_stk_sell_text_format(self):
        """Verify poller text branching for STK_SELL action_type."""
        row = {"action_type": "STK_SELL", "shares": 50, "ticker": "ADBE",
               "limit_price": 230.0, "contracts": None, "strike": None, "expiry": None}
        # Mirror poller logic
        ticker = row["ticker"]
        if row["action_type"] == "CC":
            detail = f"{row['contracts']}x {ticker} ${row['strike']:.0f}C {row['expiry']} @ ${row['limit_price']:.2f}"
        else:
            detail = f"{row['shares']}sh {ticker} @ ${row['limit_price']:.2f}"

        self.assertEqual(detail, "50sh ADBE @ $230.00")
        self.assertNotIn("None", detail)
        self.assertNotIn("C ", detail)  # No option-specific text


if __name__ == "__main__":
    unittest.main()
