"""yfinance.Ticker(x).news adapter.

Per ADR-CSP_NEWS_OVERLAY_v1 section "Adapters shipped in this MR" item 2.
yfinance is sync-only, so we wrap in ThreadPoolExecutor with a strict
timeout. Pattern lifted from pxo_scanner.py:_fetch_latest_headline and
generalized to return a list of NewsItem.

Fail-soft: timeout, network error, or malformed payload returns [].
Never raises into the aggregator.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta, timezone

from agt_equities.news.types import NewsItem

logger = logging.getLogger(__name__)


class YFinanceAdapter:
    """yfinance.Ticker(ticker).news wrapper.

    Source identifier: "yfinance".
    Macro/general fetch: NOT supported (ticker=None returns []).
    Lookback honored via published_utc filter on returned items.
    """

    source = "yfinance"

    def __init__(self, executor: ThreadPoolExecutor | None = None) -> None:
        # Optional shared executor for tests; otherwise per-call disposable.
        self._executor = executor

    async def fetch(
        self,
        ticker: str | None,
        *,
        lookback_hours: int = 24,
        timeout_s: float = 3.0,
    ) -> list[NewsItem]:
        if ticker is None:
            # yfinance has no macro/general endpoint — return empty silently.
            return []

        loop = asyncio.get_running_loop()
        executor = self._executor or ThreadPoolExecutor(max_workers=1)
        owns_executor = self._executor is None

        try:
            future = loop.run_in_executor(executor, self._fetch_sync, ticker)
            raw = await asyncio.wait_for(future, timeout=timeout_s)
        except (asyncio.TimeoutError, FuturesTimeout):
            logger.warning(
                "yfinance_adapter ticker=%s timeout=%.1fs", ticker, timeout_s
            )
            return []
        except Exception as exc:
            logger.warning("yfinance_adapter ticker=%s err=%s", ticker, exc)
            return []
        finally:
            if owns_executor:
                executor.shutdown(wait=False)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        return [
            item for item in self._normalize(ticker, raw)
            if item.published_utc >= cutoff
        ]

    @staticmethod
    def _fetch_sync(ticker: str) -> list[dict]:
        """Synchronous yfinance call. Runs in thread pool."""
        import yfinance as yf  # late import — keeps yfinance optional at module load

        result = yf.Ticker(ticker).news
        if not isinstance(result, list):
            return []
        return result

    @staticmethod
    def _normalize(ticker: str, raw: list[dict]) -> list[NewsItem]:
        """Convert yfinance raw payload list to NewsItem list.

        yfinance >=0.2.28 nests article fields under a "content" key.
        Older formats had keys at top level. Handle both.
        """
        items: list[NewsItem] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            payload = entry.get("content") if isinstance(entry.get("content"), dict) else entry
            headline = payload.get("title") or payload.get("headline")
            if not headline:
                continue
            url = (
                payload.get("canonicalUrl", {}).get("url")
                if isinstance(payload.get("canonicalUrl"), dict)
                else payload.get("link") or payload.get("url")
            )
            if not url:
                continue
            summary = payload.get("summary") or payload.get("description")
            ts = (
                payload.get("pubDate")
                or payload.get("displayTime")
                or payload.get("providerPublishTime")
            )
            published = _parse_timestamp(ts)
            if published is None:
                continue
            items.append(
                NewsItem(
                    source="yfinance",
                    ticker=ticker,
                    headline=str(headline),
                    summary=str(summary) if summary else None,
                    url=str(url),
                    published_utc=published,
                    tag=None,
                    raw_payload=entry,
                )
            )
        return items


def _parse_timestamp(value) -> datetime | None:
    """Best-effort UTC datetime from yfinance's heterogeneous timestamp formats."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            iso = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return None
