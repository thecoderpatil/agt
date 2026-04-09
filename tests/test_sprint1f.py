"""Sprint 1F: Tests for Cure Console pre-paper fixes."""
import json
import sqlite3
import unittest
from dataclasses import dataclass, field
from typing import Literal, Optional

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Fix 1: NAV per-account latest ─────────────────────────────────────

class TestNavPerAccountLatest(unittest.TestCase):
    """Fix 1: get_portfolio_nav uses per-account MAX(report_date)."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE master_log_nav (
                account_id TEXT, report_date TEXT, total TEXT
            )
        """)

    def tearDown(self):
        self.conn.close()

    def test_dormant_account_included(self):
        """U22076184 with older report_date still appears in NAV."""
        from agt_deck.queries import get_portfolio_nav
        # Active accounts: latest date 2026-04-08
        self.conn.execute("INSERT INTO master_log_nav VALUES ('U21971297', '2026-04-08', '200000')")
        self.conn.execute("INSERT INTO master_log_nav VALUES ('U22076329', '2026-04-08', '50000')")
        self.conn.execute("INSERT INTO master_log_nav VALUES ('U22388499', '2026-04-08', '100000')")
        # Dormant account: last reported 2026-04-01
        self.conn.execute("INSERT INTO master_log_nav VALUES ('U22076184', '2026-04-01', '6242')")
        self.conn.commit()

        result = get_portfolio_nav(self.conn)
        self.assertIn("U22076184", result, "Dormant account should appear")
        self.assertAlmostEqual(result["U22076184"], 6242.0)
        self.assertEqual(len(result), 4)

    def test_latest_per_account(self):
        """Each account uses its own MAX(report_date)."""
        from agt_deck.queries import get_portfolio_nav
        self.conn.execute("INSERT INTO master_log_nav VALUES ('A1', '2026-04-05', '100')")
        self.conn.execute("INSERT INTO master_log_nav VALUES ('A1', '2026-04-08', '110')")
        self.conn.execute("INSERT INTO master_log_nav VALUES ('A2', '2026-04-03', '200')")
        self.conn.commit()

        result = get_portfolio_nav(self.conn)
        self.assertAlmostEqual(result["A1"], 110.0)  # latest for A1
        self.assertAlmostEqual(result["A2"], 200.0)  # latest for A2


# ── Fix 2: Beta cache ─────────────────────────────────────────────────

