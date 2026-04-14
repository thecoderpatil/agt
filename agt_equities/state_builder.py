"""
agt_equities/state_builder.py — Upstream populator for PortfolioState + DeskSnapshot.

Fetches market data via the data provider abstraction and computes
derived fields (correlations, account EL snapshots) before handing
the immutable PortfolioState to pure evaluators.

DeskSnapshot (Sprint C1): point-in-time desk state covering settled
cycles, DEX encumbrance, beta cache, per-account NAV, and optionally
injected live IB positions. IB-free, pure DB read path.

This is the ONLY file that bridges impure I/O (provider calls) with
the pure rule_engine. Evaluators never call the provider directly.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Dict, List, Optional, Tuple

from agt_equities.config import HOUSEHOLD_MAP, ACCOUNT_TO_HOUSEHOLD
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


# ---------------------------------------------------------------------------
# Sprint C1: DeskSnapshot — point-in-time desk state (IB-free, pure DB)
# ---------------------------------------------------------------------------

# Active DEX statuses — copied from telegram_bot.py:7168 (Sprint B Unit 2)
DEX_ACTIVE_STATUSES: frozenset = frozenset({'STAGED', 'ATTESTED', 'TRANSMITTING'})


@dataclass(frozen=True)
class DeskSnapshot:
    """Point-in-time desk state for settled cycles + DB-sourced data.

    Sprint C1: additive. No callers wired yet (C2 swaps _build_cure_data,
    C3 swaps _discover_positions).

    live_positions is optionally injected by the caller (telegram_bot fetches
    from IB and passes in). When None, field is [] and a warning is appended.
    """
    # Identity + timing
    snapshot_ts: datetime
    db_path: str

    # Account-level NAV (per-account MAX(report_date), per Sprint 1F Fix 1)
    nav_by_account: Dict[str, float]       # account_id -> NLV
    nav_total: float
    household_nav: Dict[str, float]        # household -> summed NLV

    # Settled positions (from Walker via trade_repo.get_active_cycles)
    active_cycles: List                    # list[Cycle] — duck-typed

    # Intraday live positions (injected by caller from IB snapshot)
    live_positions: List[dict]

    # DEX encumbrance (from bucket3_dynamic_exit_log, per Sprint B Unit 2)
    # frozenset of (household, ticker) tuples with active DEX orders
    dex_encumbered_keys: frozenset         # frozenset[Tuple[str, str]]

    # Beta (from beta_cache table, per Sprint 1F Fix 2)
    beta_by_symbol: Dict[str, float]

    # NAV source tracking (observability — not consumed by rule engine)
    nav_source_by_account: Dict[str, str] = field(default_factory=dict)

    # Meta
    warnings: List[str] = field(default_factory=list)


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """Open a read-only DB connection with Sprint B Unit 6 PRAGMA tuning."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def build_state(
    db_path: Optional[str] = None,
    live_positions: Optional[List[dict]] = None,
    live_nlv: Optional[Dict[str, float]] = None,
) -> DeskSnapshot:
    """Build a point-in-time snapshot of desk state.

    Single source of truth consumed by _build_cure_data and
    _discover_positions (wired in C2/C3). Additive in C1 — no callers
    change yet.

    Args:
        db_path: path to SQLite DB. Defaults to agt_equities.db.DB_PATH
            (the canonical shared module constant). FU-A-03a migrated the
            default away from the now-deleted trade_repo.DB_PATH.
        live_positions: optional pre-fetched IB snapshot. Caller
            (telegram_bot) fetches from IB and passes in. When None,
            DeskSnapshot.live_positions is an empty list and a warning
            records the absence.
        live_nlv: optional caller-injected per-account NLV from IBKR
            accountSummary (0-second freshness). When provided, takes
            priority over el_snapshots and master_log_nav for matching
            account IDs.

    Raises:
        ValueError: if HOUSEHOLD_MAP is empty or NAV query returns zero rows.
        sqlite3.Error: on critical DB read failure (NAV query).
    """
    from agt_equities import trade_repo  # deferred to avoid circular at import time

    if db_path is None:
        # FU-A-03a + FU-A-04: default routes through the canonical
        # shared module agt_equities.db.DB_PATH. The legacy
        # trade_repo.DB_PATH module attribute was deleted in FU-A-04
        # Phase E; this line is the post-sprint SSOT.
        from agt_equities.db import DB_PATH as _DEFAULT_DB_PATH
        db_path = str(_DEFAULT_DB_PATH)

    warnings: List[str] = []
    now = datetime.utcnow()

    # ── 1. NAV per-account MAX(report_date) ──
    # Pattern source: agt_deck/queries.py:70-78 (Sprint 1F Fix 1)
    # Each account uses its own MAX(report_date) so dormant accounts
    # still contribute their last known NAV.
    nav_by_account: Dict[str, float] = {}
    with _open_readonly(db_path) as conn:
        rows = conn.execute("""
            SELECT m1.account_id, CAST(m1.total AS REAL) as nav
            FROM master_log_nav m1
            WHERE m1.report_date = (
                SELECT MAX(m2.report_date)
                FROM master_log_nav m2
                WHERE m2.account_id = m1.account_id
            )
        """).fetchall()
        for r in rows:
            nav_by_account[r["account_id"]] = r["nav"]

    if not nav_by_account:
        raise ValueError(
            "NAV query returned zero rows — master_log_nav is empty or DB path is wrong"
        )

    # NAV overlay: 3-tier priority
    # 1. live_nlv param (0-second, caller-injected from accountSummary)
    # 2. el_snapshots fresh (<120s, via 30s writer job)
    # 3. master_log_nav (Flex EOD fallback, already in nav_by_account)
    nav_source_by_account: Dict[str, str] = {}

    # Tier 2: el_snapshots query
    db_live_nav: Dict[str, float] = {}
    try:
        with _open_readonly(db_path) as _snap_conn:
            _snap_rows = _snap_conn.execute("""
                SELECT e1.account_id, e1.nlv
                FROM el_snapshots e1
                WHERE e1.id = (
                    SELECT MAX(e2.id) FROM el_snapshots e2
                    WHERE e2.account_id = e1.account_id
                )
                AND e1.nlv IS NOT NULL
                AND (julianday('now') - julianday(e1.timestamp)) * 86400 <= ?
            """, (120,)).fetchall()
            db_live_nav = {r["account_id"]: r["nlv"] for r in _snap_rows}
    except Exception:
        # el_snapshots may not exist in test DBs or pre-Sprint-1B schemas.
        # Silently fall back to Flex-only NAV — not an operational warning.
        db_live_nav = {}

    # Apply priority: injected > db_live > flex_eod
    for acct_id in nav_by_account:
        if live_nlv and acct_id in live_nlv:
            nav_by_account[acct_id] = live_nlv[acct_id]
            nav_source_by_account[acct_id] = "live_injected"
        elif acct_id in db_live_nav:
            nav_by_account[acct_id] = db_live_nav[acct_id]
            nav_source_by_account[acct_id] = "live_db"
        else:
            nav_source_by_account[acct_id] = "flex_eod"

    nav_total = sum(nav_by_account.values())

    # Derive household NAV via ACCOUNT_TO_HOUSEHOLD from config
    household_nav: Dict[str, float] = {}
    for acct, nav in nav_by_account.items():
        hh = ACCOUNT_TO_HOUSEHOLD.get(acct)
        if hh:
            household_nav[hh] = household_nav.get(hh, 0.0) + nav

    # ── 2. Active cycles (settled, from Walker) ──
    active_cycles: List = []
    try:
        active_cycles = trade_repo.get_active_cycles(db_path=db_path)
    except Exception as exc:
        warnings.append(f"active_cycles query failed: {exc}")

    # ── 3. DEX encumbrance ──
    # Pattern source: telegram_bot.py:7161-7182 (Sprint B Unit 2)
    dex_encumbered_keys: frozenset = frozenset()
    try:
        with _open_readonly(db_path) as conn:
            placeholders = ",".join("?" for _ in DEX_ACTIVE_STATUSES)
            dex_rows = conn.execute(
                "SELECT DISTINCT household, ticker "
                "FROM bucket3_dynamic_exit_log "
                f"WHERE final_status IN ({placeholders})",
                tuple(sorted(DEX_ACTIVE_STATUSES)),
            ).fetchall()
            dex_encumbered_keys = frozenset(
                (row["household"], row["ticker"]) for row in dex_rows
            )
    except sqlite3.Error as exc:
        warnings.append(f"DEX encumbrance query failed: {exc}")

    # ── 4. Beta cache ──
    beta_by_symbol: Dict[str, float] = {}
    try:
        with _open_readonly(db_path) as conn:
            beta_rows = conn.execute(
                "SELECT ticker, beta FROM beta_cache"
            ).fetchall()
            beta_by_symbol = {row["ticker"]: row["beta"] for row in beta_rows}
    except sqlite3.Error as exc:
        warnings.append(f"beta_cache query failed: {exc}")

    if not beta_by_symbol:
        warnings.append("beta_cache empty — consumers must fall back to 1.0")

    # ── 5. Live positions (injected) ──
    if live_positions is None:
        live_positions = []
        warnings.append(
            "live_positions not provided — DeskSnapshot built from DB only"
        )

    return DeskSnapshot(
        snapshot_ts=now,
        db_path=db_path,
        nav_by_account=nav_by_account,
        nav_total=nav_total,
        household_nav=household_nav,
        active_cycles=active_cycles,
        live_positions=live_positions,
        dex_encumbered_keys=dex_encumbered_keys,
        beta_by_symbol=beta_by_symbol,
        nav_source_by_account=nav_source_by_account,
        warnings=warnings,
    )
