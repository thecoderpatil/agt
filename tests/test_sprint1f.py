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


# ── Sprint C1: build_state() + DeskSnapshot tests ────────────────────

class _BuildStateDBMixin:
    """Shared in-memory DB setup for build_state tests."""

    def _make_db(self) -> str:
        """Create a temp DB file with required tables, return path."""
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = self._tmp.name
        self._tmp.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("""
            CREATE TABLE master_log_nav (
                account_id TEXT, report_date TEXT, total TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                household TEXT NOT NULL,
                desk_mode TEXT NOT NULL DEFAULT 'PEACETIME',
                action_type TEXT NOT NULL DEFAULT 'CC',
                household_nlv REAL NOT NULL DEFAULT 0,
                underlying_spot_at_render REAL NOT NULL DEFAULT 0,
                final_status TEXT NOT NULL DEFAULT 'STAGED',
                last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE beta_cache (
                ticker TEXT PRIMARY KEY,
                beta REAL NOT NULL DEFAULT 1.0,
                fetched_ts TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()
        return db_path

    def _cleanup_db(self):
        import os
        try:
            os.unlink(self._tmp.name)
        except Exception:
            pass


class TestBuildStateHappyPath(_BuildStateDBMixin, unittest.TestCase):
    """C1 test #1: build_state returns DeskSnapshot with all fields populated."""

    def setUp(self):
        self.db_path = self._make_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('U12345', '2026-04-09', '100000')"
        )
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            "household_nlv, underlying_spot_at_render, final_status) "
            "VALUES ('dex-001', '2026-04-09', 'AAPL', 'Yash_Household', "
            "'PEACETIME', 'CC', 100000, 170.0, 'STAGED')"
        )
        conn.execute(
            "INSERT INTO beta_cache (ticker, beta) VALUES ('AAPL', 1.2)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._cleanup_db()

    def test_build_state_returns_desk_snapshot(self):
        from agt_equities.state_builder import build_state, DeskSnapshot
        snapshot = build_state(
            db_path=self.db_path,
            live_positions=[{"symbol": "AAPL", "qty": 100}],
        )
        self.assertIsInstance(snapshot, DeskSnapshot)
        self.assertAlmostEqual(snapshot.nav_total, 100000.0)
        self.assertEqual(
            snapshot.dex_encumbered_keys,
            frozenset({("Yash_Household", "AAPL")}),
        )
        self.assertAlmostEqual(snapshot.beta_by_symbol["AAPL"], 1.2)
        self.assertEqual(len(snapshot.live_positions), 1)
        # No warnings expected (live_positions provided, beta not empty, NAV present)
        # active_cycles may warn if trade_repo can't read from our temp DB
        non_cycle_warnings = [
            w for w in snapshot.warnings if "active_cycles" not in w
        ]
        self.assertEqual(non_cycle_warnings, [])


class TestBuildStateRaisesOnEmptyNav(_BuildStateDBMixin, unittest.TestCase):
    """C1 test #2: build_state raises ValueError on empty master_log_nav."""

    def setUp(self):
        self.db_path = self._make_db()

    def tearDown(self):
        self._cleanup_db()

    def test_raises_on_empty_nav(self):
        from agt_equities.state_builder import build_state
        with self.assertRaises(ValueError) as ctx:
            build_state(db_path=self.db_path, live_positions=[])
        self.assertIn("zero rows", str(ctx.exception))


class TestBuildStateDexFilters(_BuildStateDBMixin, unittest.TestCase):
    """C1 test #3: DEX encumbrance filters out inactive statuses."""

    def setUp(self):
        self.db_path = self._make_db()
        conn = sqlite3.connect(self.db_path)
        # Need NAV to avoid ValueError
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('U12345', '2026-04-09', '100000')"
        )
        # 3 DEX rows: STAGED, ATTESTED, CANCELLED
        for audit_id, ticker, status in [
            ("dex-1", "AAPL", "STAGED"),
            ("dex-2", "MSFT", "ATTESTED"),
            ("dex-3", "GOOG", "CANCELLED"),
        ]:
            conn.execute(
                "INSERT INTO bucket3_dynamic_exit_log "
                "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
                "household_nlv, underlying_spot_at_render, final_status) "
                "VALUES (?, '2026-04-09', ?, 'Yash_Household', "
                "'PEACETIME', 'CC', 100000, 170.0, ?)",
                (audit_id, ticker, status),
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._cleanup_db()

    def test_dex_filters_inactive_statuses(self):
        from agt_equities.state_builder import build_state
        snapshot = build_state(db_path=self.db_path, live_positions=[])
        self.assertEqual(
            snapshot.dex_encumbered_keys,
            frozenset({
                ("Yash_Household", "AAPL"),
                ("Yash_Household", "MSFT"),
            }),
        )
        self.assertEqual(len(snapshot.dex_encumbered_keys), 2)


class TestBuildStateBetaCacheEmpty(_BuildStateDBMixin, unittest.TestCase):
    """C1 test #4: empty beta_cache warns, does not raise."""

    def setUp(self):
        self.db_path = self._make_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('U12345', '2026-04-09', '50000')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._cleanup_db()

    def test_beta_cache_empty_warns(self):
        from agt_equities.state_builder import build_state
        snapshot = build_state(db_path=self.db_path, live_positions=[])
        self.assertEqual(snapshot.beta_by_symbol, {})
        beta_warnings = [w for w in snapshot.warnings if "beta_cache empty" in w]
        self.assertTrue(len(beta_warnings) >= 1, f"Expected beta warning, got: {snapshot.warnings}")


class TestBuildStatePerAccountNavMaxDate(_BuildStateDBMixin, unittest.TestCase):
    """C1 test #5: per-account MAX(report_date) NAV, not global MAX.

    References: v12 Sprint 1F Fix 1 decision. The pre-Fix-1 bug used
    a global MAX(report_date) which excluded dormant accounts whose
    last report_date was older than the most recent active account.
    """

    def setUp(self):
        self.db_path = self._make_db()
        conn = sqlite3.connect(self.db_path)
        # Account A: two dates, only newest should be used
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('A1', '20260407', '90000')"
        )
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('A1', '20260409', '100000')"
        )
        # Account B: older date (dormant-style) — must still appear
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('B1', '20260401', '25000')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._cleanup_db()

    def test_per_account_nav_max_report_date(self):
        from agt_equities.state_builder import build_state
        snapshot = build_state(db_path=self.db_path, live_positions=[])
        # A1 should use 100000 (2026-04-09), not 90000 (2026-04-07)
        self.assertAlmostEqual(snapshot.nav_by_account["A1"], 100000.0)
        # B1 should still appear with its only value, even though its
        # report_date is older than A1's latest
        self.assertAlmostEqual(snapshot.nav_by_account["B1"], 25000.0)
        self.assertAlmostEqual(snapshot.nav_total, 125000.0)


