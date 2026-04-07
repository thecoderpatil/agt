"""
agt_equities/state_builder.py — Upstream populator for PortfolioState.

Fetches market data via the data provider abstraction and computes
derived fields (correlations, account EL snapshots) before handing
the immutable PortfolioState to pure evaluators.

This is the ONLY file that bridges impure I/O (provider calls) with
the pure rule_engine. Evaluators never call the provider directly.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from itertools import combinations

from agt_equities.data_provider import (
    MarketDataProvider, DataProviderError, Bar,
)
from agt_equities.rule_engine import (
    CorrelationData, AccountELSnapshot, CORRELATION_EXCLUDED_TICKERS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Correlation computation (pure math, called with pre-fetched bars)
# ---------------------------------------------------------------------------

def compute_pearson_correlation(bars_a: list[Bar], bars_b: list[Bar]) -> tuple[float, int]:
    """Compute Pearson correlation of daily log returns for two bar series.

    Aligns on common dates. Returns (correlation, sample_count).
    Returns (0.0, 0) if insufficient data.
    """
    # Build date -> close maps
    map_a = {b.date: b.close for b in bars_a}
    map_b = {b.date: b.close for b in bars_b}

    # Find common dates, sorted
    common = sorted(set(map_a.keys()) & set(map_b.keys()))
    if len(common) < 3:
        return 0.0, 0

    # Compute daily log returns on common dates
    returns_a = []
    returns_b = []
    for i in range(1, len(common)):
        prev, curr = common[i - 1], common[i]
        ca_prev, ca_curr = map_a[prev], map_a[curr]
        cb_prev, cb_curr = map_b[prev], map_b[curr]
        if ca_prev > 0 and cb_prev > 0:
            returns_a.append(math.log(ca_curr / ca_prev))
            returns_b.append(math.log(cb_curr / cb_prev))

    n = len(returns_a)
    if n < 2:
        return 0.0, 0

    # Pearson correlation via manual computation (no numpy dependency in pure math)
    mean_a = sum(returns_a) / n
    mean_b = sum(returns_b) / n
    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(returns_a, returns_b)) / n
    var_a = sum((a - mean_a) ** 2 for a in returns_a) / n
    var_b = sum((b - mean_b) ** 2 for b in returns_b) / n
    denom = math.sqrt(var_a * var_b)
    if denom < 1e-15:
        return 0.0, n

    return cov / denom, n


# ---------------------------------------------------------------------------
# Correlation matrix builder
# ---------------------------------------------------------------------------

def build_correlation_matrix(
    tickers: list[str],
    provider: MarketDataProvider,
    lookback_days: int = 180,
) -> dict[tuple[str, str], CorrelationData]:
    """Fetch bars and compute pairwise correlations for all ticker pairs.

    Skips CORRELATION_EXCLUDED_TICKERS.
    Returns dict keyed by (ticker_a, ticker_b) with ticker_a < ticker_b.
    """
    eligible = sorted(t for t in tickers if t not in CORRELATION_EXCLUDED_TICKERS)
    if len(eligible) <= 1:
        return {}

    # Fetch bars for all eligible tickers
    bars_cache: dict[str, list[Bar] | None] = {}
    for t in eligible:
        try:
            bars_cache[t] = provider.get_historical_daily_bars(t, lookback_days)
        except DataProviderError as exc:
            logger.warning("Failed to fetch bars for %s: %s", t, exc)
            bars_cache[t] = None

    # Compute pairwise
    result = {}
    for t_a, t_b in combinations(eligible, 2):
        bars_a = bars_cache.get(t_a)
        bars_b = bars_cache.get(t_b)
        if bars_a is None or bars_b is None:
            continue  # skip pair, evaluator will detect gap
        corr, sample = compute_pearson_correlation(bars_a, bars_b)
        result[(t_a, t_b)] = CorrelationData(
            value=round(corr, 6),
            sample_days=sample,
            low_confidence=sample < lookback_days,
            source="provider_daily_bars",
        )

    return result


# ---------------------------------------------------------------------------
# Account EL snapshot builder
# ---------------------------------------------------------------------------

def build_account_el_snapshot(
    account_id: str,
    provider: MarketDataProvider,
) -> AccountELSnapshot | None:
    """Fetch account summary and return an EL snapshot. Returns None on failure."""
    try:
        summary = provider.get_account_summary(account_id)
        return AccountELSnapshot(
            excess_liquidity=summary.excess_liquidity,
            net_liquidation=summary.net_liquidation,
            timestamp=summary.timestamp.isoformat(),
            stale=False,
        )
    except DataProviderError as exc:
        logger.warning("Failed to fetch account summary for %s: %s", account_id, exc)
        return None


# ---------------------------------------------------------------------------
# Account NLV builder (closes 3A.5a gap)
# ---------------------------------------------------------------------------

def build_account_nlv(
    account_ids: list[str],
    provider: MarketDataProvider,
) -> dict[str, float | None]:
    """Returns per-account NLV dict. Closes the 3A.5a gap.

    Used by R2 (margin-eligible NLV denominator) and R11
    (all-account NLV denominator).
    Returns None for accounts where the provider call fails.
    """
    result: dict[str, float | None] = {}
    for account_id in account_ids:
        try:
            summary = provider.get_account_summary(account_id)
            result[account_id] = summary.net_liquidation
        except DataProviderError as exc:
            logger.warning("Failed to fetch NLV for %s: %s", account_id, exc)
            result[account_id] = None
    return result
