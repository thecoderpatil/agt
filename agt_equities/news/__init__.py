"""News overlay package — adapters + aggregator + cache.

Per ADR-CSP_NEWS_OVERLAY_v1, this package layers exogenous news signal
on top of the screener's quantitative output. Public surface:

    NewsItem        — frozen dataclass for a single news/filing record
    NewsAdapter     — Protocol every source adapter conforms to
    YFinanceAdapter — yfinance.Ticker(x).news wrapper
    FinnhubCompanyNewsAdapter / FinnhubGeneralNewsAdapter — Finnhub adapters
    EdgarClient / EdgarAdapter — SEC EDGAR 8-K item-filtered adapter
    NewsBundle      — merged + deduped per-ticker (or macro) bundle
    fetch_bundles / fetch_news_bundle / fetch_macro_bundle — aggregator entry
    prune_news_cache — periodic news_cache GC
"""
from __future__ import annotations

from agt_equities.news.aggregator import (
    MACRO_KEY,
    NewsBundle,
    fetch_bundles,
    fetch_macro_bundle,
    fetch_news_bundle,
    prune_news_cache,
)
from agt_equities.news.edgar_adapter import EdgarAdapter
from agt_equities.news.edgar_client import HIGH_SIGNAL_8K_ITEMS, EdgarClient
from agt_equities.news.finnhub_adapter import (
    FinnhubCompanyNewsAdapter,
    FinnhubGeneralNewsAdapter,
)
from agt_equities.news.types import NewsAdapter, NewsItem
from agt_equities.news.yfinance_adapter import YFinanceAdapter

__all__ = [
    "EdgarAdapter",
    "EdgarClient",
    "FinnhubCompanyNewsAdapter",
    "FinnhubGeneralNewsAdapter",
    "HIGH_SIGNAL_8K_ITEMS",
    "MACRO_KEY",
    "NewsAdapter",
    "NewsBundle",
    "NewsItem",
    "YFinanceAdapter",
    "fetch_bundles",
    "fetch_macro_bundle",
    "fetch_news_bundle",
    "prune_news_cache",
]
