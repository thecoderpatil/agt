"""
agt_equities.screener.types — Inter-phase handoff dataclasses.

Each phase of the screener pipeline emits a strongly-typed frozen dataclass
that downstream phases consume. The frozen=True constraint enforces
immutability across phase boundaries — no phase can mutate an upstream
candidate's attributes, only construct new ones from them.

Why per-phase types instead of one progressively-populated Candidate dict:

  1. Type safety — downstream code that needs `current_price` can't
     accidentally consume a Phase 1 ticker that doesn't have one yet.
  2. Audit trail — looking at any candidate object tells you exactly
     which phase produced it.
  3. Test fixtures — building a TechnicalCandidate in a unit test
     requires explicitly populating every field, so tests can't drift
     out of sync with the production phase outputs silently.
  4. Forward-compat — adding a new field to Phase 3's output doesn't
     change the shape of Phase 1 or Phase 2 outputs.

ISOLATION CONTRACT: this module imports only stdlib. It does not import
httpx, yfinance, ib_async, or anything from telegram_bot. Enforced by
tests/test_screener_isolation.py.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UniverseTicker:
    """Output of Phase 1 — a ticker that passed Finnhub Free exclusions.

    Phase 1 verifies:
      - Market cap >= MIN_MARKET_CAP_USD
      - Sector NOT IN EXCLUDED_SECTORS
      - Country NOT IN EXCLUDED_COUNTRIES

    Attributes:
        ticker:         Symbol (uppercase, e.g. "AAPL")
        name:           Company display name (from Finnhub profile2)
        sector:         Finnhub `finnhubIndustry` field (e.g. "Technology")
        country:        Finnhub `country` field (ISO code, e.g. "US")
        market_cap_usd: Market cap in USD (converted from Finnhub's millions)
    """
    ticker: str
    name: str
    sector: str
    country: str
    market_cap_usd: float


@dataclass(frozen=True, slots=True)
class TechnicalCandidate:
    """Output of Phase 2 — a ticker in an active technical pullback.

    Phase 2 verifies (against the most recent close):
      - Current_Price > SMA_200          (long-term uptrend intact)
      - 35 <= RSI_14 <= 45                (oversold but not capitulating)
      - Current_Price <= BBand_Lower * 1.02  (within 2% of lower band)

    Carries forward all Phase 1 fields plus the technical snapshot used
    by the gate. Downstream phases (3, 4, 5, 6) consume `current_price`
    for OTM strike calculation, `bband_lower` for the Phase 5 strike-floor
    sanity check, and the upstream identity fields for cache keying.

    Attributes:
        ticker, name, sector, country, market_cap_usd:
            Phase 1 carry-forward (identical to UniverseTicker fields)
        current_price:  Most recent close (USD)
        sma_200:        200-day simple moving average of close
        rsi_14:         14-period RSI (Wilder's smoothing not used; simple)
        bband_lower:    Lower Bollinger band (20-day SMA - 2 stdev)
        bband_middle:   20-day SMA (middle band, retained for diagnostics)
        bband_upper:    Upper Bollinger band (retained for diagnostics)
        lowest_low_21d: Lowest low of trailing 21 trading days (Phase 5 input)
    """
    # Phase 1 carry-forward
    ticker: str
    name: str
    sector: str
    country: str
    market_cap_usd: float
    # Phase 2 technical snapshot
    current_price: float
    sma_200: float
    rsi_14: float
    bband_lower: float
    bband_middle: float
    bband_upper: float
    lowest_low_21d: float

    @classmethod
    def from_universe(
        cls,
        upstream: UniverseTicker,
        *,
        current_price: float,
        sma_200: float,
        rsi_14: float,
        bband_lower: float,
        bband_middle: float,
        bband_upper: float,
        lowest_low_21d: float,
    ) -> "TechnicalCandidate":
        """Construct a TechnicalCandidate by carrying forward an UniverseTicker.

        Centralizes the carry-forward logic so phase 2 doesn't have to
        re-list every Phase 1 field at every construction site. Adding a
        Phase 1 field in the future requires updating UniverseTicker and
        this classmethod, but no other call site.
        """
        return cls(
            ticker=upstream.ticker,
            name=upstream.name,
            sector=upstream.sector,
            country=upstream.country,
            market_cap_usd=upstream.market_cap_usd,
            current_price=current_price,
            sma_200=sma_200,
            rsi_14=rsi_14,
            bband_lower=bband_lower,
            bband_middle=bband_middle,
            bband_upper=bband_upper,
            lowest_low_21d=lowest_low_21d,
        )
