"""
agt_equities.screener.finnhub_client — Async Finnhub HTTP wrapper.

Responsibilities:
  1. Token-bucket rate limiter (250 calls/min sustained, 83% of Personal
     tier's 300/min ceiling — leaves headroom for retries and bursts).
  2. Single shared httpx.AsyncClient with HTTP/2 + connection pooling.
  3. Per-call cache layer (24h TTL) backed by agt_equities.screener.cache.
  4. Exponential backoff retry on 429 / 5xx (3 retries, base 2s, ±500ms jitter).
  5. Fail-soft per call: returns None on terminal failure, never raises
     into the screener orchestrator. Caller logs and skips the ticker.

Endpoints exposed (Phase 1, 2, 4 of the spec):
  get_profile2(ticker)              → /stock/profile2
  get_metric(ticker)                → /stock/metric?metric=all
  get_dividend2(ticker, frm, to)    → /stock/dividend2

API key sourced from FINNHUB_API_KEY env var. Never logged. If absent,
client construction logs a single warning and all requests fail-soft to
None (the screener falls back to cached data and reports the gap to the
operator via the /screener output banner).

ISOLATION CONTRACT:
This module imports only stdlib + httpx + agt_equities.screener.cache.
It does NOT import telegram_bot, ib_async, V2 router functions, or any
execution-path symbol. Enforced by tests/test_screener_isolation.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import deque
from typing import Any

import httpx

from agt_equities.screener import cache as screener_cache

logger = logging.getLogger(__name__)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

# Rate limit budget — 50/min sustained (17% safety headroom under the
# Finnhub FREE tier ceiling of 60/min).
#
# Architect ruling 2026-04-10: run below the literal Free tier ceiling
# rather than at it. With MAX_RETRIES=3 and exponential backoff, a single
# failing call can fire up to 4 requests in a tight burst window; the
# 10-call/min headroom absorbs that burst without tripping Finnhub's
# server-side limiter, which is known to escalate repeated 429s into
# minute-long bans on the Free tier.
#
# Wall-clock cost of the safety margin: at 50/min, 517 tickers take
# ~10.3 min for a Phase 1 pass vs ~8.6 min at 60/min — about 100 seconds
# extra for a job that runs once daily at 06:00 ET. Cheap insurance.
#
# Tier upgrade path: bumping this to 250 unlocks Personal tier behavior
# (300/min ceiling) without any other code changes. The endpoints exposed
# below (profile2, dividend2) are all available on the Free tier; get_metric
# is dormant code retained for forward-compat with a future Personal tier
# upgrade.
DEFAULT_CALLS_PER_MINUTE = 50

# Cache TTLs by endpoint category (seconds)
TTL_PROFILE2 = 24 * 60 * 60      # 24h — sector/country/MC change rarely
TTL_METRIC = 24 * 60 * 60        # 24h — fundamentals refresh quarterly
TTL_DIVIDEND2 = 24 * 60 * 60     # 24h — ex-div dates publish well in advance

# Retry policy
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2.0
JITTER_SECONDS = 0.5

# HTTP timeouts
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0


class FinnhubRateLimiter:
    """Token-bucket-equivalent rate limiter using a sliding deque window.

    Tracks the timestamps of the last N calls and blocks new acquisitions
    until the oldest call falls outside the 60-second window. Async-safe
    via asyncio.Lock.

    Deterministic and simple: every call to acquire() either returns
    immediately (if under quota) or sleeps for exactly the amount needed
    to bring the window back into compliance. No background tasks, no
    timer threads.
    """

    def __init__(self, calls_per_minute: int = DEFAULT_CALLS_PER_MINUTE):
        if calls_per_minute <= 0:
            raise ValueError("calls_per_minute must be positive")
        self.calls_per_minute = calls_per_minute
        self.window_seconds = 60.0
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until permission to make one API call is granted."""
        async with self._lock:
            now = time.monotonic()
            # Drop timestamps outside the 60s window
            cutoff = now - self.window_seconds
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) >= self.calls_per_minute:
                # Sleep until the oldest in-window call ages out
                sleep_for = self.window_seconds - (now - self._timestamps[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                    # Recompute window after sleep
                    now = time.monotonic()
                    cutoff = now - self.window_seconds
                    while self._timestamps and self._timestamps[0] < cutoff:
                        self._timestamps.popleft()

            self._timestamps.append(now)

    def current_load(self) -> int:
        """Return number of calls in the current 60s window. Diagnostic only."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        return sum(1 for t in self._timestamps if t >= cutoff)


class FinnhubClient:
    """Async Finnhub API wrapper with rate limiting + caching + retry.

    Construct once per process. The client owns a single httpx.AsyncClient
    and a single FinnhubRateLimiter, both shared across all calls.

    Usage:
        async with FinnhubClient() as client:
            profile = await client.get_profile2("AAPL")
            metric = await client.get_metric("AAPL")

    The async-context-manager pattern is recommended but not required;
    callers may also explicitly call `await client.aclose()`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        calls_per_minute: int = DEFAULT_CALLS_PER_MINUTE,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        # Resolve API key from env if not explicitly passed
        self.api_key = api_key or os.environ.get("FINNHUB_API_KEY", "").strip()
        if not self.api_key:
            logger.warning(
                "FinnhubClient: FINNHUB_API_KEY not set. All requests will "
                "fail-soft to None and the screener will rely on cached data."
            )

        self.rate_limiter = FinnhubRateLimiter(calls_per_minute=calls_per_minute)

        # C2.1: cache hit/miss counters for run-level instrumentation.
        # Incremented inside _get_cached(): hit when the TTL-checked
        # cache returns a stored value, miss when a successful HTTP
        # fetch is then written to cache. Failed fetches and missing
        # API key paths do NOT increment misses (they're not cache
        # decisions, they're upstream failures).
        self._cache_hits: int = 0
        self._cache_misses: int = 0

        # httpx client — single shared instance for connection pooling.
        # transport= override is the test-injection point (httpx.MockTransport).
        client_kwargs: dict[str, Any] = {
            "base_url": FINNHUB_BASE_URL,
            "timeout": httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=READ_TIMEOUT),
            "headers": {"User-Agent": "agt-equities-screener/1.0"},
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def __aenter__(self) -> "FinnhubClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        try:
            await self._client.aclose()
        except Exception as exc:
            logger.warning("FinnhubClient.aclose failed: %s", exc)

    def get_stats(self) -> dict:
        """Return cache hit/miss counters and computed hit rate.

        Used by Phase 1 / Phase 4 final-log instrumentation to surface
        cache effectiveness in operator-facing run summaries. Hit rate
        is the ratio of cache_hits to (cache_hits + cache_misses); if
        no cache decisions have been made yet, returns 0.0.

        Failed fetches (network errors, 429-exhausted, missing API key)
        are NOT counted in either bucket — they're upstream failures
        that bypass the cache decision entirely.
        """
        total = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total * 100) if total else 0.0
        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate_pct": round(hit_rate, 1),
        }

    # ------------------------------------------------------------------
    # Public endpoint methods — each follows the same shape:
    #   1. Cache lookup (TTL-checked)
    #   2. On miss: rate-limited HTTP call with retry
    #   3. On success: cache write + return
    #   4. On terminal failure: log warning, return None
    # ------------------------------------------------------------------

    async def get_profile2(self, ticker: str) -> dict | None:
        """Fetch /stock/profile2 — sector, country, market cap, name.

        Returns the JSON dict on success, None on failure or empty response.
        Empty response from Finnhub (`{}`) is treated as a miss (e.g. delisted
        ticker) and returned as None.
        """
        return await self._get_cached(
            category="finnhub/profile2",
            key=ticker,
            ttl=TTL_PROFILE2,
            endpoint="/stock/profile2",
            params={"symbol": ticker},
        )

    async def get_metric(self, ticker: str) -> dict | None:
        """Fetch /stock/metric?metric=all — pre-computed financial metrics.

        Returns the full response dict including `metric` and `series` subkeys.
        The `metric` subdict contains the Phase 2 fortress fields:
            altmanZScoreTTM, fcfYieldTTM, netDebtToEbitdaQuarterly, roicTTM,
            shortInterestSharePercent (Phase 1)
        """
        return await self._get_cached(
            category="finnhub/metric",
            key=ticker,
            ttl=TTL_METRIC,
            endpoint="/stock/metric",
            params={"symbol": ticker, "metric": "all"},
        )

    async def get_dividend2(
        self, ticker: str, from_date: str, to_date: str,
    ) -> list[dict] | None:
        """Fetch /stock/dividend2 — historical and upcoming dividends.

        Args:
            ticker: ticker symbol
            from_date: ISO date YYYY-MM-DD
            to_date: ISO date YYYY-MM-DD

        Returns a list of dividend records (sorted by ex-date ASC). Empty
        list means the ticker pays no dividends in the window — that's a
        valid response, not a failure.

        Cache key includes the date range so different windows don't collide.
        """
        cache_key = f"{ticker}_{from_date}_{to_date}"
        return await self._get_cached(
            category="finnhub/dividend2",
            key=cache_key,
            ttl=TTL_DIVIDEND2,
            endpoint="/stock/dividend2",
            params={"symbol": ticker, "from": from_date, "to": to_date},
            allow_list_response=True,
        )

    # ------------------------------------------------------------------
    # Internal: cached + retried request
    # ------------------------------------------------------------------

    async def _get_cached(
        self,
        category: str,
        key: str,
        ttl: int,
        endpoint: str,
        params: dict,
        allow_list_response: bool = False,
    ) -> Any:
        """Cache-first GET with rate limiting + retry."""
        # Step 1: cache lookup
        cached = screener_cache.cache_get(category, key, ttl_seconds=ttl)
        if cached is not None:
            self._cache_hits += 1
            return cached

        # Step 2: API call (rate-limited, retried)
        if not self.api_key:
            return None  # fail-soft when key is missing (not a cache miss)

        result = await self._request_with_retry(
            endpoint, params, allow_list_response=allow_list_response,
        )
        if result is None:
            return None  # upstream failure, not a cache miss

        # Step 3: cache the response — counts as a miss because we had
        # to fetch fresh data and populate the cache for next time
        screener_cache.cache_put(category, key, result)
        self._cache_misses += 1
        return result

    async def _request_with_retry(
        self,
        endpoint: str,
        params: dict,
        allow_list_response: bool = False,
    ) -> Any:
        """Execute one request with retry on 429/5xx."""
        # Inject API key as query param (Finnhub's standard auth method)
        params_with_key = dict(params)
        params_with_key["token"] = self.api_key

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            await self.rate_limiter.acquire()
            try:
                resp = await self._client.get(endpoint, params=params_with_key)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    await self._backoff_sleep(attempt)
                    continue
                logger.warning(
                    "Finnhub %s %s: HTTP error after %d retries: %s",
                    endpoint, params.get("symbol", "?"), MAX_RETRIES, exc,
                )
                return None

            # Retryable status codes: 429 + 5xx
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt < MAX_RETRIES:
                    await self._backoff_sleep(attempt)
                    continue
                logger.warning(
                    "Finnhub %s %s: status %d after %d retries",
                    endpoint, params.get("symbol", "?"), resp.status_code, MAX_RETRIES,
                )
                return None

            # Permanent failures (4xx other than 429): no retry, return None
            if resp.status_code >= 400:
                logger.warning(
                    "Finnhub %s %s: status %d (no retry)",
                    endpoint, params.get("symbol", "?"), resp.status_code,
                )
                return None

            # 2xx success
            try:
                payload = resp.json()
            except ValueError as exc:
                logger.warning(
                    "Finnhub %s %s: JSON decode failed: %s",
                    endpoint, params.get("symbol", "?"), exc,
                )
                return None

            # Empty dict from Finnhub means "no data" (e.g. delisted ticker)
            if isinstance(payload, dict) and not payload:
                return None
            # Empty list is a valid response for dividend2 (no divs in window)
            if isinstance(payload, list) and not allow_list_response:
                logger.warning(
                    "Finnhub %s %s: unexpected list response", endpoint, params.get("symbol", "?"),
                )
                return None

            return payload

        # Loop exited without returning — defensive
        if last_exc:
            logger.warning("Finnhub retry loop exhausted: %s", last_exc)
        return None

    async def _backoff_sleep(self, attempt: int) -> None:
        """Exponential backoff with jitter."""
        delay = BASE_BACKOFF_SECONDS * (2 ** attempt)
        jitter = random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
        await asyncio.sleep(max(0.1, delay + jitter))
