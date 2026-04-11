"""
tests/test_screener_phase1.py

Unit tests for agt_equities.screener.universe (Phase 1: Finnhub Free
profile2 + universe exclusions).

Mocking strategy: inject httpx.MockTransport into the FinnhubClient so
no network calls happen. Each test asserts a specific gate or behavior:

  Universe seed loader (3):
    1. load_seed_tickers() returns sorted, deduplicated symbols
    2. load_seed_tickers() raises FileNotFoundError on missing CSV
    3. Default seed CSV has 517 tickers (smoke check against the shipped file)

  Pure filter helpers (6):
    4. _passes_market_cap: above/at/below the $10B threshold
    5. _passes_sector: explicit excluded match
    6. _passes_sector: case-insensitive excluded match
    7. _passes_sector: empty/missing fails closed
    8. _passes_country: ISO code AND long name both excluded
    9. _passes_country: empty/missing fails closed

  _profile_to_universe_ticker (4):
   10. Happy path: large-cap US tech ticker survives
   11. Below MC threshold: dropped
   12. Excluded sector: dropped
   13. Empty profile dict: dropped (None return)

  run_phase_1 orchestrator (4):
   14. Happy path: 3 tickers, 2 pass exclusions, 1 dropped
   15. Heartbeat callback fires every N tickers
   16. Per-ticker exception in client doesn't kill the run
   17. Empty seed list returns empty survivor list
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from agt_equities.screener import cache as screener_cache
from agt_equities.screener import universe
from agt_equities.screener.finnhub_client import FinnhubClient
from agt_equities.screener.types import UniverseTicker


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Redirect screener cache root to a tmp_path."""
    monkeypatch.setattr(screener_cache, "CACHE_ROOT", tmp_path / "screener_cache")
    return tmp_path / "screener_cache"


# ---------------------------------------------------------------------------
# 1-3. Universe seed loader
# ---------------------------------------------------------------------------

def test_1_load_seed_tickers_sorted_unique(tmp_path):
    csv = tmp_path / "test_universe.csv"
    csv.write_text(
        "ticker,name,sector,source\n"
        "MSFT,Microsoft,Tech,SP500\n"
        "AAPL,Apple,Tech,SP500+NDX\n"
        "MSFT,Microsoft,Tech,SP500\n"  # duplicate
        "GOOGL,Alphabet,Comm,SP500+NDX\n",
        encoding="utf-8",
    )
    result = universe.load_seed_tickers(csv)
    assert result == ["AAPL", "GOOGL", "MSFT"]


def test_2_load_seed_tickers_missing_file_raises(tmp_path):
    missing = tmp_path / "does_not_exist.csv"
    with pytest.raises(FileNotFoundError):
        universe.load_seed_tickers(missing)


def test_3_default_seed_csv_present_and_sized():
    """Smoke check: the shipped sp500_nasdaq100.csv loads with the
    expected ticker count band (matches test_screener_universe_csv_present)."""
    tickers = universe.load_seed_tickers()
    assert 480 <= len(tickers) <= 560
    assert "AAPL" in tickers
    assert "MSFT" in tickers


# ---------------------------------------------------------------------------
# 4. Market cap gate
# ---------------------------------------------------------------------------

def test_4_market_cap_gate():
    assert universe._passes_market_cap(10_000_000_000.0) is True   # at threshold
    assert universe._passes_market_cap(50_000_000_000.0) is True   # above
    assert universe._passes_market_cap(9_999_999_999.0) is False   # 1c below
    assert universe._passes_market_cap(0.0) is False
    assert universe._passes_market_cap(-1.0) is False


# ---------------------------------------------------------------------------
# 5-7. Sector gate
# ---------------------------------------------------------------------------

def test_5_sector_excluded_literal():
    assert universe._passes_sector("Airlines") is False
    assert universe._passes_sector("Biotechnology") is False
    assert universe._passes_sector("Pharmaceuticals") is False
    assert universe._passes_sector("Technology") is True
    assert universe._passes_sector("Consumer Discretionary") is True


