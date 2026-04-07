# Phase 2.4: Reader Cutover Proposal

Generated: 2026-04-07
Status: **PROPOSAL ONLY — awaiting Yash review before any code edit**

## Feature Flag

```python
# telegram_bot.py, near top (after imports)
READ_FROM_MASTER_LOG = True  # Set False to rollback to legacy reads
```

Every new master_log read is wrapped in try/except. On exception, falls back to legacy read and logs loudly.

---

## Read Site Inventory + Mapping

### GROUP 1: premium_ledger → trade_repo (Walker cycles)

These are the core position/basis reads. Walker cycles replace premium_ledger entirely.

#### Site 1: `_load_premium_ledger_snapshot()` (telegram_bot.py ~line 1525)
**Used by:** CC ladder, /health, /cc, /exit, /dynamic_exit, /mode1
**Old query:**
```sql
SELECT household_id, ticker, initial_basis, total_premium_collected, shares_owned
FROM premium_ledger WHERE household_id = ? AND ticker = ?
```
**New source:** `trade_repo.get_active_cycles(household=?, ticker=?)`
**Mapping:**
| Legacy field | Walker field |
|---|---|
| `initial_basis` | `cycle.paper_basis` (IRS-adjusted) |
| `total_premium_collected` | `cycle.premium_total` |
| `shares_owned` | `cycle.shares_held` |
**Semantic difference:** `initial_basis` in legacy was raw strike. Walker `paper_basis` is IRS-adjusted (strike minus assigned-put premium). This is MORE correct. Dashboard will show slightly different basis numbers — lower by the put premium amount. **STOP CONDITION: displayed number changes.**

#### Site 2: `/ledger` command (telegram_bot.py ~line 6587)
**Old query:**
```sql
SELECT household_id, ticker, initial_basis, total_premium_collected, shares_owned
FROM premium_ledger ORDER BY household_id, ticker
```
**New source:** `trade_repo.get_active_cycles()` (all households, all tickers)
**Semantic difference:** Same as Site 1 — basis values change.

#### Sites 3-4: `/dashboard` in telegram_bot.py (~line 6171, 6192)
**Old query:** premium enrichment + fallback positions
**New source:** `trade_repo.get_active_cycles()` for positions, cycle.premium_total for premium
**Semantic difference:** Same basis change as Site 1.

#### Sites 5-6: `/dashboard` in telegram_dashboard_integration.py (~line 108, 127)
**Old query:** premium enrichment + fallback positions
**New source:** Same as Sites 3-4.
**Semantic difference:** Same.

---

### GROUP 2: cc_cycle_log → cc_decision_log (rename) + Walker

#### Site 7: `/cycles TICKER` (telegram_bot.py ~line 6443)
**Old query:**
```sql
SELECT ticker, mode, strike, expiry, bid, annualized, otm_pct, dte, walk_away_pnl,
       spot, adjusted_basis, created_at, flag
FROM cc_cycle_log WHERE ticker = ? ORDER BY created_at DESC LIMIT 20
```
**New source:** `trade_repo.get_cycles_for_ticker()` for wheel cycle history + `cc_decision_log` for CC decision audit trail
**Semantic difference:** The old `cc_cycle_log` stored CC DECISIONS (one row per /cc run). The Walker produces WHEEL CYCLES (one row per cycle lifecycle). These are different concepts. `/cycles` should show BOTH: cycle history (from Walker) AND decision history (from cc_decision_log).
**NEEDS_DESIGN:** Yash must decide how `/cycles TICKER` should display the merged view.

#### Sites 8-9: Mode 1 defensive checks (telegram_bot.py ~line 9412, 9421)
**Old query:**
```sql
SELECT DISTINCT ticker FROM cc_cycle_log WHERE mode = 'MODE_1_DEFENSIVE'
SELECT flag FROM cc_cycle_log WHERE ticker = ? AND mode = 'MODE_1_DEFENSIVE' ORDER BY created_at DESC LIMIT 3
```
**New source:** `cc_decision_log` (same schema as cc_cycle_log, renamed in Phase 5)
**Semantic difference:** None if cc_decision_log is populated by the new /cc path. During soak, cc_decision_log may have fewer rows than cc_cycle_log (only decisions made after Phase 3 cutover). **For now: keep reading from cc_cycle_log.** The rename to cc_decision_log happens in Phase 5.

---

### GROUP 3: dashboard_renderer.py — analytics tables

These support the dashboard PNG rendering. Each has a direct master_log equivalent.