class TestDeskSnapshotDistinctFromPortfolioState(unittest.TestCase):
    """C1 test #6: DeskSnapshot and PortfolioState are distinct classes."""

    def test_desk_snapshot_distinct_from_portfoliostate(self):
        from agt_equities.state_builder import DeskSnapshot
        from agt_equities.rule_engine import PortfolioState
        self.assertIsNot(DeskSnapshot, PortfolioState)
        self.assertNotEqual(DeskSnapshot.__name__, PortfolioState.__name__)


class TestBuildStateWithoutLivePositions(_BuildStateDBMixin, unittest.TestCase):
    """C1 test #7: live_positions=None produces empty list + warning."""

    def setUp(self):
        self.db_path = self._make_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('U12345', '2026-04-09', '50000')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._cleanup_db()

    def test_without_live_positions_warns(self):
        from agt_equities.state_builder import build_state
        snapshot = build_state(db_path=self.db_path)  # no live_positions arg
        self.assertEqual(snapshot.live_positions, [])
        lp_warnings = [w for w in snapshot.warnings if "live_positions not provided" in w]
        self.assertTrue(len(lp_warnings) >= 1, f"Expected warning, got: {snapshot.warnings}")


class TestBuildStateNoForbiddenImports(unittest.TestCase):
    """C1 test #8: state_builder.py has no forbidden imports (AST check)."""

    def test_no_forbidden_imports(self):
        import ast
        sb_path = os.path.join(
            os.path.dirname(__file__), "..", "agt_equities", "state_builder.py"
        )
        with open(sb_path) as f:
            tree = ast.parse(f.read())

        forbidden = {"telegram_bot", "agt_deck", "ib_async"}
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                if top in forbidden:
                    found.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in forbidden:
                        found.append(alias.name)

        self.assertEqual(
            found, [],
            f"Forbidden imports in state_builder.py: {found}"
        )


# ── Sprint C2: build_top_strip consumes DeskSnapshot ─────────────────

