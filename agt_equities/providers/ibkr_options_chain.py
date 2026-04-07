"""
agt_equities/providers/ibkr_options_chain.py — IOptionsChain via ib_async.

Sync interface. get_chain_slice() pushes DTE/delta filtering to provider
layer to avoid pacing violations on full-chain pulls.

extrinsic_value computation:
  Primary: modelGreeks.undPrice (zero drift, atomic snapshot)
  Fallback: reqHistoricalData last close (ALWAYS marked is_extrinsic_stale)

Phase 3A.5c1.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, timezone
from typing import List, Optional

from agt_equities.market_data_dtos import OptionContractDTO

logger = logging.getLogger(__name__)

OPTION_PRICING_MAX_DRIFT_MS = int(
    os.environ.get("OPTION_PRICING_MAX_DRIFT_MS", "2500")
)


def _parse_expiry(expiry_str: str) -> date:
    """Parse YYYYMMDD to date."""
    return date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:]))


class IBKROptionsChainProvider:
    """IOptionsChain implementation via ib_async."""

    def __init__(self, ib, price_provider=None):
        """
        Args:
            ib: connected ib_async.IB instance
            price_provider: IBKRPriceVolatilityProvider for spot fallback
        """
        self.ib = ib
        self._price_provider = price_provider
        self._fallback_counter = {
            "model_greeks_path": 0,
            "historical_fallback_path": 0,
        }

    def get_chain_slice(
        self, ticker: str, right: str, min_dte: int, max_dte: int,
        max_delta: Optional[float] = None,
    ) -> List[OptionContractDTO]:
        """Returns option contracts matching the spatial filter."""
        try:
            from ib_async import Stock, Option

            underlying = Stock(ticker, "SMART", "USD")
            self.ib.qualifyContracts(underlying)

            chains = self.ib.reqSecDefOptParams(
                underlying.symbol, "", underlying.secType, underlying.conId,
            )
            if not chains:
                return []

            chain = chains[0]
            today = date.today()

            # Filter expirations by DTE window
            eligible_expiries = []
            for exp_str in chain.expirations:
                exp_date = _parse_expiry(exp_str)
                dte = (exp_date - today).days
                if min_dte <= dte <= max_dte:
                    eligible_expiries.append(exp_str)

            if not eligible_expiries:
                return []

            # Get a rough spot for strike filtering (reduce contract count)
            spot_approx = self._get_spot_approx(ticker)
            if not spot_approx:
                # Without spot, can't filter strikes intelligently
                return []

            # Filter strikes to +/- 20% of spot to limit API calls
            strike_lo = spot_approx * 0.80
            strike_hi = spot_approx * 1.20
            eligible_strikes = [
                s for s in chain.strikes if strike_lo <= s <= strike_hi
            ]

            # Build and qualify contracts
            contracts = []
            for exp_str in eligible_expiries:
                for strike in eligible_strikes:
                    opt = Option(ticker, exp_str, strike, right, "SMART")
                    contracts.append(opt)

            if not contracts:
                return []

            # Qualify in batch (respect pacing)
            self.ib.qualifyContracts(*contracts)

            # Request market data
            tickers_data = []
            for c in contracts:
                if c.conId:  # only qualified contracts
                    t = self.ib.reqMktData(c, "", False, False)
                    tickers_data.append((c, t))

            # Wait for data to populate
            self.ib.sleep(3)

            # Build DTOs
            dtos = []
            for contract, ticker_data in tickers_data:
                dto = self._build_dto(contract, ticker_data, max_delta)
                if dto is not None:
                    dtos.append(dto)

            # Cancel market data subscriptions
            for contract, _ in tickers_data:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass

            return dtos

        except Exception as exc:
            logger.warning("get_chain_slice(%s) failed: %s", ticker, exc)
            return []

    def _get_spot_approx(self, ticker: str) -> Optional[float]:
        """Get approximate spot for strike filtering."""
        if self._price_provider:
            return self._price_provider.get_spot(ticker)
        return None

    def _build_dto(
        self, contract, ticker_data, max_delta: Optional[float],
    ) -> Optional[OptionContractDTO]:
        """Build OptionContractDTO from ib_async contract + ticker data."""
        mg = ticker_data.modelGreeks
        if mg is None or mg.delta is None:
            return None

        # Delta filter
        if max_delta is not None and abs(mg.delta) > max_delta:
            return None

        # Determine spot path
        if mg.undPrice and not math.isnan(mg.undPrice):
            spot = float(mg.undPrice)
            drift_ms = 0
            is_stale = False
            spot_source = "model_greeks"
            self._fallback_counter["model_greeks_path"] += 1
        else:
            # Fallback: historical close
            spot = self._get_spot_approx(contract.symbol)
            if spot is None:
                return None
            drift_ms = -1  # sentinel for fallback
            is_stale = True  # ALWAYS stale on fallback per Architect decision
            spot_source = "historical_fallback"
            self._fallback_counter["historical_fallback_path"] += 1

        # Pricing
        bid = float(ticker_data.bid) if ticker_data.bid and not math.isnan(ticker_data.bid) else 0.0
        ask = float(ticker_data.ask) if ticker_data.ask and not math.isnan(ticker_data.ask) else 0.0
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0

        # Extrinsic value
        extrinsic = OptionContractDTO.calculate_extrinsic(
            mid, spot, contract.strike, contract.right,
        )

        expiry = _parse_expiry(contract.lastTradeDateOrContractMonth)
        dte = (expiry - date.today()).days

        return OptionContractDTO(
            symbol=contract.symbol,
            strike=contract.strike,
            right=contract.right,
            expiry=expiry,
            dte=dte,
            delta=float(mg.delta),
            gamma=float(mg.gamma) if mg.gamma else None,
            vega=float(mg.vega) if mg.vega else None,
            theta=float(mg.theta) if mg.theta else None,
            iv=float(mg.impliedVol) if mg.impliedVol else None,
            bid=bid,
            ask=ask,
            mid=round(mid, 4),
            spot_price_used=spot,
            extrinsic_value=round(extrinsic, 4),
            pricing_drift_ms=drift_ms,
            is_extrinsic_stale=is_stale,
            spot_source=spot_source,
        )

    @property
    def fallback_stats(self) -> dict:
        return dict(self._fallback_counter)