#### Site 10: `_query_account_returns()` — trade_ledger (line ~69)
**Old query:**
```sql
SELECT SUM(CASE WHEN return_category='PREMIUM' THEN realized_pnl ELSE 0 END),
       SUM(CASE WHEN return_category='CAPITAL_GAIN' THEN realized_pnl ELSE 0 END)
FROM trade_ledger WHERE account_id = ? AND trade_date BETWEEN ? AND ?
```
**New source:**
```sql
SELECT
    SUM(CASE WHEN asset_category='OPT' THEN CAST(fifo_pnl_realized AS REAL) ELSE 0 END) as premium,
    SUM(CASE WHEN asset_category='STK' THEN CAST(fifo_pnl_realized AS REAL) ELSE 0 END) as capgains
FROM master_log_trades
WHERE account_id = ? AND trade_date BETWEEN ? AND ?
```
**Semantic difference:** Legacy `return_category` was manually classified during CSV import. master_log_trades uses `asset_category` (OPT vs STK) as the split. OPT realized = premium; STK realized = capital gains. This is slightly different — some OPT trades may be hedges (long puts) with negative premium, which legacy might classify differently. **Minor semantic change — displayed premium numbers may differ slightly.**

#### Site 11: `_query_account_returns()` — dividend_ledger (line ~78)
**Old query:**
```sql
SELECT SUM(amount) FROM dividend_ledger WHERE account_id = ? AND div_date BETWEEN ? AND ?
```
**New source:**
```sql
SELECT SUM(CAST(amount AS REAL)) FROM master_log_statement_of_funds
WHERE account_id = ? AND activity_code = 'DIV' AND date BETWEEN ? AND ?
```
**Semantic difference:** None — both track dividend income by account and date.

#### Sites 12-13: `_query_household_returns()` — same as 10-11 but grouped by household_id
**New source:** Same queries with `account_id IN (household_accounts)`.
**Semantic difference:** Same as Sites 10-11.

#### Site 14: `_get_nav_and_deposits()` — nav_snapshots (line ~126)
**Old query:**
```sql
SELECT nav_total, net_deposits, mwr_pct, twr_pct
FROM nav_snapshots WHERE account_id = ? ORDER BY snapshot_date DESC LIMIT 1
```
**New source:**
```sql
SELECT total as nav_total FROM master_log_nav
WHERE account_id = ? ORDER BY report_date DESC LIMIT 1
```
**Semantic difference:** master_log_nav has `total` (= nav_total) but does NOT have `net_deposits`, `mwr_pct`, or `twr_pct`. These are computed fields that nav_snapshots stored from CSV import. **NEEDS_DESIGN:** `net_deposits` comes from `master_log_statement_of_funds` (activity_code IN ('DEP','WITH')). `twr` comes from `master_log_change_in_nav.twr`. `mwr_pct` is not available from Flex — it was computed during CSV import.

#### Site 15: `_get_nav_and_deposits()` — deposit_ledger (line ~133)
**Old query:**
```sql
SELECT SUM(amount) FROM deposit_ledger WHERE account_id = ?
```
**New source:**
```sql
SELECT SUM(CAST(amount AS REAL)) FROM master_log_statement_of_funds
WHERE account_id = ? AND activity_code IN ('DEP', 'WITH')
```
**Semantic difference:** Need to verify that `activity_code` values for deposits/withdrawals match. May also be available from `master_log_change_in_nav.deposits_withdrawals`.

#### Site 16: `_get_historical_offset()` — historical_offsets (line ~151)
**Old query:**
```sql
SELECT premium_offset, capgains_offset, dividend_offset, total_offset
FROM historical_offsets WHERE account_id = ? AND period = ?
```
**New source:** **NO DIRECT EQUIVALENT.** Historical offsets are pre-IBKR Fidelity returns that were manually entered. They exist to patch up the 2025 dashboard to include Jan-Sep Fidelity returns.
**NEEDS_DESIGN:** These offsets represent Fidelity returns before the ACATS transfer. Options: (a) keep reading from historical_offsets (it stays as operational state), (b) hard-code the offsets, (c) include them in inception_config successor. **Recommendation: keep reading from historical_offsets — it's not a dropped table, it's operational reference data.**

Wait — historical_offsets IS on the Phase 5 drop list. But we're in Phase 2, not 5. For now, keep reading from it.

#### Site 17: `_get_inception_config()` — inception_config (line ~172)
**Old query:**
```sql
SELECT key, value FROM inception_config
```
**New source:** **NO DIRECT EQUIVALENT.** inception_config stores starting_capital, fidelity_remaining, fidelity_net_external — hardcoded reference values for the 2025 dashboard baseline.
**NEEDS_DESIGN:** Same as historical_offsets. Keep reading from inception_config for Phase 2. Drop in Phase 5.