def test_6_sector_excluded_case_insensitive():
    assert universe._passes_sector("airlines") is False
    assert universe._passes_sector("AIRLINES") is False
    assert universe._passes_sector("biotechnology") is False
    assert universe._passes_sector("PharmaceuticalS") is False


def test_7_sector_empty_fails_closed():
    assert universe._passes_sector("") is False
    assert universe._passes_sector("   ") is False


# ---------------------------------------------------------------------------
# 8-9. Country gate
# ---------------------------------------------------------------------------

def test_8_country_excluded_iso_and_name():
    # ISO codes
    assert universe._passes_country("CN") is False
    assert universe._passes_country("HK") is False
    assert universe._passes_country("MO") is False
    # Long names
    assert universe._passes_country("China") is False
    assert universe._passes_country("Hong Kong") is False
    assert universe._passes_country("Macau") is False
    # US passes
    assert universe._passes_country("US") is True
    assert universe._passes_country("United States") is True


def test_9_country_empty_fails_closed():
    assert universe._passes_country("") is False
    assert universe._passes_country("   ") is False


# ---------------------------------------------------------------------------
# 10-13. _profile_to_universe_ticker
# ---------------------------------------------------------------------------

def test_10_profile_to_ticker_happy_path():
    profile = {
        "ticker": "AAPL",
        "name": "Apple Inc",
        "country": "US",
        "marketCapitalization": 3500000.0,  # $3.5T in millions
        "finnhubIndustry": "Technology",
    }
    result = universe._profile_to_universe_ticker("AAPL", profile)
    assert result is not None
    assert result.ticker == "AAPL"
    assert result.name == "Apple Inc"
    assert result.sector == "Technology"
    assert result.country == "US"
    assert result.market_cap_usd == 3_500_000_000_000.0


def test_11_profile_below_mc_dropped():
    profile = {
        "ticker": "TINY",
        "name": "Tiny Corp",
        "country": "US",
        "marketCapitalization": 5000.0,  # $5B — below $10B threshold
        "finnhubIndustry": "Technology",
    }
    assert universe._profile_to_universe_ticker("TINY", profile) is None


def test_12_profile_excluded_sector_dropped():
    profile = {
        "ticker": "BIGBIO",
        "name": "Big Biotech",
        "country": "US",
        "marketCapitalization": 50000.0,  # $50B
        "finnhubIndustry": "Biotechnology",
    }
    assert universe._profile_to_universe_ticker("BIGBIO", profile) is None


def test_13_profile_empty_dict_dropped():
    assert universe._profile_to_universe_ticker("EMPTY", {}) is None
    assert universe._profile_to_universe_ticker("NULL", None) is None


# ---------------------------------------------------------------------------
# 14-17. run_phase_1 orchestrator
# ---------------------------------------------------------------------------

