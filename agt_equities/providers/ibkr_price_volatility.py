"""
agt_equities/providers/ibkr_price_volatility.py — IPriceAndVolatility via ib_async.

Sync interface. Internal ib_async calls are blocking (ib.sleep/ib.run).
Stock spot workaround for error 10089: uses reqHistoricalData last close.

Phase 3A.5c1.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class IBKRPriceVolatilityProvider:
    """IPriceAndVolatility implementation via ib_async.

    Requires an already-connected ib_async.IB instance.
    Market data type set by caller (delayed=3, live=1).
    """

    def __init__(self, ib, market_data_mode: str = "delayed"):
        self.ib = ib
        self.mode = market_data_mode
        mdt = 3 if market_data_mode == "delayed" else 1
        try:
            ib.reqMarketDataType(mdt)
        except Exception:
            pass
        self._fallback_counter = {
            "model_greeks_used": 0,
            "historical_fallback_used": 0,
            "total_spot_calls": 0,
        }

    def get_spot(self, ticker: str) -> Optional[float]:
        """Returns last daily close via reqHistoricalData.
        Stock reqMktData fails with error 10089 on default IBKR plans."""
        try:
            from ib_async import Stock
            contract = Stock(ticker, "SMART", "USD")
            self.ib.qualifyContracts(contract)
            bars = self.ib.reqHistoricalData(
                contract, endDateTime="", durationStr="1 D",
                barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
            )
            if bars:
                return float(bars[-1].close)
            return None
        except Exception as exc:
            logger.warning("get_spot(%s) failed: %s", ticker, exc)
            return None

    def get_macro_index(self, symbol: str) -> Optional[float]:
        """VIX/SPX index routing via reqMktData."""
        try:
            from ib_async import Index
            if symbol.upper() == "VIX":
                contract = Index("VIX", "CBOE")
            elif symbol.upper() == "SPX":
                contract = Index("SPX", "CBOE")
            else:
                logger.warning("Unknown macro index: %s", symbol)
                return None
            self.ib.qualifyContracts(contract)
            ticker = self.ib.reqMktData(contract, "", False, False)
            self.ib.sleep(2)
            val = ticker.last if ticker.last and not math.isnan(ticker.last) else None
            self.ib.cancelMktData(contract)
            return float(val) if val else None
        except Exception as exc:
            logger.warning("get_macro_index(%s) failed: %s", symbol, exc)
            return None

    def get_historical_daily_bars(
        self, ticker: str, lookback_days: int,
    ) -> List[Tuple[date, float]]:
        """Returns dividend-adjusted daily closes as (date, close) tuples."""
        try:
            from ib_async import Stock
            contract = Stock(ticker, "SMART", "USD")
            self.ib.qualifyContracts(contract)
            bars = self.ib.reqHistoricalData(
                contract, endDateTime="", durationStr=f"{lookback_days} D",
                barSizeSetting="1 day", whatToShow="ADJUSTED_LAST",
                useRTH=True, formatDate=1,
            )
            result = []
            for b in bars:
                bar_date = b.date if isinstance(b.date, date) else date.fromisoformat(str(b.date))
                result.append((bar_date, float(b.close)))
            return result
        except Exception as exc:
            logger.warning("get_historical_daily_bars(%s) failed: %s", ticker, exc)
            return []

    def get_factor_matrix(
        self, tickers: List[str], benchmark: str = "SPY",
        trading_days: int = 126,
    ) -> dict:
        """Returns aligned daily log-returns matrix as dict of lists.
        Keys = ticker symbols (including benchmark), values = list[float] of log returns."""
        all_symbols = list(set(list(tickers) + [benchmark]))
        series_dict: dict[str, list[tuple[date, float]]] = {}
        for symbol in all_symbols:
            bars = self.get_historical_daily_bars(symbol, trading_days + 30)
            if bars:
                series_dict[symbol] = bars

        if not series_dict:
            return {}

        # Find common dates across all series
        date_sets = [set(d for d, _ in bars) for bars in series_dict.values()]
        if not date_sets:
            return {}
        common_dates = sorted(set.intersection(*date_sets))
        common_dates = common_dates[-(trading_days + 1):]
        if len(common_dates) < 3:
            return {}

        # Build aligned close dict
        close_by_sym: dict[str, dict[date, float]] = {}
        for sym, bars in series_dict.items():
            close_by_sym[sym] = {d: c for d, c in bars}

        # Compute log returns
        result: dict[str, list[float]] = {sym: [] for sym in series_dict}
        for i in range(1, len(common_dates)):
            prev, curr = common_dates[i - 1], common_dates[i]
            for sym in series_dict:
                p = close_by_sym[sym].get(prev, 0)
                c = close_by_sym[sym].get(curr, 0)
                if p > 0 and c > 0:
                    result[sym].append(math.log(c / p))
                else:
                    result[sym].append(0.0)

        return result

    def get_volatility_surface(
        self, ticker: str,
    ):
        """Returns IV30, RV30, IV rank (or None), VRP.
        IV30 from ATM option chain interpolation.
        RV30 from trailing 30d log returns.
        IV rank from bucket3_macro_iv_history (None until 252-day bootstrap).
        """
        from agt_equities.market_data_dtos import VolatilityMetricsDTO
        try:
            # RV30 from historical bars
            bars = self.get_historical_daily_bars(ticker, 45)
            if len(bars) < 21:
                return None
            recent = bars[-31:]  # last 30 trading days + 1
            log_rets = []
            for i in range(1, len(recent)):
                p = recent[i - 1][1]
                c = recent[i][1]
                if p > 0:
                    log_rets.append(math.log(c / p))
            if not log_rets:
                return None
            rv_30 = (sum(r ** 2 for r in log_rets) / len(log_rets)) ** 0.5 * (252 ** 0.5)

            # IV30 from nearest-ATM option
            iv_30 = self._fetch_atm_iv(ticker)
            if iv_30 is None:
                return None

            vrp = iv_30 - rv_30

            return VolatilityMetricsDTO(
                symbol=ticker,
                iv_30=round(iv_30, 4),
                rv_30=round(rv_30, 4),
                iv_rank=None,  # populated by eod_macro_sync after 252-day bootstrap
                vrp=round(vrp, 4),
                sample_date=date.today(),
                iv_rank_sample_days=0,
            )
        except Exception as exc:
            logger.warning("get_volatility_surface(%s) failed: %s", ticker, exc)
            return None

    def _fetch_atm_iv(self, ticker: str) -> Optional[float]:
        """Extract ATM IV from nearest 30-DTE option chain."""
        try:
            from ib_async import Stock, Option
            stock = Stock(ticker, "SMART", "USD")
            self.ib.qualifyContracts(stock)
            chains = self.ib.reqSecDefOptParams(
                stock.symbol, "", stock.secType, stock.conId
            )
            if not chains:
                return None

            chain = chains[0]
            today = date.today()
            target_dte = 30
            # Find closest expiry to 30 DTE
            best_exp = None
            best_diff = 999
            for exp_str in chain.expirations:
                exp_date = date(int(exp_str[:4]), int(exp_str[4:6]), int(exp_str[6:]))
                diff = abs((exp_date - today).days - target_dte)
                if diff < best_diff:
                    best_diff = diff
                    best_exp = exp_str

            if not best_exp:
                return None

            # Get spot for ATM strike selection
            spot = self.get_spot(ticker)
            if not spot:
                return None

            # Find nearest ATM strike
            atm_strike = min(chain.strikes, key=lambda s: abs(s - spot))
            opt = Option(ticker, best_exp, atm_strike, "C", "SMART")
            self.ib.qualifyContracts(opt)
            t = self.ib.reqMktData(opt, "", False, False)
            self.ib.sleep(3)
            iv = None
            if t.modelGreeks and t.modelGreeks.impliedVol:
                iv = float(t.modelGreeks.impliedVol)
            self.ib.cancelMktData(opt)
            return iv
        except Exception as exc:
            logger.warning("_fetch_atm_iv(%s) failed: %s", ticker, exc)
            return None

    @property
    def fallback_stats(self) -> dict:
        return dict(self._fallback_counter)
