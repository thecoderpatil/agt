# Phase 2.1 Dry-Run Report

Generated: 2026-04-07
Source: Live IBKR Flex pull (token ${AGT_FLEX_TOKEN}, query 1461095)
Target: agt_desk.db (READ-ONLY — no writes performed)

## Transaction Plan

| Table | Rows Parsed | Would Insert | Would Update | Skip (null PK) |
|-------|-------------|--------------|--------------|----------------|
| master_log_trades | 1,466 | 1,466 | 0 | 0 |
| master_log_statement_of_funds | 1,229 | 1,221 | 0 | 8 |
| master_log_nav | 1,048 | 1,048 | 0 | 0 |
| master_log_mtm_perf | 630 | 619 | 0 | 11 |
| master_log_realized_unrealized_perf | 534 | 530 | 0 | 4 |
| master_log_option_eae | 438 | 438 | 0 | 0 |
| master_log_open_positions | 35 | 35 | 0 | 0 |
| master_log_transfers | 39 | 39 | 0 | 0 |
| master_log_account_info | 4 | 4 | 0 | 0 |
| master_log_change_in_nav | 4 | 4 | 0 | 0 |
| **TOTAL** | **5,427** | **5,404** | **0** | **23** |

- **Would Update: 0** — all tables are empty (first sync), so every valid row is an insert
- **Skip (null PK): 23** — summary rows without primary key values (8 SOF Starting/Ending Balance, 11 MTM summary, 4 FIFO summary)
- **Delete count: 0** — flex_sync is append-only

## Idempotency Check

Two runs 60 seconds apart using the same pulled XML:

| Metric | Run 1 | Run 2 | Match |
|--------|-------|-------|-------|
| Rows parsed | 5,427 | 5,427 | MATCH |
| Would insert | 5,404 | 5,404 | MATCH |
| Would update | 0 | 0 | MATCH |
| Per-table breakdown | identical | identical | MATCH |

**IDEMPOTENCY: PASS.** Both runs produce identical transaction plans. Parser is deterministic.

Note: Both runs show 5,404 would-insert (not 0 on run 2) because dry-run mode does not write — the DB is unchanged between runs. This confirms the parser produces the same output from the same input. Post-write idempotency (second run shows 0 inserts) will be verified in Task 2.2.

## Tables NOT touched

The dry-run confirmed that flex_sync would ONLY write to master_log_* tables. No existing tables (pending_orders, premium_ledger, ticker_universe, etc.) would be affected.

## Production DB: UNCHANGED (0 writes, dry-run mode)
## Tests: 34/34 passing
