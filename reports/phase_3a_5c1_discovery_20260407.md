# Phase 3A.5c1 Discovery Report — Data Layer Foundation

**Date:** 2026-04-07
**Status:** STOP — awaiting Architect review before implementation
**Tests:** 255/255 (no changes)

---

## 1. Direct ib_async Call Site Inventory (Q1)

**Total: 0 in agt_equities/ or agt_deck/. ~25 in telegram_bot.py.**

`agt_equities/` and `agt_deck/` have ZERO direct ib_async imports or calls. All ib_async usage is confined to `telegram_bot.py` and `data_provider.py` (the 3A.5a IBKRProvider).

| File:Line | Call | Classification |
|-----------|------|---------------|
| `telegram_bot.py:39` | `import ib_async` | Top-level import |
| `telegram_bot.py:1037` | `ib: ib_async.IB \| None = None` | Global IB connection |
| `telegram_bot.py:1091-1121` | `ensure_ib_connected()` + `reqMarketDataType(4)` | (c) Stays direct — connection management |
| `telegram_bot.py:1158-1187` | Spot price batch + beta via yfinance fallback | (b) IPriceAndVolatility |
| `telegram_bot.py:1247` | `get_ib_option_expirations()` | (b) IOptionsChain |
| `telegram_bot.py:1554-1611` | `accountSummaryAsync()` | (b) IAccountState |
| `telegram_bot.py:2410` | `reqMarketDataType(4)` | (c) Connection config |
| `telegram_bot.py:2507` | `accountSummaryAsync()` | (b) IAccountState |
| `telegram_bot.py:2691-2705` | `ib_async.Option()` construction for box spreads | (d) Order execution — out of scope |
| `telegram_bot.py:3035-3042` | `reqMktData` for stock reference price | (b) IPriceAndVolatility |
| `telegram_bot.py:3298-3428` | `/orders` live dashboard — `reqContractDetails`, `reqMktData` | (d) UI display — stays direct for now |
| `telegram_bot.py:5264-5308` | IV fetch for `/cc` ladder | (b) IOptionsChain |
| `telegram_bot.py:6261-6273` | Order construction — `ib_async.Order()` | (d) Order execution |
| `telegram_bot.py:6361` | `ib_async.Option()` for exit | (d) Order execution |
| `telegram_bot.py:6478-6509` | Live positions fetch | (b) IAccountState |
| `telegram_bot.py:7152-7156` | Conviction tier from yfinance | (b) ICorporateIntelligence |
| `telegram_bot.py:7910` | `ib_async.Option()` for Dynamic Exit CIO payload | (d) Order execution |
| `telegram_bot.py:8274-8286` | Spot prices IBKR + yfinance fallback | (b) IPriceAndVolatility |

**Summary:**
- (a) Already migrated: 2 methods in data_provider.py (get_historical_daily_bars, get_account_summary)
- (b) Needs migration to new ABCs: ~8 call sites
- (c) Connection management — stays direct: 3 call sites
- (d) Order execution / UI — stays direct (Phase 3D scope): ~6 call sites

---

## 2. IBKRProvider Migration Map (Q2)

| Current Method | Target ABC | Status |
|----------------|-----------|--------|
| `get_historical_daily_bars()` | **IPriceAndVolatility** | Real, stays |
| `get_account_summary()` | **IAccountState** (via state_builder) | Real, stays |
| `get_option_chain()` | **IOptionsChain** (new `get_chain_slice()`) | Stub → implement in 3A.5c1 |
| `get_fundamentals()` | **ICorporateIntelligence** | Stub → implement in 3A.5c1 |
| `get_earnings_date()` | **ICorporateIntelligence** | Stub → implement in 3A.5c1 |

**Recommendation: Option (a) — new ABCs alongside existing IBKRProvider.**

The existing IBKRProvider stays untouched. New ABC implementations are separate classes in new files (e.g., `agt_equities/providers/price_vol.py`, `agt_equities/providers/options_chain.py`, etc.). Evaluators that need the new interfaces import them directly. Migration of old call sites happens in 3A.5c2/3D.

