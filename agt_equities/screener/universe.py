"""
agt_equities.screener.universe — Phase 1: Universe loader + Finnhub exclusions.

Loads the static `sp500_nasdaq100.csv` seed (517 tickers as of 2026-04-09),
then iterates each ticker through `FinnhubClient.get_profile2()` to apply
the Phase 1 exclusion gates:

  - Market cap >= MIN_MARKET_CAP_USD ($10B)
  - Sector NOT IN EXCLUDED_SECTORS (Airlines, Biotechnology, Pharmaceuticals)
  - Country NOT IN EXCLUDED_COUNTRIES (China, Hong Kong, Macau)

Heartbeat: emits a `logger.info` line every 50 tickers processed,
including current survivor count, so the operator can watch the daily
06:00 ET refresh job make progress through the ~10-minute Phase 1 pass.

Cache reads are handled transparently by FinnhubClient.get_profile2()
(24h TTL via agt_equities.screener.cache). On a same-day re-run, Phase 1
makes zero network calls.

Fail-soft: any ticker whose profile2 lookup returns None (network error,
delisted, missing fields) is silently dropped from the survivor list.
The screener continues with whatever survivors it has.

ISOLATION CONTRACT: imports only stdlib + agt_equities.screener.{cache,
finnhub_client, config, types}. No telegram_bot, no ib_async, no
agt_equities.rule_engine. Enforced by tests/test_screener_isolation.py.
"""
from __future__ import annotations

import csv
import logging
import time
from pathlib import Path

from agt_equities.screener import config
from agt_equities.screener.finnhub_client import FinnhubClient
from agt_equities.screener.types import UniverseTicker

logger = logging.getLogger(__name__)

# Heartbeat cadence: log progress every N tickers
HEARTBEAT_INTERVAL: int = 50

# Universe seed CSV path (relative to this file)
SEED_CSV_PATH: Path = Path(__file__).resolve().parent / "sp500_nasdaq100.csv"


# ---------------------------------------------------------------------------
# Universe seed loader
# ---------------------------------------------------------------------------

def load_seed_tickers(csv_path: Path | None = None) -> list[str]:
    """Load the universe seed CSV and return the list of ticker symbols.

    Args:
        csv_path: optional override; defaults to SEED_CSV_PATH.

    Returns:
        Sorted list of unique ticker symbols (e.g. ["AAPL", "MSFT", ...]).

    Raises:
        FileNotFoundError: if the CSV is missing (this is a build-time
            invariant — sp500_nasdaq100.csv ships with the package and
            its absence is a deployment bug, not a runtime condition).
    """
    path = csv_path if csv_path is not None else SEED_CSV_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Universe seed CSV missing at {path}. "
            "This file is shipped with the screener package; check git status."
        )

    tickers: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tk = (row.get("ticker") or "").strip().upper()
            if tk:
                tickers.add(tk)

    return sorted(tickers)


# ---------------------------------------------------------------------------
# Phase 1 filter logic — pure functions, no I/O
# ---------------------------------------------------------------------------

def _passes_market_cap(market_cap_usd: float) -> bool:
    """True if market cap meets the $10B+ minimum."""
    return market_cap_usd >= config.MIN_MARKET_CAP_USD


def _passes_sector(sector: str) -> bool:
    """True if the sector is NOT in the exclusion list (case-insensitive).

    Fail-closed on missing or whitespace-only sector data — we can't
    verify exclusion against unknown values.
    """
    if not sector:
        return False
    sector_normalized = sector.strip()
    if not sector_normalized:
        # Whitespace-only — fail-closed
        return False
    # Match against the literal set first, then case-insensitive fallback
    if sector_normalized in config.EXCLUDED_SECTORS:
        return False
    sector_lower = sector_normalized.lower()
    for excluded in config.EXCLUDED_SECTORS:
        if excluded.lower() == sector_lower:
            return False
    return True


def _passes_country(country: str) -> bool:
    """True if the country is NOT in the exclusion list.

    Fail-closed on missing or whitespace-only country data — Act 60
    cares about US-domiciled exposure and unknown countries cannot be
    verified against the exclusion list.
    """
    if not country:
        return False
    country_normalized = country.strip()
    if not country_normalized:
        # Whitespace-only — fail-closed
        return False
    if country_normalized in config.EXCLUDED_COUNTRIES:
        return False
    # Case-insensitive fallback
    country_lower = country_normalized.lower()
    for excluded in config.EXCLUDED_COUNTRIES:
        if excluded.lower() == country_lower:
            return False
    return True


