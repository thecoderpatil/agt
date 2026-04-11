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
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd  # noqa: F401  (type-hint only — keeps stdlib-only isolation)


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


@dataclass(frozen=True, slots=True)
class FundamentalCandidate:
    """Output of Phase 3 — a ticker that passed the fundamental fortress.

    Phase 3 verifies (against TTM/most-recent-quarter yfinance data):
      - Altman Z-Score        > MIN_ALTMAN_Z         (3.0)
      - FCF Yield             >= MIN_FCF_YIELD       (0.04)
      - Net Debt / EBITDA     <= MAX_NET_DEBT_TO_EBITDA (3.0)
      - ROIC                  >= MIN_ROIC            (0.10)
      - Short Interest        <= MAX_SHORT_INTEREST  (0.10)

    Carries forward ALL Phase 1 + Phase 2 fields per the dispatch's
    "downstream phases can keep enriching without re-joining" principle.
    Field name mapping at the carry-forward boundary:
        TechnicalCandidate.current_price  →  FundamentalCandidate.spot
        TechnicalCandidate.bband_middle   →  FundamentalCandidate.bband_mid
    Other field names are unchanged.

    Fields `name` and `lowest_low_21d` are also carried forward beyond
    the dispatch's explicit field list — `lowest_low_21d` is needed by
    Phase 5's strike-floor sanity check, and `name` is operator-readable
    identification in the final output table.
    """
    # Phase 1 carry-forward
    ticker: str
    name: str
    sector: str
    country: str
    market_cap_usd: float
    # Phase 2 carry-forward (renamed per dispatch: spot, bband_mid)
    spot: float
    sma_200: float
    rsi_14: float
    bband_lower: float
    bband_mid: float
    bband_upper: float
    lowest_low_21d: float
    # Phase 3 fundamental fortress fields
    altman_z: float
    fcf_yield: float            # free cash flow / market cap, as fraction
    net_debt_to_ebitda: float   # leverage ratio
    roic: float                 # return on invested capital, as fraction
    short_interest_pct: float   # short interest as fraction of float

    @classmethod
    def from_technical(
        cls,
        upstream: TechnicalCandidate,
        *,
        altman_z: float,
        fcf_yield: float,
        net_debt_to_ebitda: float,
        roic: float,
        short_interest_pct: float,
    ) -> "FundamentalCandidate":
        """Construct a FundamentalCandidate from a TechnicalCandidate.

        Centralizes the carry-forward + field-rename logic so Phase 3
        doesn't have to know about TechnicalCandidate's field naming
        conventions. The two name remappings happen here:
            current_price  →  spot
            bband_middle   →  bband_mid
        """
        return cls(
            ticker=upstream.ticker,
            name=upstream.name,
            sector=upstream.sector,
            country=upstream.country,
            market_cap_usd=upstream.market_cap_usd,
            spot=upstream.current_price,
            sma_200=upstream.sma_200,
            rsi_14=upstream.rsi_14,
            bband_lower=upstream.bband_lower,
            bband_mid=upstream.bband_middle,
            bband_upper=upstream.bband_upper,
            lowest_low_21d=upstream.lowest_low_21d,
            altman_z=altman_z,
            fcf_yield=fcf_yield,
            net_debt_to_ebitda=net_debt_to_ebitda,
            roic=roic,
            short_interest_pct=short_interest_pct,
        )


@dataclass(frozen=True, slots=True)
class Phase2Output:
    """Output of Phase 2 — survivors plus the price history dataframe.

    The dataframe is retained across phase boundaries so Phase 3.5 can
    compute pairwise correlations without a second yfinance download.
    Phase 3 ignores the dataframe; Phase 3.5 consumes it.

    BREAKING CHANGE in C3.5: Phase 2's return type changed from
    `list[TechnicalCandidate]` to `Phase2Output`. Callers that previously
    iterated the result directly must now access `.survivors`.

    Attributes:
        survivors:     List of TechnicalCandidate — tickers that passed
                       trend + RSI + BBand pullback gates.
        price_history: MultiIndex-columns pd.DataFrame from yf.download
                       with group_by="ticker". Shape: (n_days, n_tickers
                       * n_fields). Top column level is the ticker symbol;
                       second level is OHLCV field name. Phase 3.5 slices
                       via `.xs("Close", level=1, axis=1)`.

                       Critically, this contains the FULL Phase 2 universe
                       (post-Phase-1 survivors), not just the Phase 2
                       gate survivors. Phase 3.5 needs history for tickers
                       that were dropped by the pullback gate so they're
                       still available as correlation references.

                       Typed as Any to preserve stdlib-only isolation.
                       Real type is pandas.DataFrame.
    """
    survivors: list["TechnicalCandidate"]
    price_history: Any  # pd.DataFrame — stdlib-only isolation


