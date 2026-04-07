"""
tests/fixtures/fake_provider.py — Deterministic test provider.

Returns canned data so evaluator unit tests run without a live IBKR connection.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from agt_equities.data_provider import (
    MarketDataProvider, Bar, AccountSummary, OptionChain, Fundamentals,
    DataProviderError,
)


class FakeProvider(MarketDataProvider):
    """Deterministic provider for unit tests.

    Configure via constructor kwargs:
      bars: dict[str, list[Bar]]  — keyed by symbol
      accounts: dict[str, AccountSummary]  — keyed by account_id
      fail_symbols: set[str]  — symbols that raise DataProviderError
      fail_accounts: set[str]  — accounts that raise DataProviderError
    """

    def __init__(
        self,
        bars: dict[str, list[Bar]] | None = None,
        accounts: dict[str, AccountSummary] | None = None,
        fail_symbols: set[str] | None = None,
        fail_accounts: set[str] | None = None,
    ):
        self.bars = bars or {}
        self.accounts = accounts or {}
        self.fail_symbols = fail_symbols or set()
        self.fail_accounts = fail_accounts or set()

    def get_historical_daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        if symbol in self.fail_symbols:
            raise DataProviderError(f"Simulated failure for {symbol}")
        return self.bars.get(symbol, [])

    def get_account_summary(self, account_id: str) -> AccountSummary:
        if account_id in self.fail_accounts:
            raise DataProviderError(f"Simulated failure for {account_id}")
        if account_id not in self.accounts:
            raise DataProviderError(f"No data for account {account_id}")
        return self.accounts[account_id]

    def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        raise NotImplementedError("FakeProvider: option chain not implemented")

    def get_fundamentals(self, symbol: str) -> Fundamentals:
        raise NotImplementedError("FakeProvider: fundamentals not implemented")

    def get_earnings_date(self, symbol: str) -> date | None:
        raise NotImplementedError("FakeProvider: earnings date not implemented")


# ---------------------------------------------------------------------------
# Helper: generate synthetic daily bars with known correlation
# ---------------------------------------------------------------------------

def make_bars(start_price: float, daily_returns: list[float],
              start_date: date | None = None) -> list[Bar]:
    """Build a list of Bar from a base price + daily return sequence.

    daily_returns: list of fractional returns (e.g., 0.01 = +1%).
    """
    if start_date is None:
        start_date = date(2025, 10, 1)
    bars = [Bar(date=start_date, close=start_price)]
    price = start_price
    for i, r in enumerate(daily_returns):
        price = price * (1 + r)
        bars.append(Bar(date=start_date + timedelta(days=i + 1), close=round(price, 4)))
    return bars


def make_correlated_bars(n: int = 180, correlation: float = 0.8,
                         base_price_a: float = 100.0,
                         base_price_b: float = 50.0,
                         start_date: date | None = None) -> tuple[list[Bar], list[Bar]]:
    """Generate two sets of bars with approximately the target Pearson correlation.

    Uses a simple linear mixing model:
      returns_b = correlation * returns_a + sqrt(1-corr^2) * noise
    """
    import math
    import random
    rng = random.Random(42)  # deterministic

    if start_date is None:
        start_date = date(2025, 10, 1)

    returns_a = [rng.gauss(0, 0.02) for _ in range(n)]
    noise = [rng.gauss(0, 0.02) for _ in range(n)]
    orthogonal = math.sqrt(max(1 - correlation ** 2, 0))
    returns_b = [correlation * ra + orthogonal * nb for ra, nb in zip(returns_a, noise)]

    bars_a = make_bars(base_price_a, returns_a, start_date)
    bars_b = make_bars(base_price_b, returns_b, start_date)
    return bars_a, bars_b
