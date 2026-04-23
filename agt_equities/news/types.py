"""News overlay types — NewsItem dataclass + NewsAdapter Protocol.

Per ADR-CSP_NEWS_OVERLAY_v1. Every source adapter normalizes its native
payload into NewsItem. The aggregator (MR 3) consumes lists of NewsItem
across sources and produces a NewsBundle. The LLM commentary layer (MR 4)
consumes NewsBundle.

Truncation invariants enforced at construction:
    headline: <= 280 chars (Twitter-class headline length)
    summary:  <= 500 chars (max two short paragraphs)

NewsItem is frozen so adapter outputs cannot be mutated downstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

HEADLINE_MAX_CHARS = 280
SUMMARY_MAX_CHARS = 500


def _truncate(text: str | None, max_chars: int) -> str | None:
    """Truncate to max_chars; preserve None passthrough."""
    if text is None:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


@dataclass(frozen=True)
class NewsItem:
    """One normalized news / filing record from any source.

    Construction invariants (enforced via __post_init__):
      - headline truncated to HEADLINE_MAX_CHARS with a trailing ellipsis.
      - summary truncated to SUMMARY_MAX_CHARS (or None).
      - published_utc must be timezone-aware (raises if naive).

    Fields:
      source        adapter source name, e.g. "yfinance" | "finnhub_company"
                    | "finnhub_general" | "edgar_8k". Stable identifier the
                    aggregator uses for dedup keys.
      ticker        ticker the item is about; None for macro/general items.
      headline      <= HEADLINE_MAX_CHARS chars.
      summary       <= SUMMARY_MAX_CHARS chars; may be None for headline-only feeds.
      url           canonical source URL (used for dedup hash).
      published_utc timezone-aware UTC datetime.
      tag           optional source-specific tag; for 8-Ks one of
                    {"1.02", "2.04", "4.01", "4.02", "5.02"}; else None.
      raw_payload   adapter-native payload, debugging only. NEVER fed to LLM.
    """

    source: str
    ticker: str | None
    headline: str
    summary: str | None
    url: str
    published_utc: datetime
    tag: str | None = None
    raw_payload: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen=True forbids direct attribute assignment; use object.__setattr__
        if self.published_utc.tzinfo is None:
            raise ValueError(
                "NewsItem.published_utc must be timezone-aware "
                f"(got naive datetime for source={self.source!r})"
            )
        object.__setattr__(self, "headline", _truncate(self.headline, HEADLINE_MAX_CHARS))
        object.__setattr__(self, "summary", _truncate(self.summary, SUMMARY_MAX_CHARS))


@runtime_checkable
class NewsAdapter(Protocol):
    """Every source adapter conforms to this Protocol.

    Implementations:
      - YFinanceAdapter (this MR)
      - FinnhubCompanyAdapter / FinnhubGeneralAdapter (this MR via finnhub_client extension)
      - EdgarAdapter (next MR)
    """

    source: str

    async def fetch(
        self,
        ticker: str | None,
        *,
        lookback_hours: int = 24,
        timeout_s: float = 3.0,
    ) -> list[NewsItem]:
        """Return a list of NewsItem within the lookback window.

        Contract:
          - ticker=None means a macro/general fetch (only some adapters support).
          - lookback_hours bounds how far back to look; adapter may cap higher.
          - timeout_s is per-adapter; on timeout, return [] (fail-soft).
          - On any other error, log and return []. NEVER raises into the
            aggregator.
        """
        ...