@dataclass(frozen=True, slots=True)
class CorrelationCandidate:
    """Output of Phase 3.5 — a fundamentally-strong ticker that is also
    uncorrelated with the current Wheel book.

    Phase 3.5 verifies:
        max(|corr(candidate, holding)|) <= MAX_HOLDING_CORRELATION
            across every holding in `current_holdings` (after applying
            CORRELATION_HOLDINGS_EXCLUSIONS)
        AND
        the candidate is NOT already a current holding (post-exclusion)
        AND
        the candidate has >= MIN_CORRELATION_OVERLAP_DAYS of return
        observations overlapping with the holdings window

    Carries forward ALL Phase 1 + Phase 2 + Phase 3 fields. Adds the
    maximum |correlation| against the holdings book + the holding that
    produced it for audit / Phase 6 RAY scoring.
    """
    # All FundamentalCandidate fields (carry-forward, verbatim)
    ticker: str
    name: str
    sector: str
    country: str
    market_cap_usd: float
    spot: float
    sma_200: float
    rsi_14: float
    bband_lower: float
    bband_mid: float
    bband_upper: float
    lowest_low_21d: float
    altman_z: float
    fcf_yield: float
    net_debt_to_ebitda: float
    roic: float
    short_interest_pct: float
    # Phase 3.5 additions
    max_abs_correlation: float
    most_correlated_holding: str

    @classmethod
    def from_fundamental(
        cls,
        upstream: "FundamentalCandidate",
        *,
        max_abs_correlation: float,
        most_correlated_holding: str,
    ) -> "CorrelationCandidate":
        """Construct a CorrelationCandidate from a FundamentalCandidate."""
        return cls(
            ticker=upstream.ticker,
            name=upstream.name,
            sector=upstream.sector,
            country=upstream.country,
            market_cap_usd=upstream.market_cap_usd,
            spot=upstream.spot,
            sma_200=upstream.sma_200,
            rsi_14=upstream.rsi_14,
            bband_lower=upstream.bband_lower,
            bband_mid=upstream.bband_mid,
            bband_upper=upstream.bband_upper,
            lowest_low_21d=upstream.lowest_low_21d,
            altman_z=upstream.altman_z,
            fcf_yield=upstream.fcf_yield,
            net_debt_to_ebitda=upstream.net_debt_to_ebitda,
            roic=upstream.roic,
            short_interest_pct=upstream.short_interest_pct,
            max_abs_correlation=max_abs_correlation,
            most_correlated_holding=most_correlated_holding,
        )


@dataclass(frozen=True, slots=True)
class VolArmorCandidate:
    """Output of Phase 4 — a candidate that passed vol + event armor.

    Phase 4 verifies (in order):
      - IVR >= MIN_IVR_PCT                             (IBKR historical IV)
      - No earnings within EARNINGS_BLACKOUT_DAYS      (CorporateCalendarDTO)
      - No ex-dividend within EX_DIV_BLACKOUT_DAYS     (CorporateCalendarDTO)
      - pending_corporate_action == CorporateActionType.NONE

    Carries forward all CorrelationCandidate fields. Adds 8 new fields
    capturing the IV snapshot and corporate calendar data used for the
    gate decisions. Calendar fields are Optional[date] in the upstream
    DTO but typed as Any here to avoid importing datetime into the
    stdlib-isolated types module.
    """
    # CorrelationCandidate carry-forward (verbatim)
    ticker: str
    name: str
    sector: str
    country: str
    market_cap_usd: float
    spot: float
    sma_200: float
    rsi_14: float
    bband_lower: float
    bband_mid: float
    bband_upper: float
    lowest_low_21d: float
    altman_z: float
    fcf_yield: float
    net_debt_to_ebitda: float
    roic: float
    short_interest_pct: float
    max_abs_correlation: float
    most_correlated_holding: str
    # Phase 4 additions
    ivr_pct: float               # 0-100 percentile
    iv_latest: float             # most recent 30-day IV, decimal (0.278 = 27.8%)
    iv_52w_min: float            # min IV over trailing ~252 trading days
    iv_52w_max: float            # max IV over trailing ~252 trading days
    iv_bars_used: int            # count of historical bars used (audit)
    next_earnings: Any           # date | None (upstream Optional[date])
    ex_dividend_date: Any        # date | None
    calendar_source: str         # data_source from CorporateCalendarDTO

    @classmethod
    def from_correlation(
        cls,
        upstream: "CorrelationCandidate",
        *,
        ivr_pct: float,
        iv_latest: float,
        iv_52w_min: float,
        iv_52w_max: float,
        iv_bars_used: int,
        next_earnings: Any,
        ex_dividend_date: Any,
        calendar_source: str,
    ) -> "VolArmorCandidate":
        """Construct a VolArmorCandidate from a CorrelationCandidate."""
        return cls(
            ticker=upstream.ticker,
            name=upstream.name,
            sector=upstream.sector,
            country=upstream.country,
            market_cap_usd=upstream.market_cap_usd,
            spot=upstream.spot,
            sma_200=upstream.sma_200,
            rsi_14=upstream.rsi_14,
            bband_lower=upstream.bband_lower,
            bband_mid=upstream.bband_mid,
            bband_upper=upstream.bband_upper,
            lowest_low_21d=upstream.lowest_low_21d,
            altman_z=upstream.altman_z,
            fcf_yield=upstream.fcf_yield,
            net_debt_to_ebitda=upstream.net_debt_to_ebitda,
            roic=upstream.roic,
            short_interest_pct=upstream.short_interest_pct,
            max_abs_correlation=upstream.max_abs_correlation,
            most_correlated_holding=upstream.most_correlated_holding,
            ivr_pct=ivr_pct,
            iv_latest=iv_latest,
            iv_52w_min=iv_52w_min,
            iv_52w_max=iv_52w_max,
            iv_bars_used=iv_bars_used,
            next_earnings=next_earnings,
            ex_dividend_date=ex_dividend_date,
            calendar_source=calendar_source,
        )
