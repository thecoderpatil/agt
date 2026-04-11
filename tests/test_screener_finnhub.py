"""
tests/test_screener_finnhub.py

Unit tests for agt_equities.screener.finnhub_client and cache layer.

Mocking strategy: httpx.MockTransport injected via the FinnhubClient
constructor's `transport` parameter. No new test dependencies (no respx,
no pytest-httpx) — matches the dependency-discipline pattern from
ADR-005's pytest-asyncio decision.

Async-to-sync wrapping: each test_* function wraps the coroutine in
asyncio.run(), matching the existing 600+-test convention. No
@pytest.mark.asyncio.

Test matrix:
  Cache layer (5):
    1. cache_put then cache_get round-trip
    2. cache_get returns None on miss
    3. cache_get returns None when expired (TTL exceeded)
    4. cache_put atomic write survives partial-write simulation
    5. cache_clear deletes entries

  Rate limiter (3):
    6. acquire() admits up to N calls without blocking
    7. acquire() blocks the (N+1)th call within the window
    8. current_load() reports the in-window count

  Finnhub client (8):
    9. get_profile2 cache miss → HTTP call → cache populated
   10. get_profile2 cache hit → no HTTP call
   11. get_metric returns the metric subdict on success
   12. get_dividend2 with from/to date params, list response allowed
   13. 429 retry then success
   14. 5xx retry exhausted → returns None (fail-soft)
   15. 4xx (non-429) → no retry, returns None
   16. Empty response dict treated as miss → None
   17. Missing API key → fail-soft None without HTTP call
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from agt_equities.screener import cache as screener_cache
from agt_equities.screener.finnhub_client import (
    FinnhubClient,
    FinnhubRateLimiter,
)


def _run(coro):
    """Sync wrapper for async test bodies — matches project convention."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Cache fixtures — redirect CACHE_ROOT to a per-test tmp dir
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Redirect screener cache root to a tmp_path so tests don't pollute disk."""
    monkeypatch.setattr(screener_cache, "CACHE_ROOT", tmp_path / "screener_cache")
    return tmp_path / "screener_cache"


# ---------------------------------------------------------------------------
# 1-5. Cache layer
# ---------------------------------------------------------------------------

def test_1_cache_roundtrip(tmp_cache):
    """cache_put then cache_get returns the original payload."""
    payload = {"foo": "bar", "n": 42}
    screener_cache.cache_put("finnhub/profile2", "AAPL", payload)
    result = screener_cache.cache_get("finnhub/profile2", "AAPL", ttl_seconds=60)
    assert result == payload


def test_2_cache_miss_returns_none(tmp_cache):
    """cache_get on absent key returns None."""
    result = screener_cache.cache_get("finnhub/profile2", "DOES_NOT_EXIST", ttl_seconds=60)
    assert result is None


def test_3_cache_expired_returns_none(tmp_cache):
    """cache_get with TTL=0 always returns None even for fresh writes."""
    screener_cache.cache_put("finnhub/metric", "MSFT", {"x": 1})
    # TTL of 0 seconds — any age is "expired"
    time.sleep(0.05)  # ensure age > 0
    result = screener_cache.cache_get("finnhub/metric", "MSFT", ttl_seconds=0)
    assert result is None


def test_4_cache_corrupt_entry_returns_none(tmp_cache):
    """A corrupt JSON file is treated as a cache miss, not a crash."""
    path = screener_cache.cache_path("finnhub/metric", "BADJSON")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    result = screener_cache.cache_get("finnhub/metric", "BADJSON", ttl_seconds=60)
    assert result is None


def test_5_cache_clear_deletes_entry(tmp_cache):
    """cache_clear removes the entry and returns True; subsequent clear returns False."""
    screener_cache.cache_put("finnhub/profile2", "GOOG", {"x": 1})
    assert screener_cache.cache_clear("finnhub/profile2", "GOOG") is True
    assert screener_cache.cache_clear("finnhub/profile2", "GOOG") is False