This avoids breaking R4/R5/R6 evaluators and the existing FakeProvider test infrastructure.

---

## 3. DTO Definitions (Q3)

```python
@dataclass(frozen=True)
class OptionContractDTO:
    """Single option contract with pricing and Greeks."""
    symbol: str
    expiry: str                    # YYYYMMDD
    strike: float
    right: str                     # 'C' or 'P'
    bid: float
    ask: float
    last: float | None
    volume: int | None
    open_interest: int | None
    delta: float | None            # from modelGreeks
    gamma: float | None
    theta: float | None
    vega: float | None
    implied_vol: float | None
    spot_price_used: float | None  # modelGreeks.undPrice or fallback
    extrinsic_value: float         # clamped >= 0.0
    pricing_drift_ms: int          # 0 if from modelGreeks.undPrice
    is_extrinsic_stale: bool       # True if drift_ms > threshold
    
    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else 0.0
    
    @property
    def spread_pct(self) -> float:
        """Bid-ask spread as % of mid. Filtered at evaluator layer."""
        m = self.mid
        return (self.ask - self.bid) / m * 100 if m > 0 else float('inf')
    
    @property
    def annualized_return(self) -> float:
        """(Premium / Strike) * (365 / DTE). For 30%/130% framework."""
        from datetime import date
        dte = (date.fromisoformat(self.expiry[:4] + '-' + self.expiry[4:6] + '-' + self.expiry[6:]) - date.today()).days
        if dte <= 0 or self.strike <= 0:
            return 0.0
        return (self.mid / self.strike) * (365 / dte) * 100


@dataclass(frozen=True)
class VolatilityMetricsDTO:
    """30-day IV, RV, IV rank, and VRP for a ticker."""
    ticker: str
    iv_30: float | None            # ATM interpolated 30-day IV
    rv_30: float | None            # trailing 30-day realized vol
    iv_rank: float | None          # percentile rank vs 252-day history (None until bootstrapped)
    vrp: float | None              # iv_30 - rv_30 (None if either input None)


class CorporateActionType(Enum):
    NONE = "NONE"
    MERGER = "MERGER"
    SPINOFF = "SPINOFF"
    SPECIAL_DIV = "SPECIAL_DIV"
    TENDER = "TENDER"
    OTHER = "OTHER"


@dataclass(frozen=True)
class CorporateCalendarDTO:
    """Earnings, dividends, and corporate action schedule."""
    ticker: str
    next_earnings: date | None
    ex_dividend_date: date | None
    dividend_amount: float | None
    pending_corporate_action: CorporateActionType


@dataclass(frozen=True)
class ConvictionMetricsDTO:
    """R8 Gate 1 conviction tier inputs from fundamentals."""
    ticker: str
    eps_positive: bool | None      # None = data unavailable
    revenue_above_sector_median: bool | None
    has_analyst_downgrade: bool | None
    operating_margin: float | None  # as decimal (0.15 = 15%)
```

**Data source availability:**

| Field | Source | Available? |
|-------|--------|-----------|
| OptionContractDTO.delta | IBKR modelGreeks | YES (confirmed Q4) |
| OptionContractDTO.spot_price_used | IBKR modelGreeks.undPrice | YES (4/5 samples, Q6) |
| OptionContractDTO.bid/ask | IBKR delayed | YES (confirmed Q7) |
| VolatilityMetricsDTO.iv_30 | ATM option chain IV interpolation | YES (modelGreeks.impliedVol confirmed Q4) |
| VolatilityMetricsDTO.rv_30 | reqHistoricalData daily closes | YES (proven 3A.5a) |
| VolatilityMetricsDTO.iv_rank | bucket3_macro_iv_history | NO until 252-day bootstrap |
| CorporateCalendarDTO.next_earnings | **yfinance** (TEMPORARY) | YES but cold-path only |
| CorporateCalendarDTO.ex_dividend_date | **yfinance** | YES but cold-path only |
| CorporateCalendarDTO.pending_corporate_action | **Manual / corp_action_quarantine table** | Partial — enum, not automated |
| ConvictionMetricsDTO.* | **yfinance** (TEMPORARY) | YES but cold-path only |