def _build_profile_handler(profiles_by_ticker: dict[str, dict | None]):
    """Build an httpx.MockTransport handler that returns canned profile2
    responses keyed by ticker. None means 'return empty response'.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.params.get("symbol", "")
        if ticker in profiles_by_ticker:
            payload = profiles_by_ticker[ticker]
            if payload is None:
                return httpx.Response(200, json={})
            return httpx.Response(200, json=payload)
        return httpx.Response(404)
    return handler


def test_14_run_phase_1_filters_correctly(tmp_cache):
    """Three tickers: 1 large-cap tech (pass), 1 small-cap (drop),
    1 excluded sector (drop). Expect 1 survivor."""
    handler = _build_profile_handler({
        "AAPL": {
            "name": "Apple Inc", "country": "US",
            "marketCapitalization": 3500000.0,
            "finnhubIndustry": "Technology",
        },
        "TINY": {
            "name": "Tiny Co", "country": "US",
            "marketCapitalization": 5000.0,
            "finnhubIndustry": "Technology",
        },
        "BIGBIO": {
            "name": "Big Biotech", "country": "US",
            "marketCapitalization": 50000.0,
            "finnhubIndustry": "Biotechnology",
        },
    })

    async def run():
        client = FinnhubClient(api_key="test_key", transport=httpx.MockTransport(handler))
        try:
            survivors = await universe.run_phase_1(
                client, tickers=["AAPL", "TINY", "BIGBIO"],
                heartbeat_interval=0,
            )
            assert len(survivors) == 1
            assert survivors[0].ticker == "AAPL"
            assert isinstance(survivors[0], UniverseTicker)
        finally:
            await client.aclose()
    _run(run())


def test_15_run_phase_1_heartbeat_logs(tmp_cache, caplog):
    """Heartbeat should fire every N tickers per the heartbeat_interval."""
    import logging
    handler = _build_profile_handler({
        f"T{i:03d}": {
            "name": f"Test {i}", "country": "US",
            "marketCapitalization": 50000.0,  # $50B passes
            "finnhubIndustry": "Technology",
        }
        for i in range(10)
    })

    async def run():
        client = FinnhubClient(api_key="test_key", transport=httpx.MockTransport(handler))
        try:
            with caplog.at_level(logging.INFO, logger="agt_equities.screener.universe"):
                tickers = [f"T{i:03d}" for i in range(10)]
                await universe.run_phase_1(client, tickers=tickers, heartbeat_interval=5)
            heartbeat_msgs = [r.message for r in caplog.records if "heartbeat" in r.message]
            # 10 tickers, interval=5 → expect 2 heartbeats (at 5 and 10)
            assert len(heartbeat_msgs) == 2
        finally:
            await client.aclose()
    _run(run())


def test_16_run_phase_1_per_ticker_exception_does_not_crash(tmp_cache):
    """If FinnhubClient.get_profile2 raises for one ticker, the run continues."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        ticker = request.url.params.get("symbol", "")
        if ticker == "BAD":
            # Simulate a transport-level failure: return malformed JSON
            return httpx.Response(200, content=b"not json at all")
        return httpx.Response(200, json={
            "name": ticker, "country": "US",
            "marketCapitalization": 50000.0,
            "finnhubIndustry": "Technology",
        })

    async def run():
        client = FinnhubClient(api_key="test_key", transport=httpx.MockTransport(handler))
        try:
            survivors = await universe.run_phase_1(
                client, tickers=["GOOD1", "BAD", "GOOD2"],
                heartbeat_interval=0,
            )
            # GOOD1 and GOOD2 should pass; BAD returns None and is dropped
            tickers = sorted(s.ticker for s in survivors)
            assert tickers == ["GOOD1", "GOOD2"]
        finally:
            await client.aclose()
    _run(run())


def test_17_run_phase_1_empty_input(tmp_cache):
    handler = _build_profile_handler({})

    async def run():
        client = FinnhubClient(api_key="test_key", transport=httpx.MockTransport(handler))
        try:
            survivors = await universe.run_phase_1(client, tickers=[], heartbeat_interval=0)
            assert survivors == []
        finally:
            await client.aclose()
    _run(run())


# ---------------------------------------------------------------------------
# C2.1 — Cache hit rate instrumentation in the final log line
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# C3.6 — Structural exclusion tests (REITs, MLPs, BDCs, SPACs, trusts)
# ---------------------------------------------------------------------------
#
# Phase 1 now excludes non-operating-corp legal structures in addition to
# the original quality exclusions (Airlines/Biotech/Pharma). Tests verify
# that each structural exclusion category fires the sector gate and emits
# the TICKER_DROPPED_PHASE1_SECTOR_EXCLUDED log line.

