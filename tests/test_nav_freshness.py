"""#43 NAV Freshness — prefer live IB NLV from el_snapshots when fresh.

3 tests:
  1. Live NAV preferred when fresh (<120s)
  2. Flex fallback when stale (>120s)
  3. Flex fallback when no snapshot exists
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Minimal DDL for build_state dependencies
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE master_log_nav (
    report_date TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    total       REAL,
    last_synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (report_date, account_id)
);

CREATE TABLE el_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    household   TEXT NOT NULL,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
    excess_liquidity REAL,
    nlv         REAL,
    buying_power REAL,
    source      TEXT NOT NULL DEFAULT 'ibkr_live',
    account_id  TEXT,
    client_id   TEXT DEFAULT 'AGT'
);

CREATE TABLE bucket3_dynamic_exit_log (
    audit_id     TEXT PRIMARY KEY,
    household    TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    final_status TEXT NOT NULL DEFAULT 'PENDING'
);

CREATE TABLE beta_cache (
    ticker TEXT PRIMARY KEY,
    beta   REAL NOT NULL
);
"""


@dataclass
class _StubCycle:
    ticker: str = "ADBE"
    household_id: str = "Yash_Household"
    status: str = "ACTIVE"
    shares_held: int = 100
    cost_basis: float = 300.0
    current_cc_contracts: int = 0
    account_id: str = "U21971297"


def _create_test_db(db_path: str):
    """Create a minimal DB with required tables for build_state."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_DDL)
    conn.close()


def _seed_flex_nav(db_path: str, account_id: str, nav: float):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO master_log_nav (report_date, account_id, total, last_synced_at) "
        "VALUES ('2026-04-08', ?, ?, datetime('now'))",
        (account_id, nav),
    )
    conn.commit()
    conn.close()


def _seed_el_snapshot(db_path: str, account_id: str, household: str,
                      nlv: float, seconds_ago: int):
    """Insert an el_snapshot with a timestamp `seconds_ago` seconds in the past."""
    ts = (datetime.utcnow() - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO el_snapshots (household, timestamp, nlv, account_id) "
        "VALUES (?, ?, ?, ?)",
        (household, ts, nlv, account_id),
    )
    conn.commit()
    conn.close()


class TestNavFreshness(unittest.TestCase):
    """#43: build_state() prefers live NLV from el_snapshots when fresh."""

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False,
        )
        self._db_path = self._tmpfile.name
        self._tmpfile.close()
        _create_test_db(self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def _build(self, live_nlv=None):
        """Call build_state with mocked trade_repo."""
        from agt_equities.state_builder import build_state

        mock_trade_repo = MagicMock()
        mock_trade_repo.DB_PATH = self._db_path
        mock_trade_repo.get_active_cycles.return_value = [_StubCycle()]

        with patch.dict(
            "sys.modules",
            {"agt_equities.trade_repo": mock_trade_repo, "agt_equities": MagicMock()},
        ):
            # Patch the deferred import inside build_state
            with patch("agt_equities.state_builder.trade_repo", mock_trade_repo, create=True):
                return build_state(db_path=self._db_path, live_nlv=live_nlv)

    def test_live_nav_preferred_when_fresh(self):
        """el_snapshot <120s old → live NLV wins over Flex."""
        _seed_flex_nav(self._db_path, "U21971297", 100_000.0)
        _seed_el_snapshot(
            self._db_path, "U21971297", "Yash_Household",
            nlv=95_000.0, seconds_ago=30,
        )

        snap = self._build()

        self.assertAlmostEqual(snap.nav_by_account["U21971297"], 95_000.0)
        self.assertEqual(snap.nav_source_by_account["U21971297"], "live_db")

    def test_flex_fallback_when_stale(self):
        """el_snapshot >120s old → Flex EOD wins."""
        _seed_flex_nav(self._db_path, "U21971297", 100_000.0)
        _seed_el_snapshot(
            self._db_path, "U21971297", "Yash_Household",
            nlv=95_000.0, seconds_ago=600,  # 10 minutes old
        )

        snap = self._build()

        self.assertAlmostEqual(snap.nav_by_account["U21971297"], 100_000.0)
        self.assertEqual(snap.nav_source_by_account["U21971297"], "flex_eod")

    def test_flex_fallback_when_no_snapshot(self):
        """No el_snapshot at all → Flex EOD wins."""
        _seed_flex_nav(self._db_path, "U21971297", 100_000.0)
        # No el_snapshot seeded

        snap = self._build()

        self.assertAlmostEqual(snap.nav_by_account["U21971297"], 100_000.0)
        self.assertEqual(snap.nav_source_by_account["U21971297"], "flex_eod")


    def test_live_injected_nlv_wins_over_db(self):
        """live_nlv param (Tier 1) wins over el_snapshots (Tier 2) and Flex (Tier 3)."""
        _seed_flex_nav(self._db_path, "U21971297", 100_000.0)
        _seed_el_snapshot(
            self._db_path, "U21971297", "Yash_Household",
            nlv=95_000.0, seconds_ago=30,
        )

        snap = self._build(live_nlv={"U21971297": 92_000.0})

        self.assertAlmostEqual(snap.nav_by_account["U21971297"], 92_000.0)
        self.assertEqual(snap.nav_source_by_account["U21971297"], "live_injected")

    def test_live_nlv_partial_injection(self):
        """live_nlv for one account; other account falls through to Tier 2/3."""
        _seed_flex_nav(self._db_path, "U21971297", 100_000.0)
        _seed_flex_nav(self._db_path, "U22388499", 80_000.0)
        _seed_el_snapshot(
            self._db_path, "U22388499", "Vikram_Household",
            nlv=78_000.0, seconds_ago=30,
        )
        # Only inject live_nlv for U21971297
        snap = self._build(live_nlv={"U21971297": 92_000.0})

        self.assertAlmostEqual(snap.nav_by_account["U21971297"], 92_000.0)
        self.assertEqual(snap.nav_source_by_account["U21971297"], "live_injected")
        # U22388499 has fresh el_snapshot → Tier 2
        self.assertAlmostEqual(snap.nav_by_account["U22388499"], 78_000.0)
        self.assertEqual(snap.nav_source_by_account["U22388499"], "live_db")


if __name__ == "__main__":
    unittest.main()