**Flag:** `pending_corporate_action` is NOT automatically detectable from IBKR feeds. Current system uses `corp_action_quarantine` table (manually populated). The ENUM provides the type classification, but detection remains manual until a Reuters/WSH feed is adopted.

---

## 4. get_chain_slice() Feasibility (Q4)

**Feasibility: YES.**

Traced call for `get_chain_slice("ADBE", "C", 14, 21, 0.30)`:

1. `reqSecDefOptParams("ADBE", ...)` → returns chain definitions with all expirations + strikes. 20 chain definitions returned. **~200ms.**
2. Filter expirations in [today+14d, today+21d] → found `20260424` (17 DTE)
3. Filter strikes near ATM (230-260 for ADBE at ~$240) → 8 strikes
4. `qualifyContracts(*options)` → 3 contracts qualified. **~300ms.**
5. `reqMktData(contract)` per contract → returns bid, ask, modelGreeks with delta. **~2-3s** (delayed feed warm-up).
6. Filter where abs(delta) <= 0.30

**Pacing:** 50 req/sec limit not a concern. A typical chain slice is 5-15 contracts → 15-45 API calls. Well within budget.

**Estimated end-to-end latency:** 3-5 seconds for first call (cold cache), <2s for subsequent (warm).

---

## 5. get_volatility_surface() Feasibility (Q5)

**Feasibility: YES (with IV rank deferred until bootstrap).**

- **IV30:** Extract from ATM option chain. Take the nearest-ATM call/put pair at ~30 DTE, read `modelGreeks.impliedVol`. Confirmed available (Q4 showed iv=0.431 for ADBE 240C). Interpolate between two nearest ATM strikes for precision.
- **RV30:** `reqHistoricalData("ADBE", 30 days, "1 day", "ADJUSTED_LAST")` → compute annualized stdev of log returns. Proven in 3A.5a.
- **VRP:** `iv_30 - rv_30`. Simple subtraction.
- **IV Rank:** Requires 252 days of IV30 history from `bucket3_macro_iv_history`. Returns `None` until bootstrapped. Falls back gracefully.

---

## 6. Extrinsic Value Atomicity (Q6)

**5-sample test results (ADBE 240C 20260424):**

| Sample | modelGreeks.undPrice | Drift (ms) | Extrinsic |
|--------|---------------------|------------|-----------|
| 1 (cold) | None | 1507 (fallback) | N/A (stock data unavailable) |
| 2 | 239.84 | 0 | $9.12 |
| 3 | 239.84 | 0 | $9.12 |
| 4 | 239.84 | 0 | $9.15 |
| 5 | 239.84 | 0 | $9.15 |

**Key findings:**
- `modelGreeks.undPrice` is available 4/5 times. First request is a cold-start (~2s warm-up), subsequent requests have it.
- When available, drift = 0ms (same snapshot, atomic).
- **Stock fallback doesn't work on delayed feeds** — `reqMktData` on the stock itself returns nan with error 10089 (subscription required). The fallback must use `reqHistoricalData` (last close), not `reqMktData`.
- **Recommended default threshold: 2500ms is fine.** The only fallback scenario is cold-start first request. Once warm, undPrice is always available.

**IMPORTANT:** The fallback path (two-call with stock) needs to use `reqHistoricalData` for the underlying's last close, NOT `reqMktData`. `reqMktData` for equities requires a paid data subscription that this account doesn't have. `reqHistoricalData` with `durationStr="1 D"` works fine (proven in 3A.5a).

---

## 7. Bid/Ask Availability (Q7)

**YES.** Confirmed on delayed feeds (reqMarketDataType(3)):
- ADBE 240C 20260424: bid=8.90, ask=9.35
- spread_pct = (9.35-8.90)/9.125 * 100 = 4.9%

**No hard stop.** Gemini's design assumption is valid.

