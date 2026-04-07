"""
agt_equities/market_data_interfaces.py — 4-way Interface Segregation for market data.

ALL SYNC. No async def. Implementation classes may use ib.run() internally
but the public interface is synchronous.

Per Gemini Q3 ISP guidance:
  IPriceAndVolatility   — real-time pricing, historical bars, vol surface, factor matrix
  IOptionsChain         — option chain slices with bounded DTE/delta filters
  ICorporateIntelligence — fundamentals, earnings, calendar (cold path, cached)
  IAccountState         — ALREADY EXISTS as state_builder.py + PortfolioState.
                          NOT duplicated here. See ADR-001 for denominator semantics.

HARD RULE: Do NOT bleed PortfolioState into any market data provider.
Account state and market data are structurally separate.

Phase 3A.5c1 — data layer foundation.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import List, Optional, Tuple

from agt_equities.market_data_dtos import (
    OptionContractDTO,
    VolatilityMetricsDTO,
    CorporateCalendarDTO,
    ConvictionMetricsDTO,
)


class IPriceAndVolatility(ABC):
    """Real-time pricing and volatility surfaces. IBKR-implemented."""

    @abstractmethod
    def get_spot(self, ticker: str) -> Optional[float]:
        """Returns underlying spot price. Falls back to last close on
        plans without streaming stock quotes (IBKR error 10089).
        Returns None on hard failure."""

    @abstractmethod
    def get_macro_index(self, symbol: str) -> Optional[float]:
        """Dedicated routing for VIX, SPX, etc. Returns None on failure."""

    @abstractmethod
    def get_historical_daily_bars(
        self, ticker: str, lookback_days: int,
    ) -> List[Tuple[date, float]]:
        """Returns dividend-adjusted daily closes as (date, close) tuples."""

    @abstractmethod
    def get_factor_matrix(
        self, tickers: List[str], benchmark: str = "SPY",
        trading_days: int = 126,
    ) -> dict:
        """Returns aligned daily log-returns matrix as dict of lists.
        Used by R4 (correlation) and R11 (beta). Provider handles
        alignment, holiday truncation, and dividend adjustment.
        Returns dict keyed by ticker, values are lists of log returns."""

    @abstractmethod
    def get_volatility_surface(
        self, ticker: str,
    ) -> Optional[VolatilityMetricsDTO]:
        """Returns IV30, RV30, IV rank (or None), VRP."""


class IOptionsChain(ABC):
    """Option chain queries with bounded spatial filters."""

    @abstractmethod
    def get_chain_slice(
        self,
        ticker: str,
        right: str,            # 'C' or 'P'
        min_dte: int,
        max_dte: int,
        max_delta: Optional[float] = None,
    ) -> List[OptionContractDTO]:
        """Returns option contracts matching the spatial filter.
        NO max_spread_pct parameter — spread is a DTO field, filtered
        at evaluator layer per Gemini Q7."""


class ICorporateIntelligence(ABC):
    """Cold-path fundamentals and corporate calendars.

    yfinance fallback during build phase. All implementations marked
    TEMPORARY with # DEPLOYMENT: replace comments.

    COLD PATH ONLY. Cached, not in any execution hot loop.
    Failure must NOT crash R8 evaluator — Gate 1 falls back to
    NEUTRAL conviction tier on None.
    """

    @abstractmethod
    def get_corporate_calendar(
        self, ticker: str,
    ) -> Optional[CorporateCalendarDTO]:
        """Earnings dates, ex-dividend dates, pending corporate actions."""

    @abstractmethod
    def get_conviction_metrics(
        self, ticker: str,
    ) -> Optional[ConvictionMetricsDTO]:
        """Fundamentals for R8 Gate 1 conviction tier."""
