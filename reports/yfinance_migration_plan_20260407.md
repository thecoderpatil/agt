# R4: yfinance Migration Plan

Generated: 2026-04-07
Status: REPORT ONLY — awaiting Yash review

## Inventory: 23 yfinance call sites across 4 files

### Classification

#### EXECUTION_CRITICAL (11 sites) — feeds live trading decisions

| # | File:Line | Function | yfinance API | Decision it feeds |
|---|-----------|----------|-------------|-------------------|
| 1 | bot:2336 | options lookup | `.options` | Available expirations for /scan |
| 2 | bot:2721 | `_load_yf_stock_reference_price` | `.fast_info` | Reference spot for CC strike selection |
| 3 | bot:2745 | options chain walker | `.options` | CC ladder strike enumeration |
| 4 | bot:7152 | gate 1 strike finder | `.options` | CSP strike selection in /think flow |
| 5 | bot:7442 | dynamic exit handler | `.options` | Expiry validation for /exit orders |
| 6 | bot:8481 | `_find_cc_mode1_strike` | `.options` | Mode 1 CC strike + chain walk |
| 7 | bot:8568 | `_find_cc_harvest_strike` | `.options` | Harvest CC strike + chain walk |
| 8 | pxo:136 | `_scan_ticker_for_csp_candidates` | `.fast_info` | Spot price for CSP screening |
| 9 | pxo:158 | (same function) | `.options` | Available expirations for CSP |
| 10 | pxo:174 | (same function) | `.option_chain()` | Full chain with bids for scoring |
| 11 | vrp:234+252 | `_yf_fetch_atm_iv` | `.options` + `.option_chain()` | ATM IV for VRP veto |

#### DISPLAY_ONLY (7 sites) — spot prices for dashboard/UI

| # | File:Line | Function | yfinance API | Display context |
|---|-----------|----------|-------------|-----------------|
| 12 | bot:2245 | pricing function | `.fast_info` | Spot price display in messages |
| 13 | bot:6389 | batch fetch | `yf.download()` | Dashboard spot prices |
| 14 | bot:7866 | position discovery | `yf.download()` | /health position spot prices |
| 15 | bot:7885 | position fallback | `.fast_info` | Fallback spot for /health |
| 16 | deck:83 | `get_vix()` | `.fast_info` | Command Deck VIX badge |
| 17 | deck:113 | `get_spots()` | `yf.download()` | Command Deck cycle table spots |
| 18 | pxo:82 | `_rank_by_recent_move` | `yf.download()` | Ranking display for /scan |

#### REFERENCE (5 sites) — fundamentals, calendar, news

| # | File:Line | Function | yfinance API | Purpose |
|---|-----------|----------|-------------|---------|
| 19 | bot:5456 | ticker enrichment | `.info` | GICS sector/industry for universe |
| 20 | bot:6749 | conviction tier | `.info` | EPS/revenue/margin data for conviction |
| 21 | vrp:162 | variance calc | `yf.download()` | 1Y historical returns for VRP |
| 22 | vrp:396 | earnings date | `.calendar` | Earnings date for VRP veto |
| 23 | pxo:296 | headline | `.news` | Latest news for /scan output |

## ib_async equivalents for EXECUTION_CRITICAL sites

### Existing ib_async patterns in codebase (confirmed working)

| ib_async call | Current location | What it provides |
|---------------|-----------------|------------------|
| `ib.reqMktData(contract)` | bot:2708, 3073, 3078 | Live bid/ask/last/greeks |
| `ib.reqContractDetailsAsync(contract)` | bot:3003 | Contract qualification |
| `ib.accountSummaryAsync()` | bot:1427, 2173 | NLV, EL, margin |
| `ib.portfolio()` | bot:6155 | Current positions with mark price |
| `ib.reqPnLSingle(acct, "", conid)` | bot:7817, 8082 | Per-position unrealized P&L |
| `ib.reqPnL(acct)` | bot:8132 | Account-level day P&L |

### Missing but needed

| Need | ib_async call | Notes |
|------|---------------|-------|
| Option expirations | `ib.reqSecDefOptParams()` | Returns all valid strikes + expirations for an underlying. Already imported but not called. |
| Option chain (strikes + bids) | `ib.reqMktData()` per strike + `ib.reqContractDetailsAsync()` | More API calls than yfinance's single `.option_chain()`, but authoritative data |
| Spot price (snapshot) | `ib.reqMktData(contract, snapshot=True)` | Fast, single tick |

### Migration map

| EXECUTION_CRITICAL site | Old (yfinance) | New (ib_async) |
|------------------------|----------------|----------------|
| #1,4,5,6,7 (expirations) | `yf.Ticker().options` | `ib.reqSecDefOptParams(underlyingSymbol, ...)` |
| #2,8 (spot price) | `.fast_info` | `ib.reqMktData(contract, snapshot=True)` |
| #3,6,7,10,11 (chain walk) | `.option_chain()` | `ib.reqContractDetailsAsync()` + `ib.reqMktData()` per strike |
| #9 (CSP expirations) | `.options` | `ib.reqSecDefOptParams()` |

## Caching strategy

| Data type | Cache TTL | Rationale |
|-----------|-----------|-----------|
| Spot price (snapshot) | 30s | Prices change intraday, but options decisions don't need tick-level freshness |
| Option expirations | 5min | Expirations don't change intraday |
| Option chain (full) | 60s | Bids change with market; 60s is acceptable for staging decisions |
| VIX | 5min | Dashboard display, not execution |

## Fallback behavior

**EXECUTION_CRITICAL sites:** On IBKR data failure, **fail loudly**. Do NOT silently fall through to yfinance. The bot should:
1. Log the failure with full traceback
2. Send Telegram alert: "IBKR data unavailable for {ticker} — {command} aborted"
3. Refuse to stage the order
4. User can retry after /reconnect

**DISPLAY_ONLY sites:** Can keep yfinance as soft fallback with try/except. Show "—" on failure, never crash.

**REFERENCE sites:** Keep yfinance. These are batch/offline data (fundamentals, earnings calendar, news) that IBKR doesn't provide or provides poorly.

## Proposed migration stages

### Stage 1 (highest risk, do first): Option expirations + chain
Sites #1, 3-7, 9-11. These directly control which strikes get staged for /cc and /scan. Bad data = wrong strike = bad trade.

### Stage 2: Spot prices for execution
Sites #2, 8. Reference spot for CC strike selection and CSP screening.

### Stage 3: Display spots
Sites #12-18. Switch to ib_async where IB connection available, keep yfinance as fallback.

### Stage 4: Never migrate
Sites #19-23 (REFERENCE). yfinance is the right tool for fundamentals, earnings, and news.

## Estimated effort

| Stage | Sites | Est. lines | Risk |
|-------|-------|------------|------|
| 1 | 9 | ~200 | High (touches /cc and /scan order paths) |
| 2 | 2 | ~50 | Medium (affects strike selection) |
| 3 | 7 | ~100 | Low (display only) |
| 4 | 5 | 0 | None (keep yfinance) |

## Production DB: READ-ONLY (no changes during investigation)
