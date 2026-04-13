"""
agt_equities.ib_chains — IBKR option chain fetcher via ib_async.

Replaces yfinance option chain calls for EXECUTION_CRITICAL paths.
Fail-loudly: never falls through to yfinance on IBKR failure.

Cache: 5-minute TTL per ticker for expirations, 60s for chain data.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "agt_desk.db"


# ── C6.2: NaN-safe numeric coercion helpers ─────────────────────
#
# IBKR reqMktData can populate volume / openInterest / impliedVolatility
# with NaN when the OPRA subscription is missing, when the strike is
# illiquid, or when the snapshot arrives partial. Bare int(NaN) raises
# ValueError, which crashed the entire chain fetch in the 2026-04-11
# paper run for GD, HSY, and MPC across multiple expiries. These
# helpers return a safe default on None / NaN / uncoercible input.
#
# Used by _build_chain_rows (the pure coercion loop extracted from
# get_chain_for_expiry for unit testability per C6.2 dispatch ruling)
# and by get_spot / get_spots_batch for the price extraction path.

def _safe_int(v, default: int = 0) -> int:
    """Coerce a value to int, returning default on None or NaN.

    IBKR reqMktData can populate numeric fields with NaN when the
    subscription is missing, the strike is illiquid, or data has
    not yet arrived. Bare int(NaN) raises ValueError. This helper
    catches None, NaN, and any other uncoercible value and returns
    the default (typically 0).
    """
    if v is None:
        return default
    if isinstance(v, float) and math.isnan(v):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v, default: float = 0.0) -> float:
    """Coerce a value to float, returning default on None or NaN.

    Same rationale as _safe_int but for float-typed fields. Also
    guards against float() succeeding on a NaN float input that
    would otherwise propagate through downstream math unchanged.
    """
    if v is None:
        return default
    if isinstance(v, float) and math.isnan(v):
        return default
    try:
        result = float(v)
        if math.isnan(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _build_chain_rows(tickers_data: dict) -> list[dict]:
    """Build chain row dicts from a {strike: ticker_data} mapping.

    Pure function — no IB dependency, no side effects. Extracted
    from get_chain_for_expiry so the NaN-safe coercion loop can be
    unit-tested without a FakeIB. Each value in tickers_data must
    expose .bid, .ask, .last, .volume, .openInterest, and
    .impliedVolatility attributes (duck-typed; pytest tests pass
    types.SimpleNamespace instances).

    Returns list of dicts shaped identically to the pre-C6.2
    get_chain_for_expiry output:
      {strike, bid, ask, last, volume, openInterest, impliedVol}

    C6.2 semantics (BIT-IDENTICAL to C5/C6/C6.1 on valid data):
      - NaN-safe via _safe_int / _safe_float on every numeric field
      - Negative bid/ask/last/iv values clamped to 0.0 (preserves
        the pre-C6.2 "> 0 else 0.0" semantic for valid negative
        inputs, which were never legitimate anyway)
      - Volume and openInterest are always int; others always float
      - Strike is coerced via float(strike) to match pre-C6.2 output
      - Output rows are in ascending strike order (caller sorts
        tickers_data.items() before passing)
    """
    results = []
    for strike, td in sorted(tickers_data.items()):
        # NaN-safe coercion — IBKR can return NaN when subscription
        # is missing or strike is illiquid. See C6.2 dispatch.
        bid = _safe_float(td.bid)
        ask = _safe_float(td.ask)
        last = _safe_float(td.last)
        vol = _safe_int(td.volume)
        oi = _safe_int(td.openInterest)
        iv = _safe_float(td.impliedVolatility)

        # Preserve pre-C6.2 semantics: clamp negative prices / IV
        # to zero. _safe_float handles NaN and None; this second
        # pass handles the "valid-but-negative" case that the
        # original `v if v > 0 else 0.0` idiom protected against.
        if bid < 0:
            bid = 0.0
        if ask < 0:
            ask = 0.0
        if last < 0:
            last = 0.0
        if iv < 0:
            iv = 0.0

        # Sprint-1.2: extract modelGreeks.delta for inception_delta tracking.
        # Defensive: None-safe at every layer. Never drop rows on missing delta.
        delta_val = None
        try:
            mg = getattr(td, "modelGreeks", None)
            if mg is not None and getattr(mg, "delta", None) is not None:
                delta_val = abs(float(mg.delta))  # sprint-1.5: unsigned delta magnitude — see HANDOFF_ARCHITECT_v20
        except (TypeError, ValueError, AttributeError):
            delta_val = None

        results.append({
            'strike': float(strike),
            'bid': bid,
            'ask': ask,
            'last': last,
            'volume': vol,
            'openInterest': oi,
            'impliedVol': iv,
            'delta': delta_val,
        })
    return results


# ── Error classification ──

class IBKRChainError(Exception):
    """Base error for IBKR chain fetch failures."""
    def __init__(self, message: str, error_class: str = "UNKNOWN"):
        super().__init__(message)
        self.error_class = error_class


class IBKRNetworkError(IBKRChainError):
    def __init__(self, message: str):
        super().__init__(message, "NETWORK")


class IBKRMarketClosedError(IBKRChainError):
    def __init__(self, message: str):
        super().__init__(message, "MARKET_CLOSED")


class IBKRNoDataError(IBKRChainError):
    def __init__(self, message: str):
        super().__init__(message, "NO_DATA")


class IBKRRateLimitError(IBKRChainError):
    def __init__(self, message: str):
        super().__init__(message, "RATE_LIMIT")


# ── Cache ──

@dataclass
class CachedChain:
    expirations: list[str]  # YYYY-MM-DD format
    fetched_at: float
    strikes_by_expiry: dict[str, list[float]] = field(default_factory=dict)

_chain_cache: dict[str, CachedChain] = {}
EXPIRY_CACHE_TTL = 300  # 5 minutes
CHAIN_CACHE_TTL = 60    # 60 seconds

# Per-(ticker, expiry, right) canonical strike cache.
# IBKR's reqSecDefOptParamsAsync returns the UNION of strikes
# across all expirations. This cache stores the actual valid
# strike list for each specific expiry, fetched via
# reqContractDetailsAsync on a partial Option contract.
_per_expiry_strikes: dict[tuple[str, str, str], set[float]] = {}


def invalidate_cache(ticker: str | None = None) -> None:
    """Clear chain cache for a ticker, or all."""
    if ticker:
        _chain_cache.pop(ticker, None)
    else:
        _chain_cache.clear()


# ── Audit logging ──

def _log_fetch(ticker: str, source: str, latency_ms: float,
               success: bool, error_class: str = "") -> None:
    """Write to market_data_log table."""
    try:
        from contextlib import closing
        with closing(sqlite3.connect(DB_PATH, timeout=5.0)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO market_data_log "
                    "(timestamp, ticker, source, latency_ms, success, error_class) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.utcnow().isoformat(), ticker, source,
                     round(latency_ms, 1), 1 if success else 0, error_class),
                )
    except Exception:
        pass  # audit logging must never crash the main path


# ── Core fetchers ──

async def get_expirations(ib, ticker: str) -> list[str]:
    """Fetch available option expirations for a ticker via IBKR.

    Returns list of expiration dates in YYYY-MM-DD format, sorted.
    Raises IBKRChainError subclass on failure.
    """
    # Check cache
    cached = _chain_cache.get(ticker)
    if cached and (time.time() - cached.fetched_at) < EXPIRY_CACHE_TTL:
        return cached.expirations

    t0 = time.time()
    try:
        import ib_async
        contract = ib_async.Stock(ticker, 'SMART', 'USD')
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            raise IBKRNoDataError(f"Could not qualify {ticker}")

        chains = await ib.reqSecDefOptParamsAsync(
            underlyingSymbol=ticker,
            futFopExchange='',
            underlyingSecType='STK',
            underlyingConId=qualified[0].conId,
        )

        if not chains:
            raise IBKRNoDataError(f"No option params for {ticker}")

        # Find the SMART exchange chain (most complete)
        best_chain = None
        for ch in chains:
            if ch.exchange == 'SMART':
                best_chain = ch
                break
        if best_chain is None:
            best_chain = chains[0]

        # Convert expirations from YYYYMMDD to YYYY-MM-DD
        expirations = []
        today = date.today()
        for exp_str in sorted(best_chain.expirations):
            try:
                exp_date = datetime.strptime(exp_str, "%Y%m%d").date()
                if exp_date > today:
                    expirations.append(exp_date.isoformat())
            except ValueError:
                continue

        strikes = sorted(best_chain.strikes)

        latency = (time.time() - t0) * 1000
        _log_fetch(ticker, 'IBKR', latency, True)

        # Cache
        _chain_cache[ticker] = CachedChain(
            expirations=expirations,
            fetched_at=time.time(),
            strikes_by_expiry={},  # populated on demand
        )
        # Store strikes globally for this ticker (same for all expiries)
        _chain_cache[ticker]._all_strikes = strikes

        return expirations

    except IBKRChainError:
        raise
    except Exception as exc:
        latency = (time.time() - t0) * 1000
        error_class = _classify_error(exc)
        _log_fetch(ticker, 'IBKR', latency, False, error_class)
        raise IBKRChainError(f"Chain fetch failed for {ticker}: {exc}", error_class) from exc


async def _get_canonical_strikes_for_expiry(
    ib, ticker: str, expiry_ib: str, right: str,
) -> set[float]:
    """Return the set of strikes that actually exist for this
    specific (ticker, expiry, right) combination at SMART.

    Cached per-session. Cache key is (ticker, expiry_ib, right).
    Uses reqContractDetailsAsync with a partial Option contract
    (no strike) — IBKR returns all listed strikes for that
    expiry as separate ContractDetails entries.

    On any failure, returns empty set (caller handles).
    """
    cache_key = (ticker, expiry_ib, right)
    if cache_key in _per_expiry_strikes:
        return _per_expiry_strikes[cache_key]

    try:
        from ib_async import Option
        partial = Option(
            symbol=ticker,
            lastTradeDateOrContractMonth=expiry_ib,
            right=right,
            exchange='SMART',
            currency='USD',
        )
        details = await ib.reqContractDetailsAsync(partial)
        if not details:
            logger.warning(
                "ib_chains: reqContractDetailsAsync empty for "
                "%s %s %s — caching empty set",
                ticker, expiry_ib, right,
            )
            _per_expiry_strikes[cache_key] = set()
            return set()

        canonical = {
            float(cd.contract.strike)
            for cd in details
            if cd.contract.strike and cd.contract.strike > 0
        }
        _per_expiry_strikes[cache_key] = canonical
        logger.info(
            "ib_chains: cached %d canonical strikes for %s %s %s",
            len(canonical), ticker, expiry_ib, right,
        )
        return canonical
    except Exception as exc:
        logger.warning(
            "ib_chains: _get_canonical_strikes_for_expiry failed "
            "for %s %s %s: %s — caching empty set as guard",
            ticker, expiry_ib, right, exc,
        )
        _per_expiry_strikes[cache_key] = set()
        return set()


async def get_chain_for_expiry(
    ib, ticker: str, expiry: str, right: str = 'C',
    min_strike: float = 0, max_strike: float = 999999,
) -> list[dict]:
    """Fetch option chain (strikes + market data) for one expiry.

    Args:
        ib: ib_async.IB instance
        ticker: underlying symbol
        expiry: YYYY-MM-DD format
        right: 'C' or 'P'
        min_strike: filter strikes >= this
        max_strike: filter strikes <= this

    Returns list of dicts: {strike, bid, ask, last, volume, openInterest, impliedVol}
    Raises IBKRChainError on failure.
    """
    t0 = time.time()
    try:
        import ib_async

        # Get strikes from cache or fetch
        cached = _chain_cache.get(ticker)
        if cached and hasattr(cached, '_all_strikes'):
            all_strikes = cached._all_strikes
        else:
            await get_expirations(ib, ticker)
            cached = _chain_cache.get(ticker)
            all_strikes = getattr(cached, '_all_strikes', []) if cached else []

        if not all_strikes:
            raise IBKRNoDataError(f"No strikes available for {ticker}")

        # Filter to requested range
        strikes = [s for s in all_strikes if min_strike <= s <= max_strike]
        if not strikes:
            raise IBKRNoDataError(f"No strikes in range [{min_strike}, {max_strike}] for {ticker}")

        # Convert expiry to YYYYMMDD
        expiry_ib = expiry.replace("-", "")

        # Filter against canonical per-expiry strikes — eliminates phantom
        # strike+expiry combinations that the union list would otherwise
        # include. Without this filter, walker generates Option contracts
        # for strikes that don't exist for the specific expiry, causing
        # Error 200 / No security definition errors at reqMktData time.
        canonical_strikes = await _get_canonical_strikes_for_expiry(
            ib, ticker, expiry_ib, right,
        )
        if canonical_strikes:
            before_count = len(strikes)
            strikes = [s for s in strikes if s in canonical_strikes]
            filtered_count = before_count - len(strikes)
            if filtered_count > 0:
                logger.info(
                    "ib_chains: filtered %d phantom strikes for %s %s %s "
                    "(kept %d of %d)",
                    filtered_count, ticker, expiry_ib, right,
                    len(strikes), before_count,
                )
        else:
            # Empty canonical set means reqContractDetailsAsync failed or
            # the expiry has no listed strikes. Fall through to the
            # existing conId==0 guard rather than failing here — this
            # preserves backward compatibility for any path that worked
            # before this fix.
            logger.warning(
                "ib_chains: empty canonical set for %s %s %s — "
                "falling back to union list (legacy behavior)",
                ticker, expiry_ib, right,
            )

        if not strikes:
            raise IBKRNoDataError(
                f"No canonical strikes for {ticker} {expiry} in range "
                f"[{min_strike}, {max_strike}]"
            )

        # Build contracts and request market data
        contracts = []
        for strike in strikes:
            c = ib_async.Option(ticker, expiry_ib, strike, right, 'SMART')
            contracts.append(c)

        # Qualify in batch (ib_async handles this efficiently)
        qualified = await ib.qualifyContractsAsync(*contracts)

        # Request snapshots
        tickers_data = {}
        for qc in qualified:
            if qc.conId == 0:
                continue  # invalid contract
            td = ib.reqMktData(qc, '', True, False)  # snapshot=True
            tickers_data[qc.strike] = td

        # Wait briefly for snapshots to populate
        import asyncio
        await asyncio.sleep(2)

        # C6.2: NaN-safe coercion loop extracted to _build_chain_rows
        # as a pure module-level helper for unit testability. Preserves
        # the pre-C6.2 output shape and valid-data semantics exactly.
        results = _build_chain_rows(tickers_data)

        # Cancel market data subscriptions
        for td in tickers_data.values():
            try:
                ib.cancelMktData(td.contract)
            except Exception:
                pass

        latency = (time.time() - t0) * 1000
        _log_fetch(ticker, 'IBKR', latency, True)

        return results

    except IBKRChainError:
        raise
    except Exception as exc:
        latency = (time.time() - t0) * 1000
        error_class = _classify_error(exc)
        _log_fetch(ticker, 'IBKR', latency, False, error_class)
        raise IBKRChainError(
            f"Chain data fetch failed for {ticker} {expiry}: {exc}", error_class
        ) from exc


def _classify_error(exc: Exception) -> str:
    """Classify an exception into an error category."""
    msg = str(exc).lower()
    if 'connection' in msg or 'timeout' in msg or 'disconnect' in msg:
        return 'NETWORK'
    if 'market' in msg and 'closed' in msg:
        return 'MARKET_CLOSED'
    if 'no data' in msg or 'no security' in msg or 'ambiguous' in msg:
        return 'NO_DATA'
    if 'rate' in msg or 'pacing' in msg or 'too many' in msg:
        return 'RATE_LIMIT'
    return 'UNKNOWN'


# ---------------------------------------------------------------------------
# Spot price quotes (R4 Stage 2)
# ---------------------------------------------------------------------------

_spot_cache: dict[str, tuple[float, float]] = {}  # {ticker: (price, fetched_at)}
SPOT_CACHE_TTL = 30  # seconds


async def get_spot(ib, ticker: str) -> float:
    """Fetch a single spot price via IBKR reqMktData snapshot.

    Returns the best available price (last, close, or bid/ask mid).
    Raises IBKRChainError on failure. Cached 30s.
    """
    now = time.time()
    cached = _spot_cache.get(ticker)
    if cached and (now - cached[1]) < SPOT_CACHE_TTL:
        return cached[0]

    t0 = time.time()
    try:
        import asyncio
        import ib_async

        contract = ib_async.Stock(ticker, 'SMART', 'USD')
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            raise IBKRNoDataError(f"Could not qualify {ticker}")

        td = ib.reqMktData(qualified[0], '', False, False)
        await asyncio.sleep(2.0)

        # Extract best price (C6.2: NaN-safe via _safe_float)
        price = None
        for val in [td.last, td.close, td.marketPrice()]:
            safe_val = _safe_float(val)
            if safe_val > 0 and safe_val != float('inf'):
                price = safe_val
                break
        if price is None:
            safe_bid = _safe_float(td.bid)
            safe_ask = _safe_float(td.ask)
            if safe_bid > 0 and safe_ask > 0:
                price = (safe_bid + safe_ask) / 2.0

        try:
            ib.cancelMktData(qualified[0])
        except Exception:
            pass

        if price is None or price <= 0:
            raise IBKRNoDataError(f"No valid price for {ticker}")

        latency = (time.time() - t0) * 1000
        _log_fetch(ticker, 'IBKR', latency, True)
        _spot_cache[ticker] = (price, time.time())
        return price

    except IBKRChainError:
        raise
    except Exception as exc:
        latency = (time.time() - t0) * 1000
        error_class = _classify_error(exc)
        _log_fetch(ticker, 'IBKR', latency, False, error_class)
        raise IBKRChainError(f"Spot fetch failed for {ticker}: {exc}", error_class) from exc


async def get_spots_batch(ib, tickers: list[str]) -> dict[str, float]:
    """Batch fetch spot prices. Returns {ticker: price}.

    Fires all reqMktData in parallel, waits 2.5s, collects.
    Tickers that fail are omitted (no exception, logged).
    """
    if not tickers:
        return {}

    now = time.time()
    result = {}
    need_fetch = []

    # Check cache first
    for tk in tickers:
        cached = _spot_cache.get(tk)
        if cached and (now - cached[1]) < SPOT_CACHE_TTL:
            result[tk] = cached[0]
        else:
            need_fetch.append(tk)

    if not need_fetch:
        return result

    t0 = time.time()
    try:
        import asyncio
        import ib_async

        # Qualify all contracts
        contracts = [ib_async.Stock(tk, 'SMART', 'USD') for tk in need_fetch]
        qualified = await ib.qualifyContractsAsync(*contracts)

        # Fire all reqMktData
        ticker_map = {}  # contract -> ticker
        for qc, tk in zip(qualified, need_fetch):
            if qc.conId == 0:
                continue
            td = ib.reqMktData(qc, '', False, False)
            ticker_map[qc] = (tk, td)

        await asyncio.sleep(2.5)

        # Collect prices (C6.2: NaN-safe via _safe_float)
        for qc, (tk, td) in ticker_map.items():
            price = None
            for val in [td.last, td.close, td.marketPrice()]:
                safe_val = _safe_float(val)
                if safe_val > 0 and safe_val != float('inf'):
                    price = safe_val
                    break
            if price is None:
                safe_bid = _safe_float(td.bid)
                safe_ask = _safe_float(td.ask)
                if safe_bid > 0 and safe_ask > 0:
                    price = (safe_bid + safe_ask) / 2.0

            try:
                ib.cancelMktData(qc)
            except Exception:
                pass

            if price and price > 0:
                result[tk] = price
                _spot_cache[tk] = (price, time.time())

        latency = (time.time() - t0) * 1000
        _log_fetch(','.join(need_fetch[:5]), 'IBKR', latency, True)

    except Exception as exc:
        latency = (time.time() - t0) * 1000
        logger.warning("Batch spot fetch failed: %s", exc)
        _log_fetch(','.join(need_fetch[:5]), 'IBKR', latency, False, _classify_error(exc))

    return result