#### Sites 18-19: `_get_ibkr_net_external()` — deposit_ledger (line ~182)
**Old query:**
```sql
SELECT SUM(amount) FROM deposit_ledger
WHERE description NOT LIKE '%ACATS%' AND description NOT LIKE '%Internal Transfer%' ...
```
**New source:** `master_log_statement_of_funds` with appropriate `activity_code` filtering. OR `master_log_change_in_nav.deposits_withdrawals` (which already nets this).
**Semantic difference:** The LIKE filters on description would need mapping to activity_code filters.

---

## Summary Table

| # | Site | Legacy Table | New Source | Semantic Change | Status |
|---|------|-------------|------------|-----------------|--------|
| 1 | _load_premium_ledger_snapshot | premium_ledger | trade_repo | Basis: raw strike → IRS-adjusted | **STOP: number changes** |
| 2 | /ledger | premium_ledger | trade_repo | Same as #1 | **STOP: number changes** |
| 3-4 | /dashboard (bot) | premium_ledger | trade_repo | Same as #1 | **STOP: number changes** |
| 5-6 | /dashboard (integration) | premium_ledger | trade_repo | Same as #1 | **STOP: number changes** |
| 7 | /cycles TICKER | cc_cycle_log | Walker + cc_decision_log | Different concept | **NEEDS_DESIGN** |
| 8-9 | Mode 1 defensive | cc_cycle_log | cc_cycle_log (keep) | None | Ready |
| 10 | Account returns | trade_ledger | master_log_trades | OPT/STK split vs manual category | Minor change |
| 11 | Account dividends | dividend_ledger | master_log_sof | None | Ready |
| 12-13 | Household returns | trade_ledger + dividend_ledger | master_log_trades + sof | Same as #10-11 | Minor change |
| 14 | NAV + metrics | nav_snapshots | master_log_nav + change_in_nav | Missing mwr_pct | **NEEDS_DESIGN** |
| 15 | Total deposits | deposit_ledger | master_log_sof | Activity code mapping | Ready with mapping |
| 16 | Historical offsets | historical_offsets | NONE | Pre-IBKR data | Keep legacy (Phase 5 decision) |
| 17 | Inception config | inception_config | NONE | Hardcoded reference | Keep legacy (Phase 5 decision) |
| 18-19 | Net external deposits | deposit_ledger | master_log_sof or change_in_nav | Description→activity_code | Ready with mapping |

## STOP CONDITIONS triggered:

1. **Sites 1-6 (premium_ledger → trade_repo):** `paper_basis` is IRS-adjusted, legacy `initial_basis` is raw strike. Displayed basis numbers WILL CHANGE. Yash must decide: (a) show IRS-adjusted basis (more correct), (b) add a `raw_strike_basis` field to Cycle for backward compat, or (c) label the field differently ("Strategy Basis" vs "Cost Basis").

2. **Site 7 (/cycles):** cc_cycle_log stores CC decisions, Walker stores wheel cycles. Different granularity. NEEDS_DESIGN.

3. **Site 14 (nav_snapshots):** `mwr_pct` not available from Flex. NEEDS_DESIGN.

## Sites ready for immediate cutover (no semantic change):

- Sites 8-9: keep reading cc_cycle_log (no change needed)
- Sites 11, 15: dividend and deposit reads → master_log_sof (direct mapping)
- Sites 16-17: keep reading historical_offsets and inception_config

## Proposed phasing:

**Phase 2.4a (immediate, behind flag):** Cut over Sites 10-13 (trade_ledger → master_log_trades for returns), Sites 11 (dividends → sof), Sites 15 + 18-19 (deposits → sof/change_in_nav).

**Phase 2.4b (after Yash basis decision):** Cut over Sites 1-6 (premium_ledger → trade_repo). This is the biggest change — affects /dashboard, /ledger, /health, /cc, /exit.

**Phase 2.4c (after Yash /cycles design):** Cut over Site 7.

**Deferred to Phase 5:** Sites 16-17 (historical_offsets, inception_config).

## Smoke Test Plan

Post-cutover, Yash manually runs:
1. `/reconcile` — must still show same A/B/C numbers
2. `/dashboard` — compare to pre-cutover screenshot
3. `/ledger` — compare basis/premium numbers
4. `/cycles UBER` — compare to pre-cutover output
5. `/health` — compare basis values
6. `/cc` (dry-run if no positions to trade) — verify basis used in CC decisions

## Production DB: unchanged by this proposal (report only)
