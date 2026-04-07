# Phase 2.2: First Live Sync Report

Generated: 2026-04-07
Target: `C:\AGT_Telegram_Bridge\agt_desk.db` (PRODUCTION)
Status: **SUCCESS — committed, gate criteria met**

## Pre-write state

- Backup: `agt_desk.db.phase1_baseline_20260407` (921,600 bytes, verified)
- master_log_* tables: 0 (created during this sync)
- Legacy tables: unchanged throughout

## Sync results

| Step | Result |
|------|--------|
| Table creation | 13 master_log + 3 Bucket 3 tables created |
| Flex pull | 3,864,358 bytes, 4 accounts |
| Parse | 39 sections |
| UPSERT | **5,404 rows inserted** across 10 tables |
| Transaction | **COMMITTED** (sync_id=1) |
| inception_carryin | 12 rows loaded from CSV |

## Post-write row counts

| Table | Rows | Expected | Status |
|-------|------|----------|--------|
| master_log_trades | 1,466 | 1,466 | OK |
| master_log_statement_of_funds | 1,219 | ~1,219 | OK |
| master_log_nav | 1,048 | 1,048 | OK |
| master_log_mtm_perf | 619 | ~619 | OK |
| master_log_realized_unrealized_perf | 530 | ~530 | OK |
| master_log_option_eae | 438 | 438 | OK |
| master_log_open_positions | 35 | 35 | OK |
| master_log_transfers | 35 | ~35 | OK |
| master_log_account_info | 4 | 4 | OK |
| master_log_change_in_nav | 4 | 4 | OK |
| master_log_sync | 1 | 1 | OK |
| master_log_corp_actions | 0 | 0 | OK |
| master_log_div_accruals | 0 | 0 | OK |

## Legacy table integrity

| Table | Before | After | Status |
|-------|--------|-------|--------|
| premium_ledger | 16 | 16 | UNCHANGED |
| trade_ledger | 1,359 | 1,359 | UNCHANGED |
| fill_log | 7 | 7 | UNCHANGED |
| ticker_universe | 597 | 597 | UNCHANGED |
| pending_orders | 205 | 205 | UNCHANGED |

## Live DB reconciliation

| Cross-check | Result | Notes |
|-------------|--------|-------|
| Parity | **438/438** | Zero violations |
| A (realized P&L) | **49/50** | ADBE -$109.95 (accepted residual) |
| B (cost basis) | **14/14** | All within $0.10/share |
| C (NAV recon) | **2/4** | U22388499 -$21.53, U22076329 +$109.95 (accepted) |
| Frozen | **0** | |
| Cycles | 174 wheel (14 active) + 2 satellite | |
| Tests | **34/34** | |

## Spot check

UBER U22076329: position=300, costBasisPrice=$73.99 — matches Phase 1 expectation.

## Gate criteria: MET

All gate criteria match fixture-based reconciliation. Same 3 accepted residuals (ADBE cross-account attribution, U22388499 IBKR rounding, U22076329 flow-through). No regressions.

## Diff vs fixture reconciliation

| Metric | Fixture | Live DB | Match |
|--------|---------|---------|-------|
| Frozen | 0 | 0 | YES |
| A | 48/49 | 49/50* | YES |
| B | 14/14 | 14/14 | YES |
| C | 2/4 | 2/4 | YES |
| Parity | 438/438 | 438/438 | YES |

\* Live DB checks 1 additional ticker (AMZN Vikram now has enough data to compare). Fixture had 48/49; live has 49/50. Same single ADBE residual.

## Production DB: WRITTEN (master_log_* tables only, legacy untouched)
