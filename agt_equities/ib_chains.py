"""
agt_equities.ib_chains — IBKR option chain fetcher via ib_async.

Replaces yfinance option chain calls for EXECUTION_CRITICAL paths.
Fail-loudly: never falls through to yfinance on IBKR failure.

Cache: 5-minute TTL per ticker for expirations, 60s for chain data.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "agt_desk.db"

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

        # Build contracts and request market data
        contracts = []
        for strike in strikes:
            c = ib_async.Option(ticker, expiry_ib, strike, right, 'SMART')
            contracts.append(c)

        # Qualify in batch (ib_async handles this efficiently)
        qualified = await ib.qualifyContractsAsync(*contracts)

        # Request snapshots
        results = []
        tickers_data = {}
        for qc in qualified:
            if qc.conId == 0:
                continue  # invalid contract
            td = ib.reqMktData(qc, '', True, False)  # snapshot=True
            tickers_data[qc.strike] = td

        # Wait briefly for snapshots to populate
        import asyncio
        await asyncio.sleep(2)

        for strike, td in sorted(tickers_data.items()):
            bid = td.bid if td.bid and td.bid > 0 else 0.0
            ask = td.ask if td.ask and td.ask > 0 else 0.0
            last = td.last if td.last and td.last > 0 else 0.0
            vol = td.volume if td.volume else 0
            oi = td.openInterest if td.openInterest else 0

            results.append({
                'strike': float(strike),
                'bid': float(bid),
                'ask': float(ask),
                'last': float(last),
                'volume': int(vol),
                'openInterest': int(oi),
                'impliedVol': float(td.impliedVolatility or 0),
            })

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

        # Extract best price
        price = None
        for val in [td.last, td.close, td.marketPrice()]:
            if val is not None and val > 0 and val != float('inf'):
                price = float(val)
                break
        if price is None and td.bid and td.ask and td.bid > 0 and td.ask > 0:
            price = (float(td.bid) + float(td.ask)) / 2.0

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

        # Collect prices
        for qc, (tk, td) in ticker_map.items():
            price = None
            for val in [td.last, td.close, td.marketPrice()]:
                if val is not None and val > 0 and val != float('inf'):
                    price = float(val)
                    break
            if price is None and td.bid and td.ask and td.bid > 0 and td.ask > 0:
                price = (float(td.bid) + float(td.ask)) / 2.0

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
