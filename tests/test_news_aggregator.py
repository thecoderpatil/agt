"""Unit tests for news aggregator + EDGAR adapter + migration.

Per ADR-CSP_NEWS_OVERLAY_v1 testing strategy. No live network. Adapters
mocked at fetch boundary; cache layer tested against an in-memory
SQLite file.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import date, datetime, time, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add scripts/ to path for migration import (since scripts/ is not a package).
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from agt_equities.news.aggregator import (  # noqa: E402
    DEFAULT_CACHE_TTL_S,
    MACRO_KEY,
    NewsBundle,
    _cache_key,
    _dedup,
    _read_cache,
    _write_cache,
    fetch_bundles,
    fetch_macro_bundle,
    fetch_news_bundle,
    prune_news_cache,
)
from agt_equities.news.edgar_adapter import EdgarAdapter  # noqa: E402
from agt_equities.news.edgar_client import HIGH_SIGNAL_8K_ITEMS, EdgarClient  # noqa: E402
from agt_equities.news.types import NewsItem  # noqa: E402

import migrate_news_cache  # noqa: E402

pytestmark = pytest.mark.sprint_a


# ---------- helpers ----------


def _utc(year=2026, month=4, day=23, hour=10, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _make_item(source: str, ticker: str | None, headline: str = "h",
               url: str = "https://x.example/a", when=None):
    return NewsItem(
        source=source, ticker=ticker, headline=headline, summary=None,
        url=url, published_utc=when or _utc(),
    )


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "agt_desk.db"
    migrate_news_cache.migrate(str(p))
    return p


# ---------- migration ----------


def test_migrate_creates_news_cache_table(db_path):
    with closing(sqlite3.connect(str(db_path))) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='news_cache'"
        ).fetchall()
    assert rows, "news_cache table should exist after migration"


def test_migrate_is_idempotent(db_path):
    # Run again — must not error and not destroy state.
    migrate_news_cache.migrate(str(db_path))
    with closing(sqlite3.connect(str(db_path))) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_news_cache_ticker_src'"
        ).fetchall()
    assert rows, "ticker/source index should exist after migrate"


# ---------- _dedup ----------


def test_dedup_drops_same_ticker_source_url():
    a = _make_item("yfinance", "AAPL", url="https://u/a")
    b = _make_item("yfinance", "AAPL", url="https://u/a")  # dupe
    c = _make_item("finnhub_company", "AAPL", url="https://u/a")  # different source: keep
    d = _make_item("yfinance", "AAPL", url="https://u/b")  # different url: keep
    out = _dedup([a, b, c, d])
    assert len(out) == 3


def test_dedup_sorts_descending_by_published_utc():
    older = _make_item("yfinance", "AAPL", url="https://u/older",
                       when=_utc(hour=8))
    newer = _make_item("yfinance", "AAPL", url="https://u/newer",
                       when=_utc(hour=14))
    out = _dedup([older, newer])
    assert out[0].url == "https://u/newer"
    assert out[1].url == "https://u/older"


# ---------- cache key ----------


def test_cache_key_per_ticker():
    assert _cache_key("yfinance", "AAPL", 24) == "yfinance:AAPL:24h"


def test_cache_key_macro():
    assert _cache_key("finnhub_general", None, 24) == f"finnhub_general:{MACRO_KEY}:24h"


# ---------- _read_cache + _write_cache ----------


def test_read_cache_miss_returns_none(db_path):
    out = _read_cache(db_path, "yfinance:AAPL:24h", cache_ttl_s=3600)
    assert out is None


def test_write_then_read_cache_roundtrip(db_path):
    items = [_make_item("yfinance", "AAPL", headline="hello", url="https://u/1")]
    _write_cache(db_path, "yfinance:AAPL:24h", "yfinance", "AAPL", 24, items, 3600)
    out = _read_cache(db_path, "yfinance:AAPL:24h", cache_ttl_s=3600)
    assert out is not None and len(out) == 1
    assert out[0].headline == "hello"
    assert out[0].source == "yfinance"
    assert out[0].url == "https://u/1"


def test_read_cache_expired_returns_none(db_path):
    items = [_make_item("yfinance", "AAPL", url="https://u/1")]
    # Write with TTL 0 — immediately expired
    _write_cache(db_path, "yfinance:AAPL:24h", "yfinance", "AAPL", 24, items, 0)
    out = _read_cache(db_path, "yfinance:AAPL:24h", cache_ttl_s=0)
    assert out is None


def test_write_cache_replace_on_duplicate_key(db_path):
    items_v1 = [_make_item("yfinance", "AAPL", headline="v1", url="u1")]
    items_v2 = [_make_item("yfinance", "AAPL", headline="v2", url="u2")]
    _write_cache(db_path, "k", "yfinance", "AAPL", 24, items_v1, 3600)
    _write_cache(db_path, "k", "yfinance", "AAPL", 24, items_v2, 3600)
    out = _read_cache(db_path, "k", cache_ttl_s=3600)
    assert out and out[0].headline == "v2"


# ---------- prune ----------


def test_prune_drops_old_rows(db_path):
    items = [_make_item("yfinance", "AAPL", url="u")]
    _write_cache(db_path, "k1", "yfinance", "AAPL", 24, items, 3600)
    # Manually backdate one row
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            "UPDATE news_cache SET fetched_at_utc = datetime('now', '-30 days') "
            "WHERE cache_key = ?",
            ("k1",),
        )
        conn.commit()
    n = prune_news_cache(db_path, keep_last_days=7)
    assert n == 1


# ---------- aggregator orchestration ----------


class _FakeAdapter:
    def __init__(self, source, items=None, raises=None):
        self.source = source
        self._items = items or []
        self._raises = raises
        self.calls = 0

    async def fetch(self, ticker, *, lookback_hours=24, timeout_s=3.0):
        self.calls += 1
        if self._raises:
            raise self._raises
        return list(self._items)


def test_fetch_news_bundle_merges_and_dedups(db_path):
    a1 = _FakeAdapter("yfinance", items=[
        _make_item("yfinance", "AAPL", url="u1"),
    ])
    a2 = _FakeAdapter("finnhub_company", items=[
        _make_item("finnhub_company", "AAPL", url="u1"),  # different source -> keep
        _make_item("finnhub_company", "AAPL", url="u2"),
    ])
    bundle = asyncio.run(fetch_news_bundle("AAPL", [a1, a2], db_path=db_path))
    assert bundle.ticker == "AAPL"
    assert len(bundle.items) == 3
    assert sorted(bundle.sources_ok) == ["finnhub_company", "yfinance"]
    assert bundle.sources_failed == []


def test_fetch_news_bundle_fail_soft_when_one_adapter_raises(db_path):
    ok = _FakeAdapter("yfinance", items=[_make_item("yfinance", "AAPL", url="u")])
    bad = _FakeAdapter("finnhub_company", raises=RuntimeError("429"))
    bundle = asyncio.run(fetch_news_bundle("AAPL", [ok, bad], db_path=db_path))
    assert bundle.sources_ok == ["yfinance"]
    assert bundle.sources_failed == ["finnhub_company"]
    assert len(bundle.items) == 1


def test_fetch_news_bundle_uses_cache_on_second_call(db_path):
    a = _FakeAdapter("yfinance", items=[_make_item("yfinance", "AAPL", url="u")])
    asyncio.run(fetch_news_bundle("AAPL", [a], db_path=db_path))
    assert a.calls == 1
    # Second call should hit cache
    asyncio.run(fetch_news_bundle("AAPL", [a], db_path=db_path))
    assert a.calls == 1, "cache hit should prevent second adapter call"


def test_fetch_news_bundle_no_cache_path(db_path):
    a = _FakeAdapter("yfinance", items=[_make_item("yfinance", "AAPL", url="u")])
    asyncio.run(fetch_news_bundle("AAPL", [a]))  # db_path=None
    asyncio.run(fetch_news_bundle("AAPL", [a]))
    assert a.calls == 2, "without db cache, every call hits adapter"


def test_fetch_macro_bundle_returns_macro_items(db_path):
    a = _FakeAdapter(
        "finnhub_general",
        items=[_make_item("finnhub_general", None, url="u")],
    )
    bundle = asyncio.run(fetch_macro_bundle([a], db_path=db_path))
    assert bundle.ticker is None
    assert len(bundle.items) == 1


def test_fetch_bundles_returns_per_ticker_plus_macro(db_path):
    company = _FakeAdapter("yfinance", items=[
        _make_item("yfinance", "AAPL", url="u"),
    ])
    macro = _FakeAdapter("finnhub_general", items=[
        _make_item("finnhub_general", None, url="m"),
    ])
    out = asyncio.run(fetch_bundles(
        ["AAPL", "MSFT"],
        per_ticker_adapters=[company],
        macro_adapters=[macro],
        db_path=db_path,
    ))
    assert "AAPL" in out and "MSFT" in out and MACRO_KEY in out
    assert out["AAPL"].items
    assert out[MACRO_KEY].items


def test_fetch_bundles_macro_is_present_even_with_no_macro_adapters(db_path):
    a = _FakeAdapter("yfinance", items=[])
    out = asyncio.run(fetch_bundles(
        ["AAPL"],
        per_ticker_adapters=[a],
        db_path=db_path,
    ))
    assert MACRO_KEY in out
    assert out[MACRO_KEY].items == []


# ---------- EDGAR client ----------


def test_edgar_extract_recent_filings_filters_8k_items():
    payload = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000010", "0000320193-26-000011"],
                "form":            ["8-K",                  "10-Q"],
                "filingDate":      ["2026-04-22",           "2026-04-22"],
                "items":           ["1.02,5.02",            ""],
                "primaryDocument": ["a.htm",                "b.htm"],
            }
        }
    }
    rows = EdgarClient._extract_recent_filings(payload)
    assert len(rows) == 2
    assert rows[0]["form"] == "8-K"
    assert rows[0]["items"] == ["1.02", "5.02"]
    assert rows[1]["items"] == []


def test_edgar_extract_handles_missing_filings_block():
    assert EdgarClient._extract_recent_filings({}) == []
    assert EdgarClient._extract_recent_filings({"filings": {}}) == []


def test_edgar_extract_handles_unparseable_date():
    payload = {"filings": {"recent": {
        "accessionNumber": ["a"], "form": ["8-K"], "filingDate": ["not-a-date"],
        "items": [""], "primaryDocument": ["x.htm"],
    }}}
    assert EdgarClient._extract_recent_filings(payload) == []


def test_edgar_high_signal_items_locked():
    assert HIGH_SIGNAL_8K_ITEMS == frozenset({"1.02", "2.04", "4.01", "4.02", "5.02"})


def test_edgar_client_returns_empty_when_cik_unknown():
    client = EdgarClient()

    async def _go():
        # Force CIK map empty
        client._cik_map = {}
        out = await client.fetch_8k_filings("UNKNOWN_TICKER")
        return out

    assert asyncio.run(_go()) == []


# ---------- EDGAR adapter ----------


def test_edgar_adapter_macro_returns_empty():
    client = EdgarClient()
    a = EdgarAdapter(client=client)
    out = asyncio.run(a.fetch(None))
    assert out == []


def test_edgar_adapter_normalizes_filings_to_news_items():
    client = EdgarClient()
    a = EdgarAdapter(client=client)

    fake_filings = [
        {
            "accession_number": "0000320193-26-000010",
            "form": "8-K",
            "filed_date": date(2026, 4, 22),
            "items": ["1.02", "5.02"],
            "primary_document_url": "https://www.sec.gov/x.htm",
            "ticker": "AAPL",
            "cik": "0000320193",
        }
    ]

    async def _fake_fetch(ticker, *, lookback_hours=72):
        return list(fake_filings)

    client.fetch_8k_filings = _fake_fetch  # type: ignore[method-assign]
    items = asyncio.run(a.fetch("AAPL", lookback_hours=72))
    assert len(items) == 2  # one per item code
    tags = sorted(it.tag for it in items)
    assert tags == ["1.02", "5.02"]
    for it in items:
        assert it.source == "edgar_8k"
        assert it.ticker == "AAPL"


def test_edgar_adapter_swallows_exceptions():
    client = EdgarClient()
    a = EdgarAdapter(client=client)

    async def _raise(ticker, *, lookback_hours=72):
        raise RuntimeError("net err")

    client.fetch_8k_filings = _raise  # type: ignore[method-assign]
    out = asyncio.run(a.fetch("AAPL"))
    assert out == []
