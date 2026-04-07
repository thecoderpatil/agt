"""
agt_equities/data_provider.py — Market data provider abstraction.

Provides a seam between rule evaluation infrastructure and live market data
sources. Evaluators themselves are PURE (take PortfolioState, no I/O).
The provider populates PortfolioState upstream before evaluators run.

Default provider: IBKRProvider (ib_async).
Test provider: FakeProvider (deterministic, no network).
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types returned by providers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bar:
    """Single daily price bar."""
    date: date
    close: float


@dataclass(frozen=True)
class AccountSummary:
    """IBKR account summary snapshot."""
    account_id: str
    excess_liquidity: float
    net_liquidation: float
    timestamp: datetime


@dataclass(frozen=True)
class OptionChain:
    """Placeholder for 3A.5c."""
    symbol: str


@dataclass(frozen=True)
class Fundamentals:
    """Placeholder for 3A.5c."""
    symbol: str


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class DataProviderError(Exception):
    """Typed error for provider failures. Evaluators catch this and
    convert to AMBER (data outage is not a rule breach)."""
    pass


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class MarketDataProvider(ABC):
    """Abstract market data provider. All rule infrastructure reads
    market data through this interface."""

    @abstractmethod
    def get_historical_daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        """Returns list of daily bars for the lookback window.
        Raises DataProviderError on failure."""

    @abstractmethod
    def get_account_summary(self, account_id: str) -> AccountSummary:
        """Returns ExcessLiquidity, NetLiquidation, etc.
        Raises DataProviderError on failure."""

    @abstractmethod
    def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        """3A.5c — raises NotImplementedError until then."""

    @abstractmethod
    def get_fundamentals(self, symbol: str) -> Fundamentals:
        """3A.5c — raises NotImplementedError until then."""

    @abstractmethod
    def get_earnings_date(self, symbol: str) -> date | None:
        """3A.5c — raises NotImplementedError until then."""


# ---------------------------------------------------------------------------
# IBKR provider (ib_async)
# ---------------------------------------------------------------------------

class IBKRProvider(MarketDataProvider):
    """Default provider. Uses ib_async for live/delayed IBKR data.

    Market data type controlled by .env MARKET_DATA_MODE:
      - 'live'    -> ib.reqMarketDataType(1)
      - 'delayed' -> ib.reqMarketDataType(3) [DEFAULT]
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 4001,
                 client_id: int = 99, market_data_mode: str = "delayed"):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._market_data_mode = market_data_mode
        self._ib = None

    def _connect(self):
        """Lazy connect to TWS/Gateway. Raises DataProviderError on failure."""
        if self._ib is not None and self._ib.isConnected():
            return
        try:
            from ib_async import IB
            self._ib = IB()
            self._ib.connect(self._host, self._port, clientId=self._client_id)
            mdt = 1 if self._market_data_mode == "live" else 3
            self._ib.reqMarketDataType(mdt)
            logger.info("IBKRProvider connected to %s:%s (mode=%s)",
                        self._host, self._port, self._market_data_mode)
        except Exception as exc:
            self._ib = None
            raise DataProviderError(f"Failed to connect to IBKR: {exc}") from exc

    def get_historical_daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        """Fetch daily close bars from IBKR via reqHistoricalData."""
        try:
            self._connect()
            from ib_async import Stock
            contract = Stock(symbol, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            duration = f"{lookback_days} D"
            bars_raw = self._ib.reqHistoricalData(
                contract, endDateTime="", durationStr=duration,
                barSizeSetting="1 day", whatToShow="ADJUSTED_LAST",
                useRTH=True, formatDate=1,
            )
            result = []
            for b in bars_raw:
                bar_date = b.date if isinstance(b.date, date) else date.fromisoformat(str(b.date))
                result.append(Bar(date=bar_date, close=float(b.close)))
            return result
        except DataProviderError:
            raise
        except Exception as exc:
            raise DataProviderError(f"Historical bars failed for {symbol}: {exc}") from exc

    def get_account_summary(self, account_id: str) -> AccountSummary:
        """Fetch ExcessLiquidity and NetLiquidation from IBKR."""
        try:
            self._connect()
            summary = self._ib.accountSummary(account_id)
            el = None
            nlv = None
            for item in summary:
                if item.tag == "ExcessLiquidity" and item.currency == "USD":
                    el = float(item.value)
                elif item.tag == "NetLiquidation" and item.currency == "USD":
                    nlv = float(item.value)
            if el is None or nlv is None:
                raise DataProviderError(
                    f"Missing fields in account summary for {account_id}: "
                    f"EL={el}, NLV={nlv}"
                )
            return AccountSummary(
                account_id=account_id,
                excess_liquidity=el,
                net_liquidation=nlv,
                timestamp=datetime.utcnow(),
            )
        except DataProviderError:
            raise
        except Exception as exc:
            raise DataProviderError(
                f"Account summary failed for {account_id}: {exc}"
            ) from exc

    def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        raise NotImplementedError("Option chain: 3A.5c scope")

    def get_fundamentals(self, symbol: str) -> Fundamentals:
        raise NotImplementedError("Fundamentals: 3A.5c scope")

    def get_earnings_date(self, symbol: str) -> date | None:
        raise NotImplementedError("Earnings date: 3A.5c scope")

    def disconnect(self):
        """Clean disconnect."""
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
            self._ib = None


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_provider_instance: MarketDataProvider | None = None

def get_provider() -> MarketDataProvider:
    """Returns the configured provider singleton.
    Reads .env MARKET_DATA_MODE and PROVIDER_TYPE on first call."""
    global _provider_instance
    if _provider_instance is not None:
        return _provider_instance

    provider_type = os.environ.get("PROVIDER_TYPE", "ibkr").lower()
    market_data_mode = os.environ.get("MARKET_DATA_MODE", "delayed").lower()

    if provider_type == "ibkr":
        host = os.environ.get("IB_HOST", "127.0.0.1")
        port = int(os.environ.get("IB_PORT", "4001"))
        client_id = int(os.environ.get("IB_CLIENT_ID", "99"))
        _provider_instance = IBKRProvider(
            host=host, port=port, client_id=client_id,
            market_data_mode=market_data_mode,
        )
    else:
        raise ValueError(f"Unknown PROVIDER_TYPE: {provider_type}")

    return _provider_instance


def set_provider(provider: MarketDataProvider) -> None:
    """Override the singleton (for testing). Call with None to reset."""
    global _provider_instance
    _provider_instance = provider


def reset_provider() -> None:
    """Reset singleton to force re-initialization."""
    global _provider_instance
    _provider_instance = None
