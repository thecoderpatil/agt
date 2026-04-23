"""News overlay package — adapters + (later) aggregator.

Per ADR-CSP_NEWS_OVERLAY_v1, this package layers exogenous news signal
on top of the screener's quantitative output. Public surface:

    NewsItem        — frozen dataclass for a single news/filing record
    NewsAdapter     — Protocol every source adapter conforms to
    YFinanceAdapter — yfinance.Ticker(x).news wrapper

Aggregator + EDGAR client + cache layer ship in a follow-up MR.
"""
from __future__ import annotations

from agt_equities.news.finnhub_adapter import (
    FinnhubCompanyNewsAdapter,
    FinnhubGeneralNewsAdapter,
)
from agt_equities.news.types import NewsAdapter, NewsItem
from agt_equities.news.yfinance_adapter import YFinanceAdapter

__all__ = [
    "FinnhubCompanyNewsAdapter",
    "FinnhubGeneralNewsAdapter",
    "NewsAdapter",
    "NewsItem",
    "YFinanceAdapter",
]
