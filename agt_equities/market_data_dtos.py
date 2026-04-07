"""
agt_equities/market_data_dtos.py — Frozen dataclass DTOs for market data.

All DTOs are frozen (immutable). Provider implementations return these;
evaluators and the Cure Console consume them. No business logic here —
just data containers with computed properties.

Phase 3A.5c1 — data layer foundation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Corporate action enum (not a bool — per Architect tightening)
# ---------------------------------------------------------------------------

class CorporateActionType(Enum):
    NONE = "none"
    MERGER = "merger"
    SPINOFF = "spinoff"
    SPECIAL_DIVIDEND = "special_dividend"
    TENDER = "tender"
    OTHER = "other"


# ---------------------------------------------------------------------------
# OptionContractDTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionContractDTO:
    """Single option contract with pricing, Greeks, and atomicity metadata."""
    symbol: str                     # underlying ticker
    strike: float
    right: str                      # 'C' or 'P'
    expiry: date
    dte: int

    # Greeks
    delta: float
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None
    iv: Optional[float] = None

    # Pricing
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0

    # Atomicity — spot source for extrinsic computation
    spot_price_used: float = 0.0
    extrinsic_value: float = 0.0    # clamped at 0.0 minimum
    pricing_drift_ms: int = 0       # 0 if modelGreeks.undPrice path
    is_extrinsic_stale: bool = False
    spot_source: str = "model_greeks"  # 'model_greeks' | 'historical_fallback'

    @property
    def spread_pct(self) -> float:
        """Bid-ask spread as fraction of mid. Filtered at evaluator layer."""
        if self.mid <= 0:
            return 999.0
        return (self.ask - self.bid) / self.mid

    @staticmethod
    def calculate_extrinsic(
        mid: float, spot: float, strike: float, right: str,
    ) -> float:
        """Compute extrinsic value, clamped at 0.0 minimum."""
        if right == "C":
            intrinsic = max(0.0, spot - strike)
        else:
            intrinsic = max(0.0, strike - spot)
        return max(0.0, mid - intrinsic)


# ---------------------------------------------------------------------------
# VolatilityMetricsDTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VolatilityMetricsDTO:
    """30-day IV, RV, IV rank, and VRP for a ticker."""
    symbol: str
    iv_30: float
    rv_30: float
    iv_rank: Optional[float]        # None until 252-day bootstrap completes
    vrp: float                      # iv_30 - rv_30
    sample_date: date
    iv_rank_sample_days: int        # how many days of history (0 if iv_rank is None)


# ---------------------------------------------------------------------------
# CorporateCalendarDTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorporateCalendarDTO:
    """Earnings, dividends, and corporate action schedule."""
    symbol: str
    next_earnings: Optional[date]
    ex_dividend_date: Optional[date]
    dividend_amount: float          # 0.0 if no upcoming dividend
    pending_corporate_action: CorporateActionType
    data_source: str                # 'yfinance_temporary' until deployment
    cached_at: datetime
    cache_age_hours: float


# ---------------------------------------------------------------------------
# ConvictionMetricsDTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConvictionMetricsDTO:
    """R8 Gate 1 conviction tier inputs from fundamentals."""
    symbol: str
    eps_positive: bool
    revenue_above_sector_median: bool
    has_analyst_downgrade: bool
    operating_margin: float         # decimal, e.g. 0.15 = 15%
    data_source: str                # 'yfinance_temporary'
    cached_at: datetime
    cache_age_hours: float
