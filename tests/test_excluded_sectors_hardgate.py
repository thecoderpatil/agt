"""Hard-exclude EXCLUDED_SECTORS from the CSP candidate pipeline.

Two layers:
  1. pxo_scanner._load_scan_universe filters at load time.
  2. csp_allocator._csp_check_rule_3b fails closed on any candidate
     whose sector is in EXCLUDED_SECTORS.

Regression guard for 2026-04-17 09:35 scan where MRNA + 12 other
biotech/airline tickers leaked into the candidate pool.
"""
from __future__ import annotations

import pytest
from dataclasses import dataclass

from agt_equities.screener.config import EXCLUDED_SECTORS
from agt_equities.csp_allocator import _csp_check_rule_3b


pytestmark = [pytest.mark.sprint_a, pytest.mark.csp_allocator]


# -- Layer 1: _load_scan_universe filter --------------------------------------

def test_load_scan_universe_drops_excluded_industry_groups(tmp_path, monkeypatch):
    """ticker_universe rows whose gics_industry_group is in EXCLUDED_SECTORS
    must not appear in the scanner's universe output."""
    import sqlite3
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE ticker_universe (
                ticker TEXT PRIMARY KEY,
                company_name TEXT,
                gics_sector TEXT,
                gics_industry_group TEXT,
                index_membership TEXT,
                has_weekly_options INTEGER,
                avg_volume_30d REAL,
                market_cap REAL,
                last_updated TEXT,
                conviction_tier TEXT
            )
        """)
        conn.executemany(
            "INSERT INTO ticker_universe(ticker, company_name, gics_sector, "
            "gics_industry_group) VALUES (?, ?, ?, ?)",
            [
                ("MRNA", "Moderna",   "Healthcare",  "Biotechnology"),
                ("DAL",  "Delta",     "Industrials", "Airlines"),
                ("REGN", "Regeneron", "Healthcare",  "Biotechnology"),
                ("AAPL", "Apple",     "Technology",  "Technology Hardware"),
                ("MSFT", "Microsoft", "Technology",  "Software"),
            ],
        )
        conn.commit()

    import pxo_scanner
    monkeypatch.setattr(pxo_scanner, "_DB_PATH", db)
    universe = pxo_scanner._load_scan_universe()
    tickers = {e["ticker"] for e in universe}
    assert "MRNA" not in tickers
    assert "DAL" not in tickers
    assert "REGN" not in tickers
    assert "AAPL" in tickers
    assert "MSFT" in tickers


def test_load_scan_universe_filter_is_case_insensitive(tmp_path, monkeypatch):
    """Lowercase 'biotechnology' must still be filtered."""
    import sqlite3
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE ticker_universe (
                ticker TEXT PRIMARY KEY, company_name TEXT,
                gics_sector TEXT, gics_industry_group TEXT,
                index_membership TEXT, has_weekly_options INTEGER,
                avg_volume_30d REAL, market_cap REAL,
                last_updated TEXT, conviction_tier TEXT
            )
        """)
        conn.executemany(
            "INSERT INTO ticker_universe(ticker, gics_industry_group) "
            "VALUES (?, ?)",
            [("LOWER", "biotechnology"), ("UPPER", "BIOTECHNOLOGY"),
             ("OK",    "Technology")],
        )
        conn.commit()

    import pxo_scanner
    monkeypatch.setattr(pxo_scanner, "_DB_PATH", db)
    tickers = {e["ticker"] for e in pxo_scanner._load_scan_universe()}
    assert "LOWER" not in tickers
    assert "UPPER" not in tickers
    assert "OK" in tickers


# -- Layer 2: _csp_check_rule_3b allocator gate -------------------------------

@dataclass
class _FakeCandidate:
    ticker: str


def test_rule_3b_rejects_biotechnology():
    cand = _FakeCandidate(ticker="MRNA")
    extras = {"sector_map": {"MRNA": "Biotechnology"}}
    ok, reason = _csp_check_rule_3b({}, cand, 1, 20.0, extras)
    assert ok is False
    assert "rule_3b" in reason
    assert "Biotechnology" in reason


def test_rule_3b_rejects_airlines_case_insensitive():
    cand = _FakeCandidate(ticker="DAL")
    extras = {"sector_map": {"DAL": "airlines"}}  # lowercase
    ok, reason = _csp_check_rule_3b({}, cand, 1, 20.0, extras)
    assert ok is False
    assert "rule_3b" in reason


def test_rule_3b_passes_non_excluded_sector():
    cand = _FakeCandidate(ticker="AAPL")
    extras = {"sector_map": {"AAPL": "Technology Hardware"}}
    ok, reason = _csp_check_rule_3b({}, cand, 1, 20.0, extras)
    assert ok is True
    assert reason == ""
