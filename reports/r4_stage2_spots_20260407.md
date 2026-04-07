# R4 Stage 2: Spot Price Migration to ib_async

Generated: 2026-04-07

## New infrastructure

### `agt_equities/ib_chains.py` additions

- `get_spot(ib, ticker)` — single spot via `reqMktData`, cached 30s, fail-loudly
- `get_spots_batch(ib, tickers)` — batch spots, fires all `reqMktData` in parallel, waits 2.5s, graceful degradation (omits failed tickers)
- Both log to `market_data_log` table

### `telegram_bot.py` wrappers

- `_ibkr_get_spot(ticker)` — fail-loudly wrapper
- `_ibkr_get_spots_batch(tickers)` — graceful batch wrapper

## Migrated sites

### EXECUTION_CRITICAL (fail-loudly, no yfinance fallback)

| Site | Function | Old | New |
|------|----------|-----|-----|
| CC ladder reference price | `run_cc_ladder` | `_load_yf_stock_reference_price` fallback | `_ibkr_get_spot` (IBKR only, no yfinance) |

### DISPLAY_ONLY (IBKR primary, yfinance fallback)

| Site | Function | Old | New |
|------|----------|-----|-----|
| /health position discovery | `_discover_positions` | `yf.download` batch | `_ibkr_get_spots_batch` primary, yfinance fallback for missed |
| Roll watchlist | `/rollcheck` | `yf.download` batch | `_ibkr_get_spots_batch` primary, yfinance fallback for missed |

### Kept as yfinance (REFERENCE / DISPLAY_ONLY, per R4 plan)

| Site | Function | Reason |
|------|----------|--------|
| Rule 11 leverage | `_check_rule_11_leverage` | Beta source (REFERENCE) |
| Market price tool | `_fetch_market_price` | LLM display tool |
| Conviction tier | `_conviction_tier_from_yf_fundamentals` | Fundamentals (REFERENCE) |
| Universe sync | `cmd_sync_universe` | GICS enrichment (REFERENCE) |
| Command Deck spots | `agt_deck/main.py get_spots()` | Separate process, no ib_async available |
| VIX badge | `agt_deck/main.py get_vix()` | Index quote (REFERENCE) |

## Fail-loudly guarantee

EXECUTION_CRITICAL path (CC ladder reference price):
1. `_get_ib_stock_reference_price()` → IBKR reqMktData (existing)
2. If None → `_ibkr_get_spot()` → IBKR snapshot (new, from ib_chains)
3. If both fail → error returned to user ("Could not determine reference market price")
4. **No yfinance fallback at any step**

## Tests: 63/63 passing
## Production DB: market_data_log table ready for audit trail
