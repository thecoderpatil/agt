"""News aggregator — fan out to all adapters, dedup, cache via news_cache table.

Per ADR-CSP_NEWS_OVERLAY_v1 section "agt_equities/news/aggregator.py".
Sole entry point for the digest layer. Returns NewsBundle keyed by
ticker + "_macro" reserved key.

Cache layer: news_cache table (created by scripts/migrate_news_cache.py).
1h TTL default; cache key namespaces source + ticker (or "_macro") +
lookback bucket.

Fail-soft: any single adapter failure becomes empty items from that
source; sources_failed lists what didn't return. Aggregator NEVER
raises into the digest layer.

Concurrency: asyncio.Semaphore(10) caps concurrent adapter calls
across all ticker fan-outs to honor Finnhub rate budget (60/min free
tier shared with screener ~50/min).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from agt_equities.news.types import NewsItem

logger = logging.getLogger(__name__)

MACRO_KEY = "_macro"
DEFAULT_CACHE_TTL_S = 3600
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_CONCURRENCY = 10
DEFAULT_PER_ADAPTER_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class NewsBundle:
    """Merged + deduped NewsItem list for one ticker (or macro).

    sources_ok and sources_failed are audit fields; the digest layer
    can render "news partially unavailable" if sources_failed non-empty.
    """

    ticker: str | None
    items: list[NewsItem]
    fetched_at_utc: datetime
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)


def _dedup(items: Iterable[NewsItem]) -> list[NewsItem]:
    """Drop duplicates by (ticker, source, url) — sort desc by published_utc."""
    seen: set[tuple] = set()
    out: list[NewsItem] = []
    for it in items:
        key = (it.ticker, it.source, it.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    out.sort(key=lambda x: x.published_utc, reverse=True)
    return out


def _cache_key(source: str, ticker: str | None, lookback_hours: int) -> str:
    bucket = ticker if ticker is not None else MACRO_KEY
    return f"{source}:{bucket}:{lookback_hours}h"


def _read_cache(
    db_path: str | Path,
    cache_key: str,
    cache_ttl_s: int,
) -> list[NewsItem] | None:
    """Return cached items if fresh, else None. Fail-soft on any DB error."""
    try:
        with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
            row = conn.execute(
                "SELECT items_json, fetched_at_utc, ttl_seconds "
                "FROM news_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        logger.warning("news_cache.read_err key=%s err=%s", cache_key, exc)
        return None
    if not row:
        return None
    items_json, fetched_at_utc, ttl_seconds = row
    try:
        fetched = datetime.fromisoformat(fetched_at_utc)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    if age >= min(cache_ttl_s, ttl_seconds):
        return None
    try:
        raw = json.loads(items_json)
    except json.JSONDecodeError:
        return None
    return [_news_item_from_dict(d) for d in raw if isinstance(d, dict)]


def _write_cache(
    db_path: str | Path,
    cache_key: str,
    source: str,
    ticker: str | None,
    lookback_hours: int,
    items: Sequence[NewsItem],
    cache_ttl_s: int,
) -> None:
    """Write items to news_cache. Fail-soft on any DB error."""
    payload = json.dumps([_news_item_to_dict(it) for it in items], default=str)
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO news_cache "
                "(cache_key, source, ticker, lookback_hours, items_json, "
                " fetched_at_utc, ttl_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cache_key, source, ticker, lookback_hours, payload, now_iso, cache_ttl_s),
            )
            conn.commit()
    except sqlite3.OperationalError as exc:
        logger.warning("news_cache.write_err key=%s err=%s", cache_key, exc)


def _news_item_to_dict(it: NewsItem) -> dict:
    return {
        "source": it.source,
        "ticker": it.ticker,
        "headline": it.headline,
        "summary": it.summary,
        "url": it.url,
        "published_utc": it.published_utc.isoformat(),
        "tag": it.tag,
    }


def _news_item_from_dict(d: dict) -> NewsItem:
    pub = d["published_utc"]
    if isinstance(pub, str):
        dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = pub
    return NewsItem(
        source=d["source"],
        ticker=d.get("ticker"),
        headline=d["headline"],
        summary=d.get("summary"),
        url=d["url"],
        published_utc=dt,
        tag=d.get("tag"),
        raw_payload={},
    )


async def _fetch_one_adapter(
    adapter,
    ticker: str | None,
    *,
    lookback_hours: int,
    timeout_s: float,
    sema: asyncio.Semaphore,
    db_path: str | Path | None,
    cache_ttl_s: int,
) -> tuple[str, list[NewsItem], bool]:
    """Fetch one adapter call (cache-or-live). Returns (source, items, ok)."""
    cache_key = _cache_key(adapter.source, ticker, lookback_hours)
    if db_path is not None:
        cached = _read_cache(db_path, cache_key, cache_ttl_s)
        if cached is not None:
            return (adapter.source, cached, True)
    async with sema:
        try:
            items = await adapter.fetch(
                ticker, lookback_hours=lookback_hours, timeout_s=timeout_s,
            )
        except Exception as exc:
            logger.warning(
                "aggregator.adapter_err source=%s ticker=%s err=%s",
                adapter.source, ticker, exc,
            )
            return (adapter.source, [], False)
    if db_path is not None and items:
        _write_cache(
            db_path, cache_key, adapter.source, ticker, lookback_hours,
            items, cache_ttl_s,
        )
    return (adapter.source, items, True)


async def fetch_news_bundle(
    ticker: str,
    adapters,
    *,
    db_path: str | Path | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
    timeout_s: float = DEFAULT_PER_ADAPTER_TIMEOUT_S,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> NewsBundle:
    """Fetch all per-ticker adapters and return a merged NewsBundle.

    `adapters` is an iterable of NewsAdapter conformers. Macro-only
    adapters (e.g., FinnhubGeneralNewsAdapter) should be excluded by
    the caller.
    """
    sema = asyncio.Semaphore(concurrency)
    coros = [
        _fetch_one_adapter(
            a, ticker, lookback_hours=lookback_hours, timeout_s=timeout_s,
            sema=sema, db_path=db_path, cache_ttl_s=cache_ttl_s,
        )
        for a in adapters
    ]
    results = await asyncio.gather(*coros, return_exceptions=False)
    items: list[NewsItem] = []
    sources_ok: list[str] = []
    sources_failed: list[str] = []
    for source, src_items, ok in results:
        if ok:
            sources_ok.append(source)
            items.extend(src_items)
        else:
            sources_failed.append(source)
    return NewsBundle(
        ticker=ticker,
        items=_dedup(items),
        fetched_at_utc=datetime.now(timezone.utc),
        sources_ok=sources_ok,
        sources_failed=sources_failed,
    )


async def fetch_macro_bundle(
    macro_adapters,
    *,
    db_path: str | Path | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
    timeout_s: float = DEFAULT_PER_ADAPTER_TIMEOUT_S,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> NewsBundle:
    """Same as fetch_news_bundle but for macro/general (ticker=None)."""
    sema = asyncio.Semaphore(concurrency)
    coros = [
        _fetch_one_adapter(
            a, None, lookback_hours=lookback_hours, timeout_s=timeout_s,
            sema=sema, db_path=db_path, cache_ttl_s=cache_ttl_s,
        )
        for a in macro_adapters
    ]
    results = await asyncio.gather(*coros, return_exceptions=False)
    items: list[NewsItem] = []
    sources_ok: list[str] = []
    sources_failed: list[str] = []
    for source, src_items, ok in results:
        if ok:
            sources_ok.append(source)
            items.extend(src_items)
        else:
            sources_failed.append(source)
    return NewsBundle(
        ticker=None,
        items=_dedup(items),
        fetched_at_utc=datetime.now(timezone.utc),
        sources_ok=sources_ok,
        sources_failed=sources_failed,
    )


async def fetch_bundles(
    tickers: Sequence[str],
    *,
    per_ticker_adapters,
    macro_adapters=(),
    db_path: str | Path | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
    timeout_s: float = DEFAULT_PER_ADAPTER_TIMEOUT_S,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict[str, NewsBundle]:
    """Fan out per-ticker + macro bundles in one orchestrated call.

    Returns dict keyed by ticker (uppercase) plus reserved "_macro"
    key for the macro bundle (always present even if no macro adapters
    supplied — bundle will have empty items in that case).
    """
    sema = asyncio.Semaphore(concurrency)

    async def _one_ticker(ticker: str) -> NewsBundle:
        coros = [
            _fetch_one_adapter(
                a, ticker, lookback_hours=lookback_hours, timeout_s=timeout_s,
                sema=sema, db_path=db_path, cache_ttl_s=cache_ttl_s,
            )
            for a in per_ticker_adapters
        ]
        results = await asyncio.gather(*coros, return_exceptions=False)
        items: list[NewsItem] = []
        sources_ok: list[str] = []
        sources_failed: list[str] = []
        for source, src_items, ok in results:
            if ok:
                sources_ok.append(source)
                items.extend(src_items)
            else:
                sources_failed.append(source)
        return NewsBundle(
            ticker=ticker,
            items=_dedup(items),
            fetched_at_utc=datetime.now(timezone.utc),
            sources_ok=sources_ok,
            sources_failed=sources_failed,
        )

    async def _macro() -> NewsBundle:
        if not macro_adapters:
            return NewsBundle(
                ticker=None, items=[], fetched_at_utc=datetime.now(timezone.utc),
            )
        coros = [
            _fetch_one_adapter(
                a, None, lookback_hours=lookback_hours, timeout_s=timeout_s,
                sema=sema, db_path=db_path, cache_ttl_s=cache_ttl_s,
            )
            for a in macro_adapters
        ]
        results = await asyncio.gather(*coros, return_exceptions=False)
        items: list[NewsItem] = []
        sources_ok: list[str] = []
        sources_failed: list[str] = []
        for source, src_items, ok in results:
            if ok:
                sources_ok.append(source)
                items.extend(src_items)
            else:
                sources_failed.append(source)
        return NewsBundle(
            ticker=None, items=_dedup(items),
            fetched_at_utc=datetime.now(timezone.utc),
            sources_ok=sources_ok, sources_failed=sources_failed,
        )

    upper_tickers = [t.upper() for t in tickers]
    per_ticker_results = await asyncio.gather(
        *[_one_ticker(t) for t in upper_tickers]
    )
    macro_result = await _macro()
    out: dict[str, NewsBundle] = {b.ticker: b for b in per_ticker_results}
    out[MACRO_KEY] = macro_result
    return out


def prune_news_cache(
    db_path: str | Path,
    *,
    keep_last_days: int = 7,
) -> int:
    """Delete news_cache rows older than keep_last_days. Returns rows deleted."""
    try:
        with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
            cur = conn.execute(
                "DELETE FROM news_cache WHERE julianday('now') - "
                "julianday(fetched_at_utc) > ?",
                (keep_last_days,),
            )
            n = cur.rowcount
            conn.commit()
            return n
    except sqlite3.OperationalError as exc:
        logger.warning("news_cache.prune_err err=%s", exc)
        return 0