def _run_phase_1_single_ticker(
    ticker: str, profile_body: dict, caplog,
) -> list:
    """Helper: run Phase 1 on exactly one ticker with a canned profile2
    response. Returns the list of survivors. caplog captures INFO+WARNING
    so exclusion log lines can be asserted."""
    import logging
    handler = _build_profile_handler({ticker: profile_body})

    async def run():
        client = FinnhubClient(api_key="test_key", transport=httpx.MockTransport(handler))
        try:
            with caplog.at_level(logging.INFO, logger="agt_equities.screener.universe"):
                return await universe.run_phase_1(
                    client, tickers=[ticker], heartbeat_interval=0,
                )
        finally:
            await client.aclose()
    return _run(run())


def test_19_equity_reit_excluded(tmp_cache, caplog):
    """REIT (Equity) must be dropped by the sector gate."""
    survivors = _run_phase_1_single_ticker(
        "SPG",
        {
            "name": "Simon Property Group",
            "country": "US",
            "marketCapitalization": 50000.0,  # $50B, well above MC floor
            "finnhubIndustry": "Equity Real Estate Investment Trusts (REITs)",
        },
        caplog,
    )
    assert survivors == []
    assert any(
        "TICKER_DROPPED_PHASE1_SECTOR_EXCLUDED" in r.message and "SPG" in r.message
        for r in caplog.records
    )


def test_20_mortgage_reit_excluded(tmp_cache, caplog):
    """REIT (Mortgage) must also be dropped by the sector gate."""
    survivors = _run_phase_1_single_ticker(
        "NLY",
        {
            "name": "Annaly Capital Management",
            "country": "US",
            "marketCapitalization": 12000.0,
            "finnhubIndustry": "Mortgage Real Estate Investment Trusts (REITs)",
        },
        caplog,
    )
    assert survivors == []
    assert any(
        "TICKER_DROPPED_PHASE1_SECTOR_EXCLUDED" in r.message and "NLY" in r.message
        for r in caplog.records
    )


def test_21_mlp_via_oil_gas_storage_excluded(tmp_cache, caplog):
    """MLPs are captured via the 'Oil & Gas Storage & Transportation'
    bucket-level exclusion. This is intentionally aggressive — it strips
    MLPs (ET, EPD, MPLX, WES) but will ALSO strip legitimate C-corp
    storage operators in the same bucket. Known trade-off per Architect
    dispatch 2026-04-11 C3.6 — err on the side of overinclusion because
    admitting an MLP breaks Phase 3 math, while missing a legitimate
    C-corp is recoverable by revisiting the filter later.
    """
    survivors = _run_phase_1_single_ticker(
        "ET",
        {
            "name": "Energy Transfer LP",
            "country": "US",
            "marketCapitalization": 60000.0,
            "finnhubIndustry": "Oil & Gas Storage & Transportation",
        },
        caplog,
    )
    assert survivors == []
    assert any(
        "TICKER_DROPPED_PHASE1_SECTOR_EXCLUDED" in r.message and "ET" in r.message
        for r in caplog.records
    )


def test_22_bdc_via_asset_management_excluded(tmp_cache, caplog):
    """BDCs are captured via the 'Asset Management & Custody Banks'
    bucket-level exclusion. Same false-negative caveat as MLPs —
    will also strip BLK/TROW which are legitimate asset managers.
    Revisit in follow-up if post-C3.6 universe feels wrong.
    """
    survivors = _run_phase_1_single_ticker(
        "ARCC",
        {
            "name": "Ares Capital Corporation",
            "country": "US",
            "marketCapitalization": 15000.0,
            "finnhubIndustry": "Asset Management & Custody Banks",
        },
        caplog,
    )
    assert survivors == []
    assert any(
        "TICKER_DROPPED_PHASE1_SECTOR_EXCLUDED" in r.message and "ARCC" in r.message
        for r in caplog.records
    )