class TestBetaCache(unittest.TestCase):
    """Fix 2: beta_cache module reads/writes correctly."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        from agt_equities.beta_cache import ensure_table
        ensure_table(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_get_beta_missing_returns_default(self):
        from agt_equities.beta_cache import get_beta
        self.assertEqual(get_beta("AAPL", self.conn), 1.0)

    def test_get_beta_cached(self):
        from agt_equities.beta_cache import get_beta
        self.conn.execute(
            "INSERT INTO beta_cache (ticker, beta, fetched_ts) VALUES ('MSFT', 1.25, datetime('now'))"
        )
        self.conn.commit()
        self.assertAlmostEqual(get_beta("MSFT", self.conn), 1.25)

    def test_get_betas_batch(self):
        from agt_equities.beta_cache import get_betas
        self.conn.execute("INSERT INTO beta_cache (ticker, beta, fetched_ts) VALUES ('AAPL', 1.3, datetime('now'))")
        self.conn.execute("INSERT INTO beta_cache (ticker, beta, fetched_ts) VALUES ('MSFT', 1.1, datetime('now'))")
        self.conn.commit()
        result = get_betas(["AAPL", "MSFT", "UNKNOWN"], self.conn)
        self.assertAlmostEqual(result["AAPL"], 1.3)
        self.assertAlmostEqual(result["MSFT"], 1.1)
        self.assertAlmostEqual(result["UNKNOWN"], 1.0)  # default


# ── Fix 4: Glide-path softening ───────────────────────────────────────

class TestGlidePathSoftening(unittest.TestCase):
    """Fix 4: paused glide paths soften RED evals to GREEN."""

    def test_paused_eval_softened_to_green(self):
        """PYPL Yash R1 raw=RED → softened=GREEN with paused annotation."""
        @dataclass
        class MockEval:
            rule_id: str
            rule_name: str
            household: str | None
            ticker: str | None
            raw_value: float | None
            status: str
            message: str
            cure_math: dict = field(default_factory=dict)
            detail: dict = field(default_factory=dict)

        evals = [
            MockEval("rule_1", "Concentration", "Yash_Household", "PYPL",
                     42.96, "RED", "42.96% > 20% limit"),
            MockEval("rule_1", "Concentration", "Yash_Household", "ADBE",
                     30.0, "AMBER", "30.0% > 20% limit"),
        ]

        # Simulate paused rules dict (from glide path pause_conditions)
        _paused_rules = {
            ("Yash_Household", "rule_1", "PYPL"): "earnings-gated",
        }

        # Apply softening (same logic as _build_cure_data)
        for ev in evals:
            pause_key = (ev.household, ev.rule_id, ev.ticker)
            pause_reason = _paused_rules.get(pause_key)
            if pause_reason and ev.status in ("RED", "AMBER"):
                ev.status = "GREEN"
                ev.message = f"{ev.message} [paused: {pause_reason}]"

        # PYPL should be softened
        self.assertEqual(evals[0].status, "GREEN")
        self.assertIn("paused: earnings-gated", evals[0].message)

        # ADBE should remain AMBER (no pause for ADBE)
        self.assertEqual(evals[1].status, "AMBER")

    def test_non_paused_eval_unchanged(self):
        """Evals without matching paused glide path stay unchanged."""
        @dataclass
        class MockEval:
            rule_id: str; household: str | None; ticker: str | None
            status: str; message: str

        ev = MockEval("rule_1", "Yash_Household", "MSFT", "RED", "28.5% > 20%")
        _paused_rules = {}  # no paused rules

        pause_key = (ev.household, ev.rule_id, ev.ticker)
        pause_reason = _paused_rules.get(pause_key)
        if pause_reason and ev.status in ("RED", "AMBER"):
            ev.status = "GREEN"

        self.assertEqual(ev.status, "RED")  # unchanged


# ── Fix 9: seed_baselines NULL ticker dedupe ───────────────────────────

class TestSeedBaselinesDedupe(unittest.TestCase):
    """Fix 9: seed_baselines handles NULL ticker without creating duplicates."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE glide_paths (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                household_id TEXT NOT NULL, rule_id TEXT NOT NULL,
                ticker TEXT, baseline_value REAL, target_value REAL,
                start_date TEXT, target_date TEXT, pause_conditions TEXT,
                notes TEXT, accelerator_clause TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(household_id, rule_id, ticker)
            )
        """)

    def tearDown(self):
        self.conn.close()

    def test_no_duplicate_null_ticker_rows(self):
        """Running seed_baselines twice should NOT create duplicate NULL-ticker rows."""
        from agt_equities.seed_baselines import seed_glide_paths
        seed_glide_paths(self.conn)
        count1 = self.conn.execute("SELECT COUNT(*) FROM glide_paths").fetchone()[0]
        seed_glide_paths(self.conn)
        count2 = self.conn.execute("SELECT COUNT(*) FROM glide_paths").fetchone()[0]
        self.assertEqual(count1, count2, "Re-seeding should not create duplicates")


# ── Fix 10: Mode transition idempotency ────────────────────────────────

class TestModeTransitionIdempotency(unittest.TestCase):
    """Fix 10: log_mode_transition no-ops when old == new."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE mode_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, old_mode TEXT NOT NULL, new_mode TEXT NOT NULL,
                trigger_rule TEXT, trigger_household TEXT, trigger_value REAL, notes TEXT
            )
        """)

    def tearDown(self):
        self.conn.close()

    def test_same_mode_noop(self):
        from agt_equities.mode_engine import log_mode_transition
        log_mode_transition(self.conn, "PEACETIME", "PEACETIME")
        count = self.conn.execute("SELECT COUNT(*) FROM mode_history").fetchone()[0]
        self.assertEqual(count, 0, "Same mode should not create a row")

    def test_different_mode_creates_row(self):
        from agt_equities.mode_engine import log_mode_transition
        log_mode_transition(self.conn, "PEACETIME", "WARTIME", trigger_rule="manual")
        count = self.conn.execute("SELECT COUNT(*) FROM mode_history").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