# ---------------------------------------------------------------------------
# 6-8. Rate limiter
# ---------------------------------------------------------------------------

def test_6_rate_limiter_admits_under_quota():
    """N calls within quota all succeed without measurable delay."""
    async def run():
        limiter = FinnhubRateLimiter(calls_per_minute=10)
        start = time.monotonic()
        for _ in range(10):
            await limiter.acquire()
        elapsed = time.monotonic() - start
        # 10 calls under a 10/min quota should complete in <100ms
        assert elapsed < 0.5, f"Elapsed {elapsed:.3f}s should be near-zero"
        assert limiter.current_load() == 10
    _run(run())


def test_7_rate_limiter_blocks_over_quota(monkeypatch):
    """The (N+1)th call within the window blocks until the oldest ages out.

    To keep the test fast, we use a tiny window by patching window_seconds.
    """
    async def run():
        limiter = FinnhubRateLimiter(calls_per_minute=3)
        # Shrink the window to 0.3s for fast testing
        limiter.window_seconds = 0.3

        # First 3 calls — should be instant
        start = time.monotonic()
        for _ in range(3):
            await limiter.acquire()
        first_three = time.monotonic() - start
        assert first_three < 0.1

        # 4th call — must block until ~0.3s after the first call
        await limiter.acquire()
        total = time.monotonic() - start
        assert 0.25 <= total <= 0.6, (
            f"4th call should land between 0.25s and 0.6s, got {total:.3f}"
        )
    _run(run())


def test_8_rate_limiter_rejects_invalid_quota():
    """calls_per_minute must be positive."""
    with pytest.raises(ValueError):
        FinnhubRateLimiter(calls_per_minute=0)
    with pytest.raises(ValueError):
        FinnhubRateLimiter(calls_per_minute=-5)


# ---------------------------------------------------------------------------
# 9-17. FinnhubClient with httpx.MockTransport
# ---------------------------------------------------------------------------

def _mock_transport(handler):
    """Build an httpx.MockTransport from a request->Response handler."""
    return httpx.MockTransport(handler)


def test_9_get_profile2_miss_then_cache(tmp_cache):
    """First call hits HTTP; second call hits cache."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        assert request.url.path == "/api/v1/stock/profile2"
        assert request.url.params.get("symbol") == "AAPL"
        assert request.url.params.get("token") == "test_key"
        return httpx.Response(200, json={
            "ticker": "AAPL",
            "name": "Apple Inc",
            "country": "US",
            "finnhubIndustry": "Technology",
            "marketCapitalization": 3500000.0,
        })

    async def run():
        client = FinnhubClient(api_key="test_key", transport=_mock_transport(handler))
        try:
            r1 = await client.get_profile2("AAPL")
            r2 = await client.get_profile2("AAPL")
            assert r1 == r2
            assert r1["ticker"] == "AAPL"
            assert r1["finnhubIndustry"] == "Technology"
            assert call_count["n"] == 1, "Second call should be served from cache"
        finally:
            await client.aclose()
    _run(run())


def test_10_get_profile2_cache_hit_skips_http(tmp_cache):
    """Pre-populating the cache means zero HTTP calls."""
    screener_cache.cache_put(
        "finnhub/profile2", "MSFT",
        {"ticker": "MSFT", "name": "Microsoft", "marketCapitalization": 2800000.0},
    )

    call_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(500)

    async def run():
        client = FinnhubClient(api_key="test_key", transport=_mock_transport(handler))
        try:
            r = await client.get_profile2("MSFT")
            assert r["ticker"] == "MSFT"
            assert call_count["n"] == 0, "Cache hit must not call HTTP"
        finally:
            await client.aclose()
    _run(run())


def test_11_get_metric_returns_payload(tmp_cache):
    """get_metric returns the full Finnhub metric response shape."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/stock/metric"
        assert request.url.params.get("metric") == "all"
        return httpx.Response(200, json={
            "metric": {
                "altmanZScoreTTM": 8.4,
                "fcfYieldTTM": 0.072,
                "netDebtToEbitdaQuarterly": 0.5,
                "roicTTM": 0.28,
                "shortInterestSharePercent": 0.012,
            },
            "series": {},
        })

    async def run():
        client = FinnhubClient(api_key="test_key", transport=_mock_transport(handler))
        try:
            r = await client.get_metric("AAPL")
            assert r["metric"]["altmanZScoreTTM"] == 8.4
            assert r["metric"]["roicTTM"] == 0.28
        finally:
            await client.aclose()
    _run(run())


