"""Unit tests for news overlay adapters — yfinance + finnhub.

Per ADR-CSP_NEWS_OVERLAY_v1 testing strategy: each adapter mocked at the
HTTP/library boundary. No live network. Asserts NewsItem shape,
truncation, error-swallow (fail-soft) behavior, and lookback filtering.

Adapters under test:
  - YFinanceAdapter (yfinance.Ticker(x).news, ThreadPoolExecutor wrapped)
  - FinnhubCompanyNewsAdapter (Finnhub /company-news)
  - FinnhubGeneralNewsAdapter (Finnhub /news?category=general)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agt_equities.news.finnhub_adapter import (
    FinnhubCompanyNewsAdapter,
    FinnhubGeneralNewsAdapter,
    _normalize_finnhub_item,
)
from agt_equities.news.types import (
    HEADLINE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    NewsAdapter,
    NewsItem,
)
from agt_equities.news.yfinance_adapter import YFinanceAdapter

pytestmark = pytest.mark.sprint_a


# ---------- NewsItem invariants ----------


def _utc(year=2026, month=4, day=23, hour=10, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_news_item_headline_truncates_to_280_chars() -> None:
    long = "x" * 400
    it = NewsItem(
        source="yfinance",
        ticker="AAPL",
        headline=long,
        summary=None,
        url="https://example.com/a",
        published_utc=_utc(),
    )
    assert len(it.headline) == HEADLINE_MAX_CHARS
    assert it.headline.endswith("…")


def test_news_item_summary_truncates_to_500_chars() -> None:
    long = "y" * 600
    it = NewsItem(
        source="yfinance",
        ticker="AAPL",
        headline="ok",
        summary=long,
        url="https://example.com/a",
        published_utc=_utc(),
    )
    assert len(it.summary) == SUMMARY_MAX_CHARS
    assert it.summary.endswith("…")


def test_news_item_naive_datetime_raises() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        NewsItem(
            source="yfinance",
            ticker="AAPL",
            headline="ok",
            summary=None,
            url="https://example.com/a",
            published_utc=datetime(2026, 4, 23, 10, 0),  # naive
        )


def test_news_item_summary_none_passes_through() -> None:
    it = NewsItem(
        source="edgar_8k",
        ticker="AAPL",
        headline="filing",
        summary=None,
        url="https://www.sec.gov/x",
        published_utc=_utc(),
    )
    assert it.summary is None


def test_news_item_is_frozen() -> None:
    it = NewsItem(
        source="yfinance",
        ticker="AAPL",
        headline="ok",
        summary=None,
        url="https://example.com/a",
        published_utc=_utc(),
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        it.headline = "mutated"  # type: ignore[misc]


# ---------- NewsAdapter Protocol conformance ----------


def test_yfinance_adapter_conforms_to_protocol() -> None:
    a = YFinanceAdapter()
    assert isinstance(a, NewsAdapter)
    assert a.source == "yfinance"


def test_finnhub_company_adapter_conforms_to_protocol() -> None:
    fake_client = SimpleNamespace()
    a = FinnhubCompanyNewsAdapter(client=fake_client)  # type: ignore[arg-type]
    assert isinstance(a, NewsAdapter)
    assert a.source == "finnhub_company"


def test_finnhub_general_adapter_conforms_to_protocol() -> None:
    fake_client = SimpleNamespace()
    a = FinnhubGeneralNewsAdapter(client=fake_client)  # type: ignore[arg-type]
    assert isinstance(a, NewsAdapter)
    assert a.source == "finnhub_general"


# ---------- YFinanceAdapter ----------


def test_yfinance_adapter_macro_returns_empty() -> None:
    a = YFinanceAdapter()
    out = asyncio.run(a.fetch(None))
    assert out == []


def test_yfinance_adapter_happy_path_old_schema() -> None:
    """Schema before yfinance 0.2.28: top-level keys."""
    raw = [
        {
            "title": "Apple beats earnings",
            "summary": "Strong quarter",
            "link": "https://news.example/aapl-1",
            "providerPublishTime": _utc().timestamp(),
        }
    ]
    a = YFinanceAdapter()
    with patch.object(YFinanceAdapter, "_fetch_sync", return_value=raw):
        items = asyncio.run(a.fetch("AAPL", lookback_hours=24))
    assert len(items) == 1
    assert items[0].source == "yfinance"
    assert items[0].ticker == "AAPL"
    assert items[0].headline == "Apple beats earnings"


def test_yfinance_adapter_happy_path_new_schema() -> None:
    """Schema yfinance >= 0.2.28: nested 'content' key."""
    raw = [
        {
            "content": {
                "title": "MSFT cloud growth",
                "summary": None,
                "canonicalUrl": {"url": "https://news.example/msft-1"},
                "pubDate": _utc().isoformat(),
            }
        }
    ]
    a = YFinanceAdapter()
    with patch.object(YFinanceAdapter, "_fetch_sync", return_value=raw):
        items = asyncio.run(a.fetch("MSFT", lookback_hours=24))
    assert len(items) == 1
    assert items[0].headline == "MSFT cloud growth"


def test_yfinance_adapter_filters_by_lookback() -> None:
    fresh = _utc()
    stale = _utc() - timedelta(hours=48)
    raw = [
        {"title": "fresh", "link": "u1", "providerPublishTime": fresh.timestamp()},
        {"title": "stale", "link": "u2", "providerPublishTime": stale.timestamp()},
    ]
    a = YFinanceAdapter()
    with patch.object(YFinanceAdapter, "_fetch_sync", return_value=raw):
        items = asyncio.run(a.fetch("AAPL", lookback_hours=24))
    assert len(items) == 1
    assert items[0].headline == "fresh"


def test_yfinance_adapter_swallows_exceptions() -> None:
    a = YFinanceAdapter()
    with patch.object(YFinanceAdapter, "_fetch_sync", side_effect=RuntimeError("boom")):
        items = asyncio.run(a.fetch("AAPL"))
    assert items == []


def test_yfinance_adapter_returns_empty_on_malformed_payload() -> None:
    a = YFinanceAdapter()
    raw = [{"no_title": True}, "not a dict", None]
    with patch.object(YFinanceAdapter, "_fetch_sync", return_value=raw):
        items = asyncio.run(a.fetch("AAPL"))
    assert items == []


def test_yfinance_adapter_returns_empty_on_non_list() -> None:
    a = YFinanceAdapter()
    with patch.object(YFinanceAdapter, "_fetch_sync", return_value={"not": "a list"}):
        items = asyncio.run(a.fetch("AAPL"))
    # _fetch_sync filters non-list to []; adapter just sees empty
    assert items == []


# ---------- FinnhubCompanyNewsAdapter ----------


class _FakeFinnhubClient:
    """Test double with .get_company_news / .get_general_news async methods."""

    def __init__(self, company_response=None, general_response=None,
                 company_raises=None, general_raises=None):
        self._company_response = company_response
        self._general_response = general_response
        self._company_raises = company_raises
        self._general_raises = general_raises
        self.company_calls: list[tuple] = []
        self.general_calls: list[tuple] = []

    async def get_company_news(self, ticker, from_date, to_date):
        self.company_calls.append((ticker, from_date, to_date))
        if self._company_raises:
            raise self._company_raises
        return self._company_response

    async def get_general_news(self, category="general"):
        self.general_calls.append((category,))
        if self._general_raises:
            raise self._general_raises
        return self._general_response


def test_finnhub_company_adapter_macro_returns_empty() -> None:
    a = FinnhubCompanyNewsAdapter(client=_FakeFinnhubClient())  # type: ignore[arg-type]
    out = asyncio.run(a.fetch(None))
    assert out == []


def test_finnhub_company_adapter_happy_path() -> None:
    raw = [
        {
            "id": 1,
            "datetime": int(_utc().timestamp()),
            "headline": "Apple announces buyback",
            "summary": "$50B over 4y",
            "url": "https://news.example/aapl-x",
            "source": "Reuters",
        },
    ]
    client = _FakeFinnhubClient(company_response=raw)
    a = FinnhubCompanyNewsAdapter(client=client)  # type: ignore[arg-type]
    items = asyncio.run(a.fetch("AAPL", lookback_hours=24))
    assert len(items) == 1
    assert items[0].source == "finnhub_company"
    assert items[0].headline == "Apple announces buyback"
    assert items[0].url == "https://news.example/aapl-x"
    assert client.company_calls[0][0] == "AAPL"


def test_finnhub_company_adapter_swallows_exceptions() -> None:
    client = _FakeFinnhubClient(company_raises=RuntimeError("429 Too Many Requests"))
    a = FinnhubCompanyNewsAdapter(client=client)  # type: ignore[arg-type]
    items = asyncio.run(a.fetch("AAPL"))
    assert items == []


def test_finnhub_company_adapter_returns_empty_on_none() -> None:
    client = _FakeFinnhubClient(company_response=None)
    a = FinnhubCompanyNewsAdapter(client=client)  # type: ignore[arg-type]
    items = asyncio.run(a.fetch("AAPL"))
    assert items == []


def test_finnhub_company_adapter_filters_by_lookback() -> None:
    fresh = _utc()
    stale = _utc() - timedelta(hours=48)
    raw = [
        {"datetime": int(fresh.timestamp()), "headline": "fresh", "url": "u1"},
        {"datetime": int(stale.timestamp()), "headline": "stale", "url": "u2"},
    ]
    client = _FakeFinnhubClient(company_response=raw)
    a = FinnhubCompanyNewsAdapter(client=client)  # type: ignore[arg-type]
    items = asyncio.run(a.fetch("AAPL", lookback_hours=24))
    assert len(items) == 1
    assert items[0].headline == "fresh"


# ---------- FinnhubGeneralNewsAdapter ----------


def test_finnhub_general_adapter_per_ticker_returns_empty_with_warning() -> None:
    a = FinnhubGeneralNewsAdapter(client=_FakeFinnhubClient())  # type: ignore[arg-type]
    items = asyncio.run(a.fetch("AAPL"))
    assert items == []


def test_finnhub_general_adapter_happy_path() -> None:
    raw = [
        {
            "datetime": int(_utc().timestamp()),
            "headline": "Fed holds rates",
            "summary": "FOMC unchanged",
            "url": "https://news.example/macro-1",
        }
    ]
    client = _FakeFinnhubClient(general_response=raw)
    a = FinnhubGeneralNewsAdapter(client=client)  # type: ignore[arg-type]
    items = asyncio.run(a.fetch(None))
    assert len(items) == 1
    assert items[0].source == "finnhub_general"
    assert items[0].ticker is None
    assert items[0].headline == "Fed holds rates"


def test_finnhub_general_adapter_swallows_exceptions() -> None:
    client = _FakeFinnhubClient(general_raises=ValueError("malformed"))
    a = FinnhubGeneralNewsAdapter(client=client)  # type: ignore[arg-type]
    items = asyncio.run(a.fetch(None))
    assert items == []


def test_finnhub_general_adapter_returns_empty_on_empty_response() -> None:
    client = _FakeFinnhubClient(general_response=[])
    a = FinnhubGeneralNewsAdapter(client=client)  # type: ignore[arg-type]
    items = asyncio.run(a.fetch(None))
    assert items == []


# ---------- _normalize_finnhub_item ----------


def test_normalize_finnhub_item_drops_missing_headline() -> None:
    raw = {"datetime": 1700000000, "url": "u"}
    assert _normalize_finnhub_item(raw, ticker="AAPL", source="finnhub_company") is None


def test_normalize_finnhub_item_drops_missing_url() -> None:
    raw = {"datetime": 1700000000, "headline": "x"}
    assert _normalize_finnhub_item(raw, ticker="AAPL", source="finnhub_company") is None


def test_normalize_finnhub_item_drops_invalid_timestamp() -> None:
    raw = {"datetime": "not_a_number", "headline": "x", "url": "u"}
    assert _normalize_finnhub_item(raw, ticker="AAPL", source="finnhub_company") is None


def test_normalize_finnhub_item_handles_none_summary() -> None:
    raw = {
        "datetime": int(_utc().timestamp()),
        "headline": "ok",
        "url": "u",
        "summary": "",
    }
    item = _normalize_finnhub_item(raw, ticker="AAPL", source="finnhub_company")
    assert item is not None
    assert item.summary is None
