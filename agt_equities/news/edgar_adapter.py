"""SEC EDGAR 8-K adapter — wraps EdgarClient, normalizes to NewsItem.

Per ADR-CSP_NEWS_OVERLAY_v1. Conforms to NewsAdapter Protocol.
source = "edgar_8k". Macro fetch NOT supported (8-Ks are per-company).
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone

from agt_equities.news.edgar_client import EdgarClient
from agt_equities.news.types import NewsItem

logger = logging.getLogger(__name__)


class EdgarAdapter:
    """EDGAR 8-K adapter. tag = 8-K item code (e.g., '1.02', '5.02')."""

    source = "edgar_8k"

    def __init__(self, client: EdgarClient | None = None) -> None:
        self._client = client or EdgarClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(
        self,
        ticker: str | None,
        *,
        lookback_hours: int = 72,  # 8-Ks lower volume; default longer window
        timeout_s: float = 8.0,
    ) -> list[NewsItem]:
        if ticker is None:
            return []
        try:
            filings = await self._client.fetch_8k_filings(
                ticker, lookback_hours=lookback_hours,
            )
        except Exception as exc:
            logger.warning("edgar_adapter ticker=%s err=%s", ticker, exc)
            return []
        items: list[NewsItem] = []
        for filing in filings:
            # Filings give us a date; promote to UTC midday for ordering.
            published = datetime.combine(
                filing["filed_date"], time(12, 0), tzinfo=timezone.utc,
            )
            for item_code in filing["items"]:
                items.append(NewsItem(
                    source="edgar_8k",
                    ticker=ticker.upper(),
                    headline=f"8-K Item {item_code} — {ticker.upper()}",
                    summary=(
                        f"SEC 8-K filed {filing['filed_date'].isoformat()} "
                        f"with item {item_code}. Accession "
                        f"{filing['accession_number']}."
                    ),
                    url=filing.get("primary_document_url") or "",
                    published_utc=published,
                    tag=item_code,
                    raw_payload=filing,
                ))
        return items