def test_23_spac_excluded(tmp_cache, caplog):
    """SPACs (Blank Check companies) must be dropped. Phase 3 cannot
    evaluate a shell entity's fundamentals meaningfully."""
    survivors = _run_phase_1_single_ticker(
        "SHELLCO",
        {
            "name": "Some SPAC Inc",
            "country": "US",
            "marketCapitalization": 15000.0,
            "finnhubIndustry": "Blank Checks",
        },
        caplog,
    )
    assert survivors == []
    assert any(
        "TICKER_DROPPED_PHASE1_SECTOR_EXCLUDED" in r.message and "SHELLCO" in r.message
        for r in caplog.records
    )


def test_24_plain_c_corp_still_passes(tmp_cache, caplog):
    """REGRESSION: the C3.6 expansion must not accidentally break
    legitimate tech/consumer/industrial C-corps. AAPL's actual Finnhub
    sub-industry is 'Technology Hardware, Storage and Peripherals'.
    This string is NOT in EXCLUDED_SECTORS and must pass the gate.
    """
    survivors = _run_phase_1_single_ticker(
        "AAPL",
        {
            "name": "Apple Inc",
            "country": "US",
            "marketCapitalization": 3500000.0,
            "finnhubIndustry": "Technology Hardware, Storage and Peripherals",
        },
        caplog,
    )
    assert len(survivors) == 1
    assert survivors[0].ticker == "AAPL"
    assert survivors[0].sector == "Technology Hardware, Storage and Peripherals"
    # Regression: no sector-exclusion log line should fire for AAPL
    assert not any(
        "TICKER_DROPPED_PHASE1_SECTOR_EXCLUDED" in r.message and "AAPL" in r.message
        for r in caplog.records
    )


def test_18_phase1_final_log_surfaces_cache_stats(tmp_cache, caplog):
    """The final Phase 1 log line must surface cache_hits, cache_misses,
    hit_rate, elapsed, AND the 'Phase 1 complete' marker.

    Setup: pre-populate the cache for one ticker (forces a hit) and leave
    another uncached (forces a miss). Run Phase 1 with both tickers and
    capture the final log message via caplog.
    """
    import logging

    # Pre-populate cache for HIT_TICKER — counts as a hit when run_phase_1
    # calls get_profile2(HIT_TICKER)
    screener_cache.cache_put(
        "finnhub/profile2", "HIT_TICKER",
        {
            "name": "Cached Co", "country": "US",
            "marketCapitalization": 50000.0,  # $50B
            "finnhubIndustry": "Technology",
        },
    )

    # MISS_TICKER is not in cache — triggers an HTTP call (counts as miss)
    handler = _build_profile_handler({
        "MISS_TICKER": {
            "name": "Fresh Co", "country": "US",
            "marketCapitalization": 50000.0,
            "finnhubIndustry": "Technology",
        },
    })

    async def run():
        client = FinnhubClient(api_key="test_key", transport=httpx.MockTransport(handler))
        try:
            with caplog.at_level(logging.INFO, logger="agt_equities.screener.universe"):
                await universe.run_phase_1(
                    client,
                    tickers=["HIT_TICKER", "MISS_TICKER"],
                    heartbeat_interval=0,
                )
        finally:
            await client.aclose()

    _run(run())

    # Find the final "Phase 1 complete" line
    complete_lines = [
        r.message for r in caplog.records if "Phase 1 complete" in r.message
    ]
    assert len(complete_lines) == 1, (
        f"Expected exactly one 'Phase 1 complete' line, got {len(complete_lines)}: "
        f"{complete_lines}"
    )
    final_line = complete_lines[0]

    # Assert all five required tokens are present
    required_tokens = (
        "Phase 1 complete",
        "cache_hits=",
        "cache_misses=",
        "hit_rate=",
        "elapsed=",
    )
    for token in required_tokens:
        assert token in final_line, (
            f"Missing token {token!r} in final log line: {final_line!r}"
        )

    # Defensive: confirm the counters reflect the test setup (1 hit, 1 miss)
    assert "cache_hits=1" in final_line
    assert "cache_misses=1" in final_line
    assert "hit_rate=50.0%" in final_line