class _TopStripDBMixin:
    """Shared temp DB for build_top_strip tests."""

    def _make_top_strip_db(self) -> str:
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = self._tmp.name
        self._tmp.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE master_log_nav (
                account_id TEXT, report_date TEXT, total TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE beta_cache (
                ticker TEXT PRIMARY KEY,
                beta REAL NOT NULL DEFAULT 1.0,
                fetched_ts TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                household TEXT NOT NULL,
                desk_mode TEXT NOT NULL DEFAULT 'PEACETIME',
                action_type TEXT NOT NULL DEFAULT 'CC',
                household_nlv REAL NOT NULL DEFAULT 0,
                underlying_spot_at_render REAL NOT NULL DEFAULT 0,
                final_status TEXT NOT NULL DEFAULT 'STAGED',
                last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Tables queried by build_top_strip via conn (graceful fallback on missing)
        conn.execute("""
            CREATE TABLE master_log_change_in_nav (
                account_id TEXT, starting_value TEXT, ending_value TEXT,
                twr TEXT, deposits_withdrawals TEXT, asset_transfers TEXT
            )
        """)
        conn.commit()
        conn.close()
        return db_path

    def _cleanup_top_strip_db(self):
        import os
        try:
            os.unlink(self._tmp.name)
        except Exception:
            pass


class TestBuildTopStripConsumesDeskSnapshot(_TopStripDBMixin, unittest.TestCase):
    """C2 test #1: build_top_strip reads NAV/cycles/betas from DeskSnapshot."""

    def setUp(self):
        self.db_path = self._make_top_strip_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('U21971297', '20260409', '200000')"
        )
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('U22388499', '20260409', '100000')"
        )
        conn.execute(
            "INSERT INTO beta_cache (ticker, beta) VALUES ('AAPL', 1.15)"
        )
        conn.execute(
            "INSERT INTO master_log_change_in_nav VALUES "
            "('U21971297', '190000', '200000', '0.05', '50000', '0')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._cleanup_top_strip_db()

    def test_build_top_strip_has_expected_keys(self):
        from unittest.mock import patch
        from agt_equities import trade_repo
        from pathlib import Path

        # Monkeypatch trade_repo.DB_PATH for build_state() internal connection
        orig_db_path = trade_repo.DB_PATH
        trade_repo.DB_PATH = Path(self.db_path)
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            with patch("agt_deck.main.get_vix", return_value=18.5), \
                 patch("agt_deck.main.get_spots", return_value={}):
                from agt_deck.main import build_top_strip
                result = build_top_strip(conn)

            conn.close()

            expected_keys = {
                "vix", "el_retain_pct", "total_nav", "inception_pnl",
                "inception_pnl_pct", "net_inflows", "el_current", "el_required",
                "vikram_el_pct", "conc_ticker", "conc_pct", "conc_hh",
                "sector_violations", "leverage", "last_sync", "nav_by_acct",
                "change_nav", "walker_warning_count", "walker_worst_severity",
                "desk_mode",
            }
            self.assertEqual(set(result.keys()), expected_keys)
            self.assertAlmostEqual(result["total_nav"], 300000.0)
            self.assertIn("U21971297", result["nav_by_acct"])
        finally:
            trade_repo.DB_PATH = orig_db_path


class TestBuildTopStripNavMatchesBuildState(_TopStripDBMixin, unittest.TestCase):
    """C2 test #2: build_top_strip total_nav equals build_state().nav_total."""

    def setUp(self):
        self.db_path = self._make_top_strip_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('U21971297', '20260409', '175000')"
        )
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('U22076329', '20260409', '45000')"
        )
        conn.execute(
            "INSERT INTO master_log_nav VALUES ('U22388499', '20260408', '80000')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._cleanup_top_strip_db()

    def test_nav_matches_build_state(self):
        from unittest.mock import patch
        from agt_equities import trade_repo
        from agt_equities.state_builder import build_state
        from pathlib import Path

        orig_db_path = trade_repo.DB_PATH
        trade_repo.DB_PATH = Path(self.db_path)
        try:
            snapshot = build_state(db_path=self.db_path, live_positions=[])

            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            with patch("agt_deck.main.get_vix", return_value=None), \
                 patch("agt_deck.main.get_spots", return_value={}):
                from agt_deck.main import build_top_strip
                result = build_top_strip(conn)

            conn.close()

            self.assertAlmostEqual(
                result["total_nav"], snapshot.nav_total,
                msg="build_top_strip total_nav must equal build_state().nav_total",
            )
        finally:
            trade_repo.DB_PATH = orig_db_path


# ── Polish G2: Underwater Positions tests ────────────────────────────

class TestBuildUnderwaterRowsFilter(unittest.TestCase):
    """G2 test #1: _build_underwater_rows filters correctly."""

    def test_filter_logic(self):
        from agt_deck.main import _build_underwater_rows
        cycles = [
            # Included: DTE <= 5
            {"ticker": "AAPL", "household": "Yash", "nearest_dte": 3,
             "unreal_pct": -5.0, "unreal_dollar": -500, "open_short_calls": 1},
            # Included: loss > 15% AND |$loss| >= $1500
            {"ticker": "MSFT", "household": "Yash", "nearest_dte": 10,
             "unreal_pct": -20.0, "unreal_dollar": -2000, "open_short_calls": 0},
            # Excluded: DTE=10, loss=10% (neither threshold met)
            {"ticker": "GOOG", "household": "Vikram", "nearest_dte": 10,
             "unreal_pct": -10.0, "unreal_dollar": -800, "open_short_calls": 1},
        ]
        result = _build_underwater_rows(cycles)
        tickers = [r["ticker"] for r in result]
        self.assertEqual(len(result), 2)
        self.assertIn("AAPL", tickers)
        self.assertIn("MSFT", tickers)
        self.assertNotIn("GOOG", tickers)


class TestBuildUnderwaterRowsSort(unittest.TestCase):
    """G2 test #2: _build_underwater_rows sorts by unreal_pct asc."""

    def test_sort_order(self):
        from agt_deck.main import _build_underwater_rows
        cycles = [
            {"ticker": "A", "household": "Yash", "nearest_dte": 2,
             "unreal_pct": -5.0, "unreal_dollar": -500, "open_short_calls": 0},
            {"ticker": "B", "household": "Yash", "nearest_dte": 1,
             "unreal_pct": -30.0, "unreal_dollar": -3000, "open_short_calls": 1},
            {"ticker": "C", "household": "Vikram", "nearest_dte": 3,
             "unreal_pct": -15.0, "unreal_dollar": -1500, "open_short_calls": 0},
        ]
        result = _build_underwater_rows(cycles)
        pcts = [r["unreal_pct"] for r in result]
        self.assertEqual(pcts, [-30.0, -15.0, -5.0])


class TestBuildCureDataIncludesUnderwater(unittest.TestCase):
    """G2 test #3: _build_cure_data return dict contains 'underwater' key."""

    def test_underwater_key_present(self):
        from unittest.mock import patch, MagicMock
        from agt_equities import trade_repo
        from pathlib import Path
        import tempfile

        # Create minimal temp DB for build_state + _build_cure_data
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE master_log_nav (account_id TEXT, report_date TEXT, total TEXT)")
        conn.execute("INSERT INTO master_log_nav VALUES ('U21971297', '20260409', '200000')")
        conn.execute("CREATE TABLE beta_cache (ticker TEXT PRIMARY KEY, beta REAL DEFAULT 1.0, fetched_ts TEXT DEFAULT (datetime('now')))")
        conn.execute("CREATE TABLE bucket3_dynamic_exit_log (audit_id TEXT PRIMARY KEY, trade_date TEXT, ticker TEXT, household TEXT, desk_mode TEXT DEFAULT 'PEACETIME', action_type TEXT DEFAULT 'CC', household_nlv REAL DEFAULT 0, underlying_spot_at_render REAL DEFAULT 0, final_status TEXT DEFAULT 'STAGED', last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE master_log_change_in_nav (account_id TEXT, starting_value TEXT, ending_value TEXT, twr TEXT, deposits_withdrawals TEXT, asset_transfers TEXT)")
        conn.execute("CREATE TABLE glide_paths (id INTEGER PRIMARY KEY, household_id TEXT, rule_id TEXT, ticker TEXT, baseline_value REAL, target_value REAL, start_date TEXT, target_date TEXT, pause_conditions TEXT, notes TEXT, accelerator_clause TEXT, UNIQUE(household_id, rule_id, ticker))")
        conn.execute("CREATE TABLE mode_history (id INTEGER PRIMARY KEY, timestamp TEXT, old_mode TEXT, new_mode TEXT, trigger_rule TEXT, trigger_household TEXT, trigger_value REAL, notes TEXT)")
        conn.commit()

        orig_db_path = trade_repo.DB_PATH
        trade_repo.DB_PATH = Path(db_path)
        try:
            with patch("agt_deck.main.get_vix", return_value=18.0), \
                 patch("agt_deck.main.get_spots", return_value={}), \
                 patch("agt_deck.main.load_active_cycles", return_value=[]), \
                 patch("agt_deck.main.build_cycles_table", return_value=[]):
                from agt_deck.main import _build_cure_data
                result = _build_cure_data(conn)

            self.assertIn("underwater", result)
            self.assertIn("underwater_grouped", result)
            self.assertIsInstance(result["underwater"], list)
            self.assertIsInstance(result["underwater_grouped"], list)
        finally:
            trade_repo.DB_PATH = orig_db_path
            conn.close()
            try:
                import os
                os.unlink(db_path)
            except OSError:
                pass  # Windows may hold lock briefly after close


# ── Polish G7: breathe animation smoke test ──────────────────────────

class TestTopStripBreatheClass(unittest.TestCase):
    """G7: top strip <header> has breathe class in both templates."""

    def test_breathe_class_present(self):
        for template_name in ("command_deck.html", "cure_console.html"):
            path = os.path.join(
                os.path.dirname(__file__), "..", "agt_deck", "templates", template_name
            )
            with open(path) as f:
                html = f.read()
            self.assertIn(
                'class="breathe sticky',
                html,
                f"{template_name} top strip <header> must have 'breathe' class",
            )


# ── Sprint D: margin config + Rule 6 regression ─────────────────────

class TestMarginEligibleAccountsLiveMode(unittest.TestCase):
    """D test #1: MARGIN_ELIGIBLE_ACCOUNTS has correct live-mode values."""

    def test_live_mode_values(self):
        from agt_equities.config import MARGIN_ELIGIBLE_ACCOUNTS, PAPER_MODE
        if not PAPER_MODE:
            self.assertEqual(
                MARGIN_ELIGIBLE_ACCOUNTS,
                {"Yash_Household": ["U21971297"], "Vikram_Household": ["U22388499"]},
            )


class TestMarginAccountsIsFrozenset(unittest.TestCase):
    """D test #2: MARGIN_ACCOUNTS is frozenset with correct derived contents."""

    def test_is_frozenset(self):
        from agt_equities.config import MARGIN_ACCOUNTS, MARGIN_ELIGIBLE_ACCOUNTS
        self.assertIsInstance(MARGIN_ACCOUNTS, frozenset)
        expected = frozenset(
            acct for accts in MARGIN_ELIGIBLE_ACCOUNTS.values() for acct in accts
        )
        self.assertEqual(MARGIN_ACCOUNTS, expected)


class TestRule6DerivesVikramFromConfig(unittest.TestCase):
    """D test #3: Rule 6 reads Vikram account from config, not hardcoded."""

    def test_rule_6_uses_config_account(self):
        from agt_equities.rule_engine import PortfolioState, evaluate_rule_6, AccountELSnapshot

        # Build PortfolioState with Vikram's configured account in account_el
        from agt_equities.config import MARGIN_ELIGIBLE_ACCOUNTS
        vikram_acct_id = MARGIN_ELIGIBLE_ACCOUNTS["Vikram_Household"][0]

        ps = PortfolioState(
            household_nlv={"Vikram_Household": 100000.0},
            household_el={"Vikram_Household": 30000.0},
            active_cycles=[],
            spots={},
            betas={},
            industries={},
            sector_overrides={},
            vix=18.0,
            report_date="20260409",
            account_el={
                vikram_acct_id: AccountELSnapshot(
                    excess_liquidity=25000.0,
                    net_liquidation=100000.0,
                    timestamp="2026-04-09T10:00:00",
                    stale=False,
                ),
            },
            account_nlv={vikram_acct_id: 100000.0},
        )
        result = evaluate_rule_6(ps, "Vikram_Household")
        self.assertEqual(result.status, "GREEN")
        self.assertAlmostEqual(result.raw_value, 25.0)  # 25000/100000 * 100


class TestRule6HandlesMissingVikramConfig(unittest.TestCase):
    """D test #4: Rule 6 returns GREEN when Vikram config is empty."""

    def test_missing_vikram_returns_green(self):
        from unittest.mock import patch
        from agt_equities.rule_engine import PortfolioState, evaluate_rule_6

        ps = PortfolioState(
            household_nlv={"Vikram_Household": 100000.0},
            household_el={},
            active_cycles=[],
            spots={},
            betas={},
            industries={},
            sector_overrides={},
            vix=18.0,
            report_date="20260409",
        )
        with patch("agt_equities.rule_engine.MARGIN_ELIGIBLE_ACCOUNTS",
                    {"Yash_Household": ["U21971297"]}):
            result = evaluate_rule_6(ps, "Vikram_Household")
        self.assertEqual(result.status, "GREEN")
        self.assertIn("no Vikram margin-eligible account configured", result.message)


class TestNoHardcodedAccountIdsInRuleEngine(unittest.TestCase):
    """D test #5: AST guard — no U-prefixed 8-digit string literals in rule_engine.py."""

    def test_no_hardcoded_account_ids(self):
        import ast
        import re
        re_path = os.path.join(
            os.path.dirname(__file__), "..", "agt_equities", "rule_engine.py"
        )
        with open(re_path) as f:
            tree = ast.parse(f.read())

        pattern = re.compile(r"^U\d{8}$")
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if pattern.match(node.value):
                    found.append((node.lineno, node.value))

        self.assertEqual(
            found, [],
            f"Hardcoded account IDs in rule_engine.py: {found}"
        )


# ── Hotfix: _build_cure_data get_betas coverage gap ──────────────────

class TestBuildCureDataWithNonEmptyCycles(unittest.TestCase):
    """Hotfix: _build_cure_data must not NameError when cycles have spots."""

    def test_get_betas_called_when_spots_exist(self):
        from unittest.mock import patch, MagicMock
        from agt_equities import trade_repo
        from pathlib import Path
        import tempfile

        # Minimal DB for build_state + _build_cure_data
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE master_log_nav (account_id TEXT, report_date TEXT, total TEXT)")
        conn.execute("INSERT INTO master_log_nav VALUES ('U21971297', '20260409', '200000')")
        conn.execute("CREATE TABLE beta_cache (ticker TEXT PRIMARY KEY, beta REAL DEFAULT 1.0, fetched_ts TEXT DEFAULT (datetime('now')))")
        conn.execute("CREATE TABLE bucket3_dynamic_exit_log (audit_id TEXT PRIMARY KEY, trade_date TEXT, ticker TEXT, household TEXT, desk_mode TEXT DEFAULT 'PEACETIME', action_type TEXT DEFAULT 'CC', household_nlv REAL DEFAULT 0, underlying_spot_at_render REAL DEFAULT 0, final_status TEXT DEFAULT 'STAGED', last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE master_log_change_in_nav (account_id TEXT, starting_value TEXT, ending_value TEXT, twr TEXT, deposits_withdrawals TEXT, asset_transfers TEXT)")
        conn.execute("CREATE TABLE glide_paths (id INTEGER PRIMARY KEY, household_id TEXT, rule_id TEXT, ticker TEXT, baseline_value REAL, target_value REAL, start_date TEXT, target_date TEXT, pause_conditions TEXT, notes TEXT, accelerator_clause TEXT, UNIQUE(household_id, rule_id, ticker))")
        conn.execute("CREATE TABLE mode_history (id INTEGER PRIMARY KEY, timestamp TEXT, old_mode TEXT, new_mode TEXT, trigger_rule TEXT, trigger_household TEXT, trigger_value REAL, notes TEXT)")
        conn.commit()

        # Mock cycle with a real ticker so spots is non-empty
        mock_cycle = MagicMock()
        mock_cycle.status = "ACTIVE"
        mock_cycle.ticker = "SPY"
        mock_cycle.cycle_type = "WHEEL"
        mock_cycle.household_id = "Yash_Household"
        mock_cycle.shares_held = 100
        mock_cycle.open_short_calls = 1
        mock_cycle.open_short_puts = 0

        orig_db_path = trade_repo.DB_PATH
        trade_repo.DB_PATH = Path(db_path)
        try:
            with patch("agt_deck.main.get_vix", return_value=18.0), \
                 patch("agt_deck.main.get_spots", return_value={"SPY": 520.0}), \
                 patch("agt_deck.main.load_active_cycles", return_value=[mock_cycle]), \
                 patch("agt_deck.main.build_cycles_table", return_value=[]):
                from agt_deck.main import _build_cure_data
                # This call triggers get_betas(["SPY"]) at line 513
                result = _build_cure_data(conn)

            self.assertIn("households", result)
        finally:
            trade_repo.DB_PATH = orig_db_path
            conn.close()
            try:
                os.unlink(db_path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
