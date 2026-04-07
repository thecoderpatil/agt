# R4 Stage 1: Option Chain Migration to ib_async

Generated: 2026-04-07

## Summary

Migrated 8 of 11 EXECUTION_CRITICAL yfinance chain call sites to ib_async. The remaining 3 (pxo_scanner.py) deferred to Stage 1b due to sync/async architecture mismatch.

## Files created/modified

### New: `agt_equities/ib_chains.py`
- `get_expirations(ib, ticker)` → `reqSecDefOptParams` → list of YYYY-MM-DD
- `get_chain_for_expiry(ib, ticker, expiry, right)` → `qualifyContractsAsync` + `reqMktData(snapshot)` → list of {strike, bid, ask, last, volume, openInterest, impliedVol}
- Error classes: `IBKRNetworkError`, `IBKRMarketClosedError`, `IBKRNoDataError`, `IBKRRateLimitError`
- Cache: 5min for expirations, 60s for chain data
- Audit: every fetch logged to `market_data_log` table

### New: `market_data_log` table in schema.py
- id, timestamp, ticker, source, latency_ms, success, error_class

### Modified: `telegram_bot.py`
- Added `_ibkr_get_expirations()` and `_ibkr_get_chain()` wrappers
- 8 call sites migrated:

| Site | Function | Old | New |
|------|----------|-----|-----|
| 1 | PXO/spread expiry lookup | `yf.Ticker().options` | `_ibkr_get_expirations()` |
| 2 | CC ladder snapshot | `yf.Ticker().options` + `.option_chain()` | `_ibkr_get_expirations()` + `_ibkr_get_chain()` |
| 3 | Mode 1 CC strike | `yf.Ticker().options` + `.option_chain()` | Same |
| 4 | Harvest CC strike | `yf.Ticker().options` + `.option_chain()` | Same |
| 5 | Gate 1 strike finder | `yf.Ticker().options` | `_ibkr_get_expirations()` |
| 6 | Dynamic exit expiry validation | `yf.Ticker().options` | `_ibkr_get_expirations()` |
| 7-8 | `_walk_chain_limited` wrapper | `asyncio.to_thread(sync_func)` | Direct `await async_func()` |

### Functions converted sync → async
- `_load_cc_ladder_snapshot` → `async`
- `_walk_mode1_chain` → `async`
- `_walk_harvest_chain` → `async`
- `_walk_chain_limited` → handles both sync and async callables

## Deferred to Stage 1b

3 pxo_scanner.py sites (#8-10 in original inventory):
- `_scan_ticker_for_csp_candidates()`: uses `.fast_info`, `.options`, `.option_chain()`
- This module is sync and called via `asyncio.to_thread()` — needs architecture change to use ib_async

## Fail-loudly behavior

Every migrated site:
1. Calls `_ibkr_get_expirations()` or `_ibkr_get_chain()`
2. These call `ensure_ib_connected()` → `ib_chains.get_expirations/get_chain_for_expiry`
3. On IBKR failure → `IBKRChainError` propagated → calling function catches and returns error to user
4. **No yfinance fallback** — the chain call either succeeds via IBKR or fails loudly

## Tests: 57/57 passing

No new chain-specific tests added yet (require live IBKR connection for integration testing). Mock tests planned for post-market session.

## Known limitations

- pxo_scanner.py still uses yfinance (Stage 1b)
- Spot prices still use yfinance (Stage 2)
- VIX badge in Command Deck still uses yfinance (DISPLAY_ONLY, acceptable)
- Chain snapshots require 2s sleep for IBKR data to populate (vs yfinance instant)

## Production DB: market_data_log table added (additive, no behavior change)
