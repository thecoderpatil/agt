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

# Sectors / sub-industries permanently excluded from the Wheel
# candidate universe. Two kinds of exclusions coexist here:
#
# 1. QUALITY exclusions (original C1/C2 set) — sectors with
#    structural Wheel incompatibility due to event risk,
#    binary outcomes, or capital intensity:
#      Airlines, Biotechnology, Pharmaceuticals
#
# 2. STRUCTURAL exclusions (added C3.6) — non-operating-corp
#    legal structures whose fundamentals break the Phase 3
#    thresholds for reasons that have nothing to do with
#    business quality. Phase 3's Altman Z, FCF yield, ND/EBITDA,
#    and ROIC are calibrated for operating C-corporations;
#    REITs, MLPs, BDCs, trusts, and closed-end funds produce
#    mechanically-distorted values in at least one of those
#    metrics regardless of underlying business health.
#
# Yash ruling 2026-04-11 (AGT principle): the Wheel candidate
# universe contains only US-domiciled common-stock C-corporations.
# This is a permanent structural filter, not a quality filter.
#
# Finnhub taxonomy reference: these strings match the literal
# values returned by profile2.finnhubIndustry. Case-insensitive
# matching in _passes_sector (universe.py) absorbs minor casing
# drift. If Finnhub changes its taxonomy substantially, this
# frozenset must be updated.
#
# Known trade-off (Architect dispatch 2026-04-11 C3.6):
# "Oil & Gas Storage & Transportation" and "Asset Management &
# Custody Banks" are bucket-level exclusions. They correctly
# strip MLPs (ET, EPD, MPLX, WES) and BDCs (ARCC, MAIN, BXSL)
# but will also strip some legitimate C-corp operators in those
# spaces (e.g., BLK, TROW). Err on the side of overinclusion —
# cost of false negative (missing BLK) < cost of false positive
# (admitting ARCC, whose fundamentals will mechanically break
# Phase 3). Revisit with a narrower Finnhub-taxonomy-aware
# filter in a follow-up sprint if post-C3.6 universe feels wrong.
EXCLUDED_SECTORS: frozenset[str] = frozenset({
    # ─── QUALITY exclusions (C1/C2) ───
    "Airlines",
    "Biotechnology",
    "Pharmaceuticals",
    # ─── STRUCTURAL exclusions (C3.6) — REITs ───
    "REIT",
    "REITs",
    "Real Estate Investment Trusts",
    "Equity Real Estate Investment Trusts (REITs)",
    "Mortgage Real Estate Investment Trusts (REITs)",
    "Real Estate Investment Trusts (REITs)",
    # ─── STRUCTURAL exclusions (C3.6) — MLPs and partnerships ───
    "Master Limited Partnerships",
    "MLPs",
    "Oil & Gas Storage & Transportation",  # MLP-heavy Finnhub bucket
    # ─── STRUCTURAL exclusions (C3.6) — BDCs and closed-end funds ───
    "Business Development Companies",
    "BDCs",
    "Closed-End Funds",
    "Asset Management & Custody Banks",  # BDC-heavy Finnhub bucket
    # ─── STRUCTURAL exclusions (C3.6) — Trusts and other non-corp ───
    "Trust",
    "Trusts",
    "Royalty Trusts",
    # ─── STRUCTURAL exclusions (C3.6) — SPACs and shell entities ───
    "Special Purpose Acquisition Companies",
    "Blank Checks",
    "Shell Companies",
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
# ─── Phase 3: fundamentals thresholds ────────────────────────────
# All thresholds per Architect dispatch 2026-04-11 (C3 greenlight).
# A candidate must pass ALL five filters to survive Phase 3.
#
# DELTA from C2 placeholder values: the dispatch loosens four of the
# five gates relative to the original Master Specification reading
# (which appears to have been calibrated tight enough to yield zero
# survivors). These are the authoritative working values.

# Altman Z-Score: credit-risk composite. Z > 3.0 = "safe zone".
# Rulebook rationale: we do not sell CSPs on names with meaningful
# bankruptcy-cycle exposure. Wheel assignment must not land us in a
# distressed name at the bottom of a credit cycle.
# Predicate: altman_z > MIN_ALTMAN_Z (strict greater-than per dispatch)
MIN_ALTMAN_Z: float = 3.0

# Free cash flow yield: FCF / market cap. Minimum 4%.
# Rulebook rationale: positive, material FCF is the cleanest signal
# that the company is self-funding and not dilution-dependent.
# Predicate: fcf_yield >= MIN_FCF_YIELD
MIN_FCF_YIELD: float = 0.04

# Net Debt / EBITDA: leverage. Maximum 3.0.
# Rulebook rationale: above 3.0x, interest coverage risk rises
# sharply in a rates-up environment; assignment risk on the CSP
# and recovery risk on subsequent Wheel cycles both increase.
# Predicate: net_debt_to_ebitda <= MAX_NET_DEBT_TO_EBITDA
MAX_NET_DEBT_TO_EBITDA: float = 3.0

# Return on invested capital: minimum 10%.
# Rulebook rationale: ROIC below WACC (call it ~8% as a floor)
# means the company is destroying value on reinvestment. Wheel
# assignment into value-destroying names is a structural loser.
# Predicate: roic >= MIN_ROIC
MIN_ROIC: float = 0.10

# Short interest as % of float: maximum 10%.
# Rulebook rationale: high short interest above 10% signals
# institutional skepticism and raises the probability of a
# negative-surprise drawdown during our hold period.
# Predicate: short_interest_pct <= MAX_SHORT_INTEREST
MAX_SHORT_INTEREST: float = 0.10

# Default effective tax rate when ticker.info["effectiveTaxRate"] is
# unavailable. 21% matches the US federal corporate rate post-TCJA.
# Used for the NOPAT computation in ROIC: nopat = ebit * (1 - tax).
DEFAULT_EFFECTIVE_TAX_RATE: float = 0.21


# ─── Phase 3.5: correlation fit thresholds ───────────────────────
# Per Architect dispatch + Yash ruling 2026-04-11 (C3.5 greenlight).
# Phase 3.5 sits between fundamentals (Phase 3) and volatility (Phase 4),
# rejecting candidates that are too closely correlated with the existing
# Wheel book. Global fit — no per-household routing. Wheel candidate
# universe is identical across all households per Yash's ruling.

# Rule 4 correlation threshold. A candidate is rejected if its
# |correlation| with ANY existing holding exceeds this value.
# Pearson correlation of daily returns.
# Rationale: Portfolio Risk Rulebook v10 Rule 4. Tightening below
# 0.60 starves the candidate pool; loosening above 0.60 admits
# closet-clone exposure. Do NOT tune without an Architect amendment.
# Predicate: max(|corr(candidate, h)| for h in effective_holdings)
#            <= MAX_HOLDING_CORRELATION
MAX_HOLDING_CORRELATION: float = 0.60

# Correlation window: trailing trading days used for the pairwise
# correlation computation. The Phase 2 dataframe covers ~295 trading
# days (14mo); we slice the last 90 for correlation. Rationale:
# 90 days balances stability (enough sample) against regime sensitivity
# (a 2-year window would wash out recent correlation changes).
CORRELATION_WINDOW_DAYS: int = 90

# Minimum return observations that must overlap between a candidate
# and the holdings window. Below this, the correlation is not
# trustworthy and the candidate is dropped fail-closed. Rationale:
# an IPO with 20 days of history can produce spurious near-zero
# correlation against the book. 60 trading days is a ~3-month
# minimum track record.
MIN_CORRELATION_OVERLAP_DAYS: int = 60

# Tickers excluded from the "current holdings" list when computing
# Phase 3.5 correlation fit. These are residual / fully-amortized /
# legacy positions, not active Wheel state. New CSP candidates should
# be evaluated against the active Wheel book only.
# Yash ruling 2026-04-11: the Wheel candidate universe is identical
# across all households, so this list is global, not per-household.
CORRELATION_HOLDINGS_EXCLUSIONS: frozenset[str] = frozenset({
    "SLS",
    "GTLB",
    "TRAW.CVR",
})


# ─── Phase 4: volatility / event armor thresholds ───────────────
# Per Architect dispatch + probe verification 2026-04-11 (C4 greenlight).
# Phase 4 sits between Phase 3.5 correlation and Phase 5 option chains.
# Uses LIVE IBKR reqHistoricalDataAsync for IVR (Option D) and the
# existing YFinanceCorporateIntelligenceProvider for earnings / ex-div /
# pending corporate action gates.

# Minimum IV Rank. A candidate must be in at least the 30th percentile
# of its own trailing 1-year IV range. Rationale: Wheel sellers monetize
# IV richness. Names at the 5th percentile of their own IV history are
# cheap-premium names — yield will not compensate for risk. 30% is a
# relatively permissive floor; can be tuned upward if Phase 4 output
# is too fat. Do NOT tune below 20% without an Architect amendment.
# Predicate: ivr_pct >= MIN_IVR_PCT
MIN_IVR_PCT: float = 30.0

# Earnings window: no CSP may be initiated within this window of a
# scheduled earnings release. Rulebook Rule 7 CSP earnings buffer
# says 7 calendar days. We use 10 here to add a 3-day safety margin
# against earnings date imprecision in the yfinance corporate
# calendar cache. The Wall Street Horizon replacement at deployment
# will allow tightening to 7.
# Predicate: 0 <= days_to_earnings <= EARNINGS_BLACKOUT_DAYS → drop
EARNINGS_BLACKOUT_DAYS: int = 10

# Ex-dividend blackout: short calls face early-assignment risk on the
# trading day before ex-dividend. We avoid opening new Wheel positions
# on names with ex-div within this window.
# Predicate: 0 <= days_to_ex_div <= EX_DIV_BLACKOUT_DAYS → drop
EX_DIV_BLACKOUT_DAYS: int = 5

# IBKR historical IV fetch duration string passed to
# reqHistoricalDataAsync. "1 Y" produces ~252 trading-day bars, which
# is the standard 52-week IV Rank denominator. Probe verification
# 2026-04-11 confirmed AAPL=249, MSFT=250, SPY=250 bars from this call.
IV_HISTORY_DURATION: str = "1 Y"

# IVR minimum bar count — candidates with fewer than this many valid
# IV bars are dropped fail-closed. Rationale: a short IV history
# produces unstable percentile rankings. 200 bars ~= 10 months of
# trading days, sufficient for a meaningful 52w range.
MIN_IV_BARS: int = 200

# Per-candidate IBKR rate limit courtesy delay (seconds). IBKR caps
# historical data requests at ~60/minute on default market data
# subscriptions. 0.1s between calls keeps ~15 candidates under the
# cap with headroom.
IBKR_HIST_DATA_COURTESY_DELAY_S: float = 0.1


# ─── Phase 5: IBKR option chain walker ──────────────────────────
# Per Architect dispatch + Yash ruling 2026-04-11 (C5 greenlight).
# Phase 5 sits between Phase 4 (vol/event armor) and Phase 6 (RAY
# filter). Walks the option chain for each VolArmorCandidate and
# emits one StrikeCandidate per valid (expiry, strike) pair. Phase 6
# picks the winners — Phase 5 just produces the universe.
#
# Data access: agt_equities.ib_chains.get_expirations +
# get_chain_for_expiry. chain_walker.py never calls ib_async
# directly; all IBKR interaction is routed through ib_chains.

# Number of nearest Friday expiries to walk per candidate.
# Yash ruling 2026-04-11: two nearest Fridays, no weekly/monthly
# distinction. Phase 5 takes the first N future Friday expiries
# from get_expirations() regardless of whether they are weeklies
# or monthlies — both trade, both are liquid.
CHAIN_WALKER_EXPIRY_COUNT: int = 2

# Minimum DTE floor. We don't want expiries that land within 1
# trading day (0 or 1 DTE) — those carry assignment risk before
# operators can react. Rulebook CSP window is "3-10 DTE"
# historically but we cast a slightly wider net here and let
# Phase 6 narrow.
CHAIN_WALKER_MIN_DTE: int = 2

# Maximum DTE ceiling. Anything beyond 21 DTE is outside the
# weekly CSP framework and gets filtered out regardless of whether
# it happens to be one of the two nearest expiries.
CHAIN_WALKER_MAX_DTE: int = 21

# Strike filter: we walk puts BELOW spot (OTM puts for CSPs).
# Floor: lowest_low_21d from Phase 2 — no put below the 21-day
# low because yield drops off and risk/reward inverts. Ceiling:
# spot itself (nearest-to-the-money put is the highest yield).
# No tunable constant here — the filter uses per-candidate
# spot + lowest_low_21d directly at call time.

# Minimum mid price for a strike to be considered. Strikes with
# mid < $0.05 have effectively zero yield and unreliable quotes.
# Matches MODE1_ABSOLUTE_BID_FLOOR convention in the Rulebook.
CHAIN_WALKER_MIN_MID: float = 0.05

# Per-ticker rate limit courtesy delay between the two expiry
# fetches. ib_chains.get_chain_for_expiry already has an internal
# 2-second sleep waiting for reqMktData to populate. This adds
# a small additional delay between the TWO expiry calls per
# ticker to avoid overlapping snapshot subscriptions.
CHAIN_WALKER_INTER_EXPIRY_DELAY_S: float = 0.5

# ─── Phase 5 strike band (C6.1 fix — interval-based floor) ──────
# Per Architect dispatch 2026-04-11 (C6.1 greenlight), following
# paper-run triage of 2026-04-11 CHRW failure.
#
# The C5 implementation used lowest_low_21d as the Phase 5 strike
# band lower bound. In the 2026-04-11 paper run, CHRW was trading
# at $163.49 with a 21-day low of $160.45 — a band of only $3.04
# wide. IBKR's reqSecDefOptParams returned zero strikes in that
# band because CHRW does not actually list $2.50 strikes at the
# $160 price level despite OCC rules suggesting it could, and the
# chain fetch raised "No strikes in range" for every expiry,
# dropping CHRW from the run entirely.
#
# Fix: compute the band floor as spot - (expected_interval × 5).
# The "5" guarantees at least 5 strike increments below spot. The
# expected_interval is CONSERVATIVELY overestimated — we assume
# $5 grids where OCC rules suggest $2.50 is possible, because
# real-world chains are often wider than theoretical rules allow.

# Strike interval estimates for computing the Phase 5 strike band
# lower bound. CONSERVATIVE — intentionally overestimates interval
# width to guarantee walkable bands even for illiquid names whose
# actual strike grid is wider than OCC rules suggest.
#
# Real-world example (2026-04-11 paper run): CHRW at $163 spot had
# zero strikes in the band [$160.45, $163.49] despite OCC rules
# saying $2.50 strikes should be available. Conservative sizing
# with $5 interval (not $2.50) prevents this class of failure.
#
# These are NOT the actual strike intervals IBKR returns via
# reqSecDefOptParams — they are estimates used ONLY to compute the
# band lower bound in chain_walker.py. The actual chain fetched
# from IBKR returns whatever strikes the exchange actually lists.
STRIKE_INTERVAL_UNDER_25: float = 2.5    # real range $1-$2.50, we assume $2.50
STRIKE_INTERVAL_UNDER_100: float = 5.0   # real range $2.50-$5, we assume $5
STRIKE_INTERVAL_100_PLUS: float = 5.0    # real range $5+, we assume $5

# Minimum number of strike increments to guarantee in the Phase 5
# strike band below spot. The band lower bound is computed as
# spot - (expected_interval × CHAIN_WALKER_MIN_STRIKES_IN_BAND).
CHAIN_WALKER_MIN_STRIKES_IN_BAND: int = 5


# ─── Phase 6: RAY (Ratio of Annualized Yield) filter ────────────
# Per Architect dispatch + Yash ruling 2026-04-11 (C6 greenlight).
# Phase 6 is the terminal screener phase. Pure in-memory filter —
# no network, no database, no provider. Takes a StrikeCandidate
# list from Phase 5 and returns RAYCandidate entries whose
# annualized_yield falls within [MIN_RAY * 100, MAX_RAY * 100]
# inclusive. Phase 6 does NOT sort, rank, or select winners —
# downstream consumers decide presentation.

# RAY band: the canonical 30%/130% annualized yield window defined
# by Rulebook Rule 7 Mode 2 CSP Operating Procedure. A candidate
# strike must have annualized_yield in [MIN_RAY * 100, MAX_RAY * 100]
# inclusive to survive Phase 6.
#
# Below MIN_RAY: yield doesn't compensate for the risk of selling
# premium in the current market. Wait for IV expansion.
#
# Above MAX_RAY: unusually rich premium signals either a pricing
# anomaly, an impending event not yet in our data, or a data
# quality issue. Treat as too-hot-to-handle.
#
# Rationale: Rulebook Rule 7 Mode 2 Step 1 and Step 2 define the
# 30%/130% band. This is the canonical AGT Wheel capture window
# under Act 60. Do NOT tune without an Architect amendment.
MIN_RAY: float = 0.30
MAX_RAY: float = 1.30


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