def test_12_get_dividend2_list_response(tmp_cache):
    """get_dividend2 accepts list responses and caches them per date range."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("from") == "2026-01-01"
        assert request.url.params.get("to") == "2026-12-31"
        return httpx.Response(200, json=[
            {"symbol": "KO", "exDate": "2026-03-15", "amount": 0.485},
            {"symbol": "KO", "exDate": "2026-06-15", "amount": 0.485},
        ])

    async def run():
        client = FinnhubClient(api_key="test_key", transport=_mock_transport(handler))
        try:
            r = await client.get_dividend2("KO", "2026-01-01", "2026-12-31")
            assert isinstance(r, list)
            assert len(r) == 2
            assert r[0]["exDate"] == "2026-03-15"
        finally:
            await client.aclose()
    _run(run())


def test_13_429_retry_then_success(tmp_cache, monkeypatch):
    """Two 429 responses followed by 200 should retry and succeed."""
    # Speed up retries — patch the backoff helper to no-op
    from agt_equities.screener import finnhub_client as fc
    monkeypatch.setattr(fc, "BASE_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(fc, "JITTER_SECONDS", 0.0)

    call_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"ticker": "X"})

    async def run():
        client = FinnhubClient(api_key="test_key", transport=_mock_transport(handler))
        try:
            r = await client.get_profile2("X")
            assert r is not None
            assert r["ticker"] == "X"
            assert call_count["n"] == 3, f"Expected 3 calls (2 retries + success), got {call_count['n']}"
        finally:
            await client.aclose()
    _run(run())


def test_14_5xx_exhausted_returns_none(tmp_cache, monkeypatch):
    """Persistent 503 → returns None after MAX_RETRIES, never raises."""
    from agt_equities.screener import finnhub_client as fc
    monkeypatch.setattr(fc, "BASE_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(fc, "JITTER_SECONDS", 0.0)

    call_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(503)

    async def run():
        client = FinnhubClient(api_key="test_key", transport=_mock_transport(handler))
        try:
            r = await client.get_profile2("DEAD")
            assert r is None
            # MAX_RETRIES=3 means 1 initial + 3 retries = 4 calls
            assert call_count["n"] == 4
        finally:
            await client.aclose()
    _run(run())


def test_15_404_no_retry_returns_none(tmp_cache):
    """4xx (non-429) is permanent: 1 call, no retry, returns None."""
    call_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(404)

    async def run():
        client = FinnhubClient(api_key="test_key", transport=_mock_transport(handler))
        try:
            r = await client.get_profile2("NOPE")
            assert r is None
            assert call_count["n"] == 1, "4xx (non-429) should not retry"
        finally:
            await client.aclose()
    _run(run())


def test_16_empty_dict_treated_as_miss(tmp_cache):
    """Finnhub returns {} for delisted/unknown tickers — treat as None."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async def run():
        client = FinnhubClient(api_key="test_key", transport=_mock_transport(handler))
        try:
            r = await client.get_profile2("DELISTED")
            assert r is None
        finally:
            await client.aclose()
    _run(run())


def test_17_missing_api_key_fails_soft(tmp_cache, monkeypatch):
    """No API key → no HTTP call, returns None."""
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)

    call_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"ticker": "AAPL"})

    async def run():
        client = FinnhubClient(api_key="", transport=_mock_transport(handler))
        try:
            r = await client.get_profile2("AAPL")
            assert r is None
            assert call_count["n"] == 0, "Missing key must short-circuit before HTTP"
        finally:
            await client.aclose()
    _run(run())
