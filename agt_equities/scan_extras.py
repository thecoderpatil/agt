"""agt_equities.scan_extras — Bridge-2 data fetchers for CSP allocator gates.

Impure module: calls yfinance (via YFinanceCorporateIntelligenceProvider)
and pandas for correlation computation. Kept separate from scan_bridge.py
which remains pure.

Called by telegram_bot.py's /scan handler to pre-fetch:
  - earnings_map: {TICKER: days_to_next_earnings} for Rule 7
  - correlation_pairs: {(ticker_a, ticker_b): float} for Rule 4

Both functions are sync (yfinance has no async API). The /scan handler
wraps them in asyncio.to_thread().
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from agt_equities.dates import et_today
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Earnings map (Rule 7: 7-day earnings blackout)
# ---------------------------------------------------------------------------


def fetch_earnings_map(
    tickers: list[str],
    *,
    provider: Any | None = None,
    today: date | None = None,
) -> dict[str, int | None]:
    """Fetch days-to-next-earnings for each ticker.

    Uses YFinanceCorporateIntelligenceProvider (24h file cache).
    Returns {TICKER: int_days} or {TICKER: None} if no earnings date
    available. Never raises — logs warnings and returns None per ticker.

    Args:
        tickers: List of ticker symbols to look up.
        provider: Optional injected provider (for testing). Default
            instantiates YFinanceCorporateIntelligenceProvider.
        today: Optional date override (for testing). Default is
            date.today().
    """
    if provider is None:
        try:
            from agt_equities.providers.yfinance_corporate_intelligence import (
                YFinanceCorporateIntelligenceProvider,
            )
            provider = YFinanceCorporateIntelligenceProvider()
        except Exception as exc:
            logger.warning(
                "scan_extras: failed to instantiate earnings provider: %s", exc
            )
            return {t.upper(): None for t in tickers}

    ref_date = today or et_today()
    result: dict[str, int | None] = {}

    for ticker in tickers:
        tk = ticker.upper()
        try:
            cal = provider.get_corporate_calendar(tk)
            if cal is None or cal.next_earnings is None:
                result[tk] = None
                continue
            delta_days = (cal.next_earnings - ref_date).days
            result[tk] = delta_days
        except Exception as exc:
            logger.warning(
                "scan_extras: earnings lookup failed for %s: %s", tk, exc
            )
            result[tk] = None

    logger.info(
        "scan_extras: earnings_map fetched for %d tickers, "
        "%d have dates, %d missing",
        len(result),
        sum(1 for v in result.values() if v is not None),
        sum(1 for v in result.values() if v is None),
    )
    return result


# ---------------------------------------------------------------------------
# Correlation pairs (Rule 4: >0.6 pairwise correlation veto)
# ---------------------------------------------------------------------------


def build_correlation_pairs(
    candidate_tickers: list[str],
    holding_tickers: list[str],
    *,
    download_fn: Any | None = None,
    period: str = "6mo",
) -> dict[tuple[str, str], float]:
    """Compute 6-month pairwise correlations: candidates × holdings.

    Returns {(candidate, holding): correlation_float}. Rule 4 checks
    both orderings so we only store one direction.

    Uses yfinance batch download for all unique tickers in one call,
    then pandas .corr() on daily returns. Never raises — returns empty
    dict on failure so Rule 4 fail-opens (same as bridge-1 behavior,
    but with structured logging).

    Args:
        candidate_tickers: Tickers from the scan candidates.
        holding_tickers: Tickers from the household positions/CSPs.
        download_fn: Optional injection point (for testing). Called as
            download_fn(symbols, period) -> {ticker: DataFrame}.
            Default uses yfinance.download.
        period: yfinance period string. Default "6mo" per Rulebook.
    """
    if not candidate_tickers or not holding_tickers:
        return {}

    candidates_set = {t.upper() for t in candidate_tickers}
    holdings_set = {t.upper() for t in holding_tickers}
    # Remove overlap — a ticker held AND being scanned needs no self-corr
    all_tickers = sorted(candidates_set | holdings_set)

    if len(all_tickers) < 2:
        return {}

    # ── Download price history ──
    if download_fn is not None:
        prices = download_fn(all_tickers, period)
    else:
        prices = _default_yf_batch(all_tickers, period)

    if prices is None or not len(prices):
        logger.warning(
            "scan_extras: correlation download returned empty; "
            "Rule 4 will fail-open for all candidates"
        )
        return {}

    # ── Extract daily returns ──
    try:
        import pandas as pd

        # prices is a {ticker: DataFrame} dict from the download
        closes: dict[str, Any] = {}
        for tk, df in prices.items():
            if df is not None and not df.empty and "Close" in df.columns:
                closes[tk] = df["Close"]

        if len(closes) < 2:
            return {}

        close_df = pd.DataFrame(closes)
        returns = close_df.pct_change().dropna(how="all")

        if returns.empty or len(returns) < 20:
            logger.warning(
                "scan_extras: insufficient return data (%d rows); "
                "correlation pairs empty", len(returns),
            )
            return {}

        corr_matrix = returns.corr()
    except Exception as exc:
        logger.warning(
            "scan_extras: correlation computation failed: %s", exc
        )
        return {}

    # ── Extract candidate × holding pairs ──
    result: dict[tuple[str, str], float] = {}
    for cand in candidates_set:
        if cand not in corr_matrix.columns:
            continue
        for hold in holdings_set:
            if hold not in corr_matrix.columns or hold == cand:
                continue
            try:
                val = float(corr_matrix.loc[cand, hold])
                if val != val:  # NaN check
                    continue
                result[(cand, hold)] = round(val, 4)
            except (KeyError, TypeError, ValueError):
                continue

    logger.info(
        "scan_extras: correlation_pairs computed: %d pairs "
        "(%d candidates × %d holdings, %d tickers downloaded)",
        len(result), len(candidates_set), len(holdings_set),
        len(prices),
    )
    return result


def _default_yf_batch(
    symbols: list[str], period: str,
) -> dict[str, Any]:
    """Production yfinance batch downloader for correlation.

    Returns {ticker: DataFrame} dict. Empty dict on any failure.
    Lazy import keeps module testable without yfinance installed.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("scan_extras: yfinance not installed; returning empty")
        return {}

    try:
        raw = yf.download(
            tickers=symbols,
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.warning("scan_extras: yfinance batch download failed: %s", exc)
        return {}

    if raw is None or raw.empty:
        return {}

    import pandas as pd

    result: dict[str, Any] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for tk in raw.columns.get_level_values(0).unique():
            try:
                sub = raw[tk].dropna(how="all")
                if not sub.empty:
                    result[str(tk)] = sub
            except (KeyError, ValueError):
                continue
    else:
        # Single ticker — raw is already a flat DataFrame
        if len(symbols) == 1:
            sub = raw.dropna(how="all")
            if not sub.empty:
                result[symbols[0]] = sub

    return result