---

## 8. IV History Schema + EOD Cron (Q8)

**Schema draft:**
```sql
CREATE TABLE IF NOT EXISTS bucket3_macro_iv_history (
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    iv_30 REAL NOT NULL,
    PRIMARY KEY (ticker, trade_date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_iv_hist 
    ON bucket3_macro_iv_history(ticker, trade_date DESC);
```

**jobs/eod_macro_sync.py outline:**
1. Reads tickers from `ticker_universe` table (existing, refreshed monthly by `/sync_universe`)
2. For each ticker with active cycles: fetch IV30 via IPriceAndVolatility
3. `INSERT OR REPLACE INTO bucket3_macro_iv_history (ticker, trade_date, iv_30) VALUES (?, ?, ?)`
4. `DELETE FROM bucket3_macro_iv_history WHERE trade_date < date('now', '-400 days')` (retention)
5. **Trigger:** Standalone cron (NOT inside flex_sync.py). Suggest running at 4:30 PM ET (after market close, before flex_sync EOD). Cron entry or APScheduler job.
6. First 252 days: iv_rank returns None (sentinel). After bootstrap: percentile rank computed as `count(hist_iv < current_iv) / count(hist_iv)`.

---

## 9. ICorporateIntelligence Cache Strategy (Q9)

**Recommendation: Bucket 3 table with TTL.**

```sql
CREATE TABLE IF NOT EXISTS bucket3_corporate_cache (
    ticker TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,       -- serialized CorporateCalendarDTO + ConvictionMetricsDTO
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'yfinance'
);
```

- **Staleness:** TTL = 24 hours. Queries check `fetched_at`. If older than 24h, refetch on next query.
- **Cold path:** Only queried during R8 Gate 1 evaluation and `/dynamic_exit` CIO payload generation. NOT in any execution hot loop.
- **Failure handling:** yfinance failure returns cached data if available (any age). If no cache, returns None. R8 uses hardcoded conviction modifier as fallback per Gemini Q1.
- **TEMPORARY markers:** Every yfinance call wrapped with `# DEPLOYMENT: replace with Reuters/WSH feed` comment.

---

## 10. IAccountState Confirmation (Q10)

**state_builder.py is CLEAN. No market-data bleed.**

| Responsibility | In state_builder? | Clean? |
|---------------|-------------------|--------|
| build_correlation_matrix() | YES | YES — calls provider.get_historical_daily_bars() for price data, but this is a DERIVED metric from price data, not a market-data query for its own sake |
| build_account_el_snapshot() | YES | YES — calls provider.get_account_summary() |
| Option chain queries | NO | N/A |
| Fundamentals queries | NO | N/A |

**state_builder IS the correct realization of IAccountState.** It handles account EL + NLV population. No refactoring needed.

**Gap: build_account_nlv() still missing.** Currently, callers manually query `master_log_nav` for per-account NLV. Since we're touching state_builder in 3A.5c1 anyway, recommend adding `build_account_nlv(conn, household_map)` in this sprint. It's a 10-line function reading from `master_log_nav`.

**Recommendation:** Build `build_account_nlv()` in 3A.5c1 (closes the 3A.5a gap cleanly).

---

## 11. Existing yfinance Call Sites (Q11)

| File:Line | Usage | Hot/Cold | Migration Target |
|-----------|-------|----------|-----------------|
| `telegram_bot.py:41` | `import yfinance as yf` | Top-level | — |
| `telegram_bot.py:1175-1187` | Spot price + beta batch | **HOT** — called by `/cc`, `/health` | IPriceAndVolatility |
| `telegram_bot.py:2575-2579` | Single ticker price | Cold — LLM tool | IPriceAndVolatility |
| `telegram_bot.py:3055` | Stock reference price | Cold — `/orders` display | IPriceAndVolatility |
| `telegram_bot.py:5716-5867` | `/sync_universe` — Wikipedia + yfinance GICS enrichment | **COLD** — monthly cron | Stays (genuinely cold, GICS data) |
| `telegram_bot.py:6784-6796` | Spot prices IBKR primary, yfinance fallback | **HOT** — `/cc` scan | IPriceAndVolatility |
| `telegram_bot.py:7152-7156` | Conviction tier fundamentals | Cold — `/dynamic_exit` | ICorporateIntelligence |
| `telegram_bot.py:8274-8286` | Spot prices IBKR + yfinance fallback | **HOT** — `/health` | IPriceAndVolatility |
| `telegram_bot.py:10341` | Monthly universe refresh | COLD — cron | Stays |

