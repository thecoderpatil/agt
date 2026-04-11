"""
agt_equities.screener.config — Spec thresholds and exclusion lists.

Single source of truth for every numeric gate, threshold, and exclusion
set referenced by the Act 60 Fortress CSP Screener spec. Centralizing
these here makes the spec auditable in one file and makes future tuning
trivial (no grep across phase modules).

ARCHITECTURAL NOTE: this module is also the seam for future Workstream C
multi-tenancy. When the screener needs to support per-tenant exclusion
lists (e.g. one client wants to allow biotech, another wants to add
defense-sector exclusions), the constants below become tenant-scoped
config-loader returns rather than module-level frozensets. The phase
modules consume them via accessor functions, not direct attribute reads,
so the seam is upgrade-safe.

ISOLATION CONTRACT: this module imports only stdlib. It does not import
httpx, yfinance, ib_async, or anything from telegram_bot. Enforced by
tests/test_screener_isolation.py.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Phase 1 — Universe exclusions (Finnhub Free profile2)
# ---------------------------------------------------------------------------

# Minimum market cap in USD. Spec: $10B+ large/mega-cap fortresses only.
# Finnhub returns marketCapitalization in MILLIONS of USD, so the gate
# is applied as `market_cap_M >= MIN_MARKET_CAP_USD / 1_000_000`.
MIN_MARKET_CAP_USD: int = 10_000_000_000

# Sector exclusions. Spec: Airlines, Biotechnology, Pharmaceuticals.
# Match against Finnhub's `finnhubIndustry` field, which uses these
# exact category names (verified against Finnhub free tier responses).
# Additional case-insensitive matching is applied at filter time so
# minor capitalization drift in the API doesn't break the gate.
EXCLUDED_SECTORS: frozenset[str] = frozenset({
    "Airlines",
    "Biotechnology",
    "Pharmaceuticals",
})

# Country exclusions. Spec: China, Hong Kong, Macau.
# Finnhub's `country` field returns ISO 3166-1 alpha-2 codes ("US", "CN",
# "HK", "MO"). We include both the ISO codes AND the long names so the
# gate is robust against API drift in either direction.
EXCLUDED_COUNTRIES: frozenset[str] = frozenset({
    # ISO codes
    "CN",  # China
    "HK",  # Hong Kong
    "MO",  # Macau
    # Long names (defensive)
    "China",
    "Hong Kong",
    "Macau",
})


# ---------------------------------------------------------------------------
# Phase 2 — Technical pullback (yfinance batch)
# ---------------------------------------------------------------------------
#
# Spec:
#   Current_Price > SMA_200
#   RSI_14 in [35, 45]
#   Current_Price <= Lower_Bollinger_Band_20_2 * 1.02
#
SMA_LONG_WINDOW: int = 200          # 200-day simple moving average
RSI_PERIOD: int = 14                # 14-period RSI
RSI_MIN: float = 35.0               # lower bound (oversold edge)
RSI_MAX: float = 45.0               # upper bound (capitulation edge)
BBAND_PERIOD: int = 20              # 20-day Bollinger band
BBAND_STDEV: float = 2.0            # ±2 standard deviations
BBAND_PULLBACK_TOLERANCE: float = 1.02  # within 2% of lower band counts as touch

# yfinance batch download window. 14 calendar months ≈ 295 trading days,
# guaranteeing the 200-day SMA has no NaN at the most recent observation
# (200 + 30-day buffer for non-trading-day gaps + 60-day startup margin).
YFINANCE_HISTORY_PERIOD: str = "14mo"
YFINANCE_HISTORY_INTERVAL: str = "1d"


# ---------------------------------------------------------------------------
# Phase 3 — Fundamental fortress (yfinance per-ticker)
# ---------------------------------------------------------------------------
#
# Spec:
#   Altman_Z_Score >= 3.0
#   FCF_Yield >= 0.05
#   Net_Debt_to_EBITDA <= 2.0
#   ROIC >= 0.12
#   Short_Interest_Pct_Float <= 0.05
#
ALTMAN_Z_MIN: float = 3.0
FCF_YIELD_MIN: float = 0.05
NET_DEBT_TO_EBITDA_MAX: float = 2.0
ROIC_MIN: float = 0.12
MAX_SHORT_INTEREST_PCT_FLOAT: float = 0.05  # moved from Phase 1 → Phase 3
                                            # under the Finnhub Free pivot


# ---------------------------------------------------------------------------
# Phase 4 — Volatility & event armor
# ---------------------------------------------------------------------------
#
# Spec:
#   Days_To_Next_Earnings > 7 AND Days_To_Next_Earnings > (DTE + 2)
#   Next_Ex_Dividend_Date > Expiration_Date
#   IV_Rank in [30, 70]
#   Absolute_IV_30 <= 0.65
#   IV_30 / Historical_Vol_30 >= 1.25
#
EARNINGS_MIN_DAYS_AHEAD: int = 7        # absolute floor
EARNINGS_DTE_BUFFER_DAYS: int = 2       # additional buffer beyond expiration
IV_RANK_MIN: float = 30.0
IV_RANK_MAX: float = 70.0
ABSOLUTE_IV_30_MAX: float = 0.65
IV_HV_RATIO_MIN: float = 1.25
HV_PERIOD: int = 30                     # 30-day historical volatility window
IVR_BOOTSTRAP_MIN_SNAPSHOTS: int = 30   # below this, IVR gate fails open


# ---------------------------------------------------------------------------
# Phase 5 — Options chain iterator (ib_async — chain_walker.py only)
# ---------------------------------------------------------------------------
#
# Spec:
#   DTE in [7, 21]
#   abs(Delta) in [0.05, 0.25]
#   Strike_Price <= Current_Price * 0.95
#   Strike_Price <= Lowest_Low_Price(Window=21)
#   Open_Interest >= 500
#   (Ask - Bid) / Bid <= 0.10
#
DTE_MIN: int = 7
DTE_MAX: int = 21
DELTA_ABS_MIN: float = 0.05
DELTA_ABS_MAX: float = 0.25
STRIKE_OTM_GAMMA_BUFFER: float = 0.95   # absolute 5% OTM
LOWEST_LOW_WINDOW: int = 21              # ~1 trading month
OPEN_INTEREST_MIN: int = 500
BID_ASK_SPREAD_RATIO_MAX: float = 0.10


# ---------------------------------------------------------------------------
# Phase 6 — Act 60 yield calculator
# ---------------------------------------------------------------------------
#
# Spec:
#   Capital_At_Risk = Strike - Bid (cash-secured)
#   RAY = (Bid / Capital_At_Risk) * (365 / DTE)
#   Output gate: 0.30 <= RAY <= 1.30
#
RAY_MIN: float = 0.30
RAY_MAX: float = 1.30
CASH_SECURED_MULTIPLIER: int = 100      # standard equity option contract size
