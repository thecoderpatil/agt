"""Finnhub adapter wrappers — convert raw Finnhub news payloads to NewsItem.

Per ADR-CSP_NEWS_OVERLAY_v1: finnhub_client returns raw dicts; the
adapter classes here normalize to NewsItem and conform to NewsAdapter
Protocol so the aggregator (MR 3) can fan out across all sources
uniformly.

Two adapters:
    FinnhubCompanyNewsAdapter — per-ticker news via /company-news
    FinnhubGeneralNewsAdapter — macro/general feed via /news?category=general

Both delegate to a shared FinnhubClient instance (passed in at construction)
so the rate limiter is shared across adapters within one digest run.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from agt_equities.news.types import NewsItem

if TYPE_CHECKING:
    from agt_equities.screener.finnhub_client import FinnhubClient

logger = logging.getLogger(__name__)


def _normalize_finnhub_item(
    raw: dict,
    *,
    ticker: str | None,
    source: str,
) -> NewsItem | None:
    """Convert one Finnhub news dict to NewsItem, or None if malformed."""
    if not isinstance(raw, dict):
        return None
    headline = raw.get("headline")
    url = raw.get("url")
    ts = raw.get("datetime")  # Finnhub uses epoch seconds
    if not headline or not url or ts is None:
        return None
    try:
        published = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    summary = raw.get("summary") or None
    return NewsItem(
        source=source,
        ticker=ticker,
        headline=str(headline),
        summary=str(summary) if summary else None,
        url=str(url),
        published_utc=published,
        tag=None,
        raw_payload=raw,
    )


class FinnhubCompanyNewsAdapter:
    """Per-ticker Finnhub /company-news adapter.

    source = "finnhub_company".
    Macro/general fetch: NOT supported (ticker=None returns []).
    """

    source = "finnhub_company"

    def __init__(self, client: "FinnhubClient") -> None:
        self._client = client

    async def fetch(
        self,
        ticker: str | None,
        *,
        lookback_hours: int = 24,
        timeout_s: float = 3.0,
    ) -> list[NewsItem]:
        if ticker is None:
            return []
        # timeout_s honored at HTTP layer in finnhub_client (READ_TIMEOUT etc).
        # Per-call timeout enforcement at adapter level is best-effort: the
        # underlying client's retry+backoff loop has its own bounds.
        today = datetime.now(timezone.utc).date()
        from_d: date = today - timedelta(days=max(1, (lookback_hours + 23) // 24))
        try:
            raw = await self._client.get_company_news(
                ticker, from_date=from_d.isoformat(), to_date=today.isoformat(),
            )
        except Exception as exc:
            logger.warning(
                "finnhub_company_adapter ticker=%s err=%s", ticker, exc
            )
            return []
        if not raw:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        items: list[NewsItem] = []
        for entry in raw:
            item = _normalize_finnhub_item(entry, ticker=ticker, source=self.source)
            if item is not None and item.published_utc >= cutoff:
                items.append(item)
        return items


class FinnhubGeneralNewsAdapter:
    """Macro Finnhub /news?category=general adapter.

    source = "finnhub_general".
    Per-ticker fetch: NOT supported in this adapter — call with ticker=None.
    """

    source = "finnhub_general"

    def __init__(self, client: "FinnhubClient", *, category: str = "general") -> None:
        self._client = client
        self._category = category

    async def fetch(
        self,
        ticker: str | None,
        *,
        lookback_hours: int = 24,
        timeout_s: float = 3.0,
    ) -> list[NewsItem]:
        # Macro feed ignores ticker. Caller MUST pass None to make intent
        # explicit; calling with a ticker is a contract violation we surface
        # by returning empty (rather than fetching wrong data).
        if ticker is not None:
            logger.warning(
                "finnhub_general_adapter called with ticker=%s; ignoring",
                ticker,
            )
            return []
        try:
            raw = await self._client.get_general_news(category=self._category)
        except Exception as exc:
            logger.warning("finnhub_general_adapter err=%s", exc)
            return []
        if not raw:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        items: list[NewsItem] = []
        for entry in raw:
            item = _normalize_finnhub_item(entry, ticker=None, source=self.source)
            if item is not None and item.published_utc >= cutoff:
                items.append(item)
        return items