**HOT path yfinance calls (lines 1175, 6784, 8274):** These are in `/cc`, `/health`, and related commands. They MUST migrate to IPriceAndVolatility in 3A.5c1 or at latest 3A.5c2.

**COLD path yfinance calls:** `/sync_universe` and conviction tier stay with TEMPORARY markers.

---

## 12. Backward Compatibility (Q12)

**Strategy: Option (a) — new ABCs alongside existing IBKRProvider.**

| Component | 3A.5c1 | 3A.5c2 | Phase 3D |
|-----------|--------|--------|----------|
| Existing IBKRProvider | Untouched | Untouched | Deprecate |
| Existing FakeProvider | Untouched | Untouched | Replace with per-ABC fakes |
| New IPriceAndVolatility | Ship | R8 uses | Hot-path migration |
| New IOptionsChain | Ship | R8 uses | — |
| New ICorporateIntelligence | Ship | R8 uses | — |
| R4/R5/R6 evaluators | Unchanged | Unchanged | Migrate to new ABCs |

No breaking changes in 3A.5c1. New ABCs live in `agt_equities/providers/` alongside existing `data_provider.py`.

---

## 13. Surprises, Gotchas, Hidden Coupling

1. **Stock reqMktData fails on delayed feeds.** Error 10089 — requires paid subscription. Only reqHistoricalData works for equity prices. This means the extrinsic_value fallback path CANNOT use reqMktData for the underlying. Must use reqHistoricalData (last daily close) instead.

2. **modelGreeks cold-start.** First reqMktData on an option returns no modelGreeks (~2s). Subsequent requests have it. The implementation needs a retry/wait loop for first-time queries.

3. **Zero direct ib_async calls in agt_equities/.** The entire rule evaluation layer is already cleanly separated from IBKR. Only telegram_bot.py has direct calls. This is good architecture — the new ABCs only need to be wired into telegram_bot.py call sites.

4. **yfinance in HOT paths.** Three call sites (lines 1175, 6784, 8274) use yfinance for spot prices in `/cc` and `/health` — commands that run multiple times daily. These are the highest-priority migration targets.

5. **corp_action_quarantine is manual.** CorporateActionType.NONE is the default. Detection of corporate actions requires manual operator input or a future Reuters/WSH feed. The DTO provides the enum, but the automation isn't there.

---

## 14. Architect Review Queue

1. **Stock price fallback:** Confirm reqHistoricalData (last close) is acceptable for extrinsic_value fallback instead of reqMktData (unavailable on current plan).
2. **build_account_nlv():** Confirm inclusion in 3A.5c1 scope (closes 3A.5a gap).
3. **Hot-path yfinance migration timing:** Migrate in 3A.5c1 (alongside ABC build) or 3A.5c2 (alongside R8)?
4. **IV rank bootstrap:** 252 days of None is a long time. Should we seed from yfinance historical IV data for a faster bootstrap? Or accept the gradual fill?
5. **EOD cron placement:** Confirm standalone `jobs/eod_macro_sync.py` at 4:30 PM ET, NOT inside flex_sync.py.

---

```
Phase 3A.5c1 discovery | call sites: 25 (8 need migration, 6 order-exec stay)
| DTOs drafted: 4/4 | chain feasibility: YES | vol feasibility: YES
| drift sample: 0ms median (modelGreeks.undPrice available 4/5)
| bid/ask: YES | state_builder: CLEAN | STOP for Architect review
| reports/phase_3a_5c1_discovery_20260407.md
```