def _profile_to_universe_ticker(ticker: str, profile: dict) -> UniverseTicker | None:
    """Convert a Finnhub profile2 response to a UniverseTicker if it passes
    the Phase 1 exclusion gates. Returns None if any gate fails or required
    fields are missing.

    Finnhub profile2 response shape (verified against Free tier):
        {
            "ticker": "AAPL",
            "name": "Apple Inc",
            "country": "US",
            "currency": "USD",
            "exchange": "NASDAQ NMS - GLOBAL MARKET",
            "ipo": "1980-12-12",
            "marketCapitalization": 3500000.0,   # in MILLIONS of USD
            "shareOutstanding": 15000.0,
            "logo": "...",
            "phone": "...",
            "weburl": "...",
            "finnhubIndustry": "Technology"
        }
    """
    if not isinstance(profile, dict) or not profile:
        return None

    name = str(profile.get("name") or "").strip()
    country = str(profile.get("country") or "").strip()
    sector = str(profile.get("finnhubIndustry") or "").strip()
    mc_millions = profile.get("marketCapitalization")

    # Convert MC from millions to absolute USD
    try:
        mc_usd = float(mc_millions) * 1_000_000.0 if mc_millions else 0.0
    except (TypeError, ValueError):
        return None

    if mc_usd <= 0:
        # No MC data — fail-closed
        return None

    # Apply gates in cheapest-first order
    if not _passes_market_cap(mc_usd):
        return None
    if not _passes_sector(sector):
        # C3.6 delta: per-ticker drop log for the sector exclusion path
        # (tight-scoped per Architect ruling 2026-04-11 — other Phase 1
        # drop paths remain silent, will be audited as a follow-up).
        # info-level because sector exclusion is expected behavior, not
        # degenerate data. Matches Phase 3 / Phase 3.5 planned-drop logging.
        logger.info(
            "[screener.universe] TICKER_DROPPED_PHASE1_SECTOR_EXCLUDED "
            "ticker=%s sector=%s", ticker, sector,
        )
        return None
    if not _passes_country(country):
        return None

    return UniverseTicker(
        ticker=ticker,
        name=name,
        sector=sector,
        country=country,
        market_cap_usd=mc_usd,
    )


# ---------------------------------------------------------------------------
# Phase 1 orchestrator — async, rate-limited via FinnhubClient
# ---------------------------------------------------------------------------

async def run_phase_1(
    client: FinnhubClient,
    tickers: list[str] | None = None,
    *,
    heartbeat_interval: int = HEARTBEAT_INTERVAL,
) -> list[UniverseTicker]:
    """Execute Phase 1: fetch profile2 for each ticker, apply exclusions.

    Args:
        client: an open FinnhubClient instance (caller manages lifecycle)
        tickers: optional list to override the default seed CSV load
        heartbeat_interval: log progress every N tickers (0 to disable)

    Returns:
        List of UniverseTicker survivors. Order is the input ticker order
        with non-survivors filtered out.

    Notes:
      - Rate limiting is handled inside FinnhubClient (50/min sustained).
      - Caching is handled inside FinnhubClient (24h TTL on profile2).
      - Per-ticker failures (None response) are silently dropped, never
        raised. The screener fails soft per ticker.
      - The 06:00 ET scheduled refresh wall-clock for the full 517-ticker
        cold pass at 50/min is approximately 10.3 minutes. Subsequent
        same-day calls are served from cache and complete in seconds.
    """
    seed = tickers if tickers is not None else load_seed_tickers()
    total = len(seed)
    survivors: list[UniverseTicker] = []

    logger.info(
        "Phase 1 (Finnhub Free profile2): starting with %d seed tickers",
        total,
    )

    start_ts = time.monotonic()

    for idx, ticker in enumerate(seed, start=1):
        try:
            profile = await client.get_profile2(ticker)
        except Exception as exc:
            # Defensive — FinnhubClient is fail-soft, but catch unexpected
            # exceptions so a single bad ticker can't kill the whole pass
            logger.warning("Phase 1: get_profile2(%s) raised: %s", ticker, exc)
            profile = None

        if profile is None:
            continue  # fail-soft per ticker

        candidate = _profile_to_universe_ticker(ticker, profile)
        if candidate is not None:
            survivors.append(candidate)

        # Heartbeat — every N tickers, regardless of survivor status
        if heartbeat_interval > 0 and idx % heartbeat_interval == 0:
            logger.info(
                "Phase 1 heartbeat: %d/%d processed, %d survivors so far",
                idx, total, len(survivors),
            )

    elapsed = time.monotonic() - start_ts
    stats = client.get_stats()
    logger.info(
        "Phase 1 complete: processed=%d survivors=%d dropped=%d "
        "cache_hits=%d cache_misses=%d hit_rate=%.1f%% elapsed=%.1fs",
        total, len(survivors), total - len(survivors),
        stats["cache_hits"], stats["cache_misses"],
        stats["hit_rate_pct"], elapsed,
    )
    return survivors
