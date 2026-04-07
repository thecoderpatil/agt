# Phase 1 Reconciliation Report

Generated: 2026-04-07
Fixture: tests/fixtures/master_log_sample.xml (YTD: 2026-01-01 → 2026-04-03)
Database: TEST FIXTURE ONLY (production DB untouched)

## Summary
- Total active Walker cycles: **7**
- Total closed Walker cycles: **8**
- Tickers frozen (ORPHAN_EVENT): **19** — ALL due to YTD fixture missing pre-2026 CSP opens
- Parity violations (OptionEAE vs BookTrade): **0 / 157** — perfect match
- inception_carryin rows used: **0** (file intentionally empty)

## Root Cause of Frozen Tickers

All 19 frozen tickers have their first YTD event as either an `expire_worthless` or `assign_opt_leg` on 2026-01-02 (or 2026-01-16 for NFLX). These are closing legs for CSPs opened in Dec 2025, which fall outside the YTD window.

**This is the expected behavior documented in spec Update 2 and Update 3.** The production sync from `fromDate=20250901` will include the missing CSP_OPEN events, and all 19 tickers should unfreeze. If any remain frozen after the inception sync, it's a BUG requiring investigation.

## Active Cycles (7)

### Yash_Household

| Ticker | Account | Shares | Short Opts | Paper Basis | Adj Basis | Premium | Events |
|--------|---------|--------|------------|-------------|-----------|---------|--------|
| ADBE | U21971297 | 500 | 1* | $337.00 | $330.13 | $3,436.85 | 46 |
| CRM | U21971297 | 100 | -1** | $262.50 | $255.01 | $749.29 | 12 |
| QCOM | U21971297 | 300 | 0 | $158.00 | $156.91 | $325.79 | 13 |
| SLS | U21971297 | 1,900 | 0 | $4.00 | $3.67 | $633.55 | 16 |

### Vikram_Household

| Ticker | Account | Shares | Short Opts | Paper Basis | Adj Basis | Premium | Events |
|--------|---------|--------|------------|-------------|-----------|---------|--------|
| ADBE | U22388499 | 200 | 1* | $325.00 | $316.42 | $1,715.24 | 23 |
| CRM | U22388499 | 100 | -1** | $262.50 | $257.03 | $546.52 | 8 |
| QCOM | U22388499 | 100 | 0 | $167.50 | $166.09 | $140.60 | 5 |

\* open_short_options shows 1 but per-contract analysis shows 2 active 260417C contracts — the discrepancy is caused by missing pre-2026 CSP opens in the YTD fixture. Will correct with inception sync.

\*\* Negative open_short_options indicates a contract was closed/expired without the Walker seeing the open. Same YTD fixture limitation.

## Frozen Tickers (19) — ALL fixture-limited, not bugs

### Vikram_Household (8 frozen)
| Ticker | First Event | Type | Notes |
|--------|-------------|------|-------|
| AVGO | 20260102 | expire_worthless | Pre-2026 CSP expired |
| GTLB | 20260102 | expire_worthless | Pre-2026 CSP expired |
| IBKR | 20260102 | expire_worthless | Pre-2026 CSP expired |
| META | 20260102 | assign_opt_leg | Pre-2026 CSP assigned |
| MSFT | 20260102 | assign_opt_leg | Pre-2026 CSP assigned |
| NXPI | 20260102 | expire_worthless | Pre-2026 CSP expired |
| PYPL | 20260102 | assign_opt_leg | Pre-2026 CSP assigned |
| UBER | 20260102 | expire_worthless | Pre-2026 CSP expired |

### Yash_Household (11 frozen)
| Ticker | First Event | Type | Notes |
|--------|-------------|------|-------|
| AVGO | 20260102 | expire_worthless | Pre-2026 CSP expired |
| GOOGL | 20260102 | expire_worthless | Pre-2026 CSP expired |
| GTLB | 20260102 | expire_worthless | Pre-2026 CSP expired |
| IBKR | 20260102 | expire_worthless | Pre-2026 CSP expired |
| META | 20260102 | assign_opt_leg | Pre-2026 CSP assigned |
| MSFT | 20260102 | assign_opt_leg | Pre-2026 CSP assigned |
| NFLX | 20260116 | expire_worthless | Pre-2026 CSP expired (later date) |
| NXPI | 20260102 | expire_worthless | Pre-2026 CSP expired |
| PYPL | 20260102 | assign_opt_leg | Pre-2026 CSP assigned |
| UBER | 20260102 | expire_worthless | Pre-2026 CSP expired |
| VRT | 20260102 | expire_worthless | Pre-2026 CSP expired |

## Sync-Time Parity Check
- OptionEAE rows: **157**
- Matched to BookTrade in master_log_trades: **157/157 (100%)**
- Violations: **0**

## Schema Observations Applied During Phase 1

1. **activity_code nullable** (Edit A): `master_log_statement_of_funds.activity_code` relaxed from NOT NULL to nullable. IBKR sends Starting/Ending Balance summary rows with empty activityCode.

2. **bond_interest column rename** (Edit B): `broker_interest_accruals_component` renamed to `bond_interest_accruals_component` to match actual XML attribute `bondInterestAccrualsComponent`. Long/Short variants added for all accrual fields.

3. **dividend_accruals dead column** (Edit C): Column kept for forward-compat but documented as DEAD. IBKR does not emit this attribute on EquitySummaryByReportDateInBase.

## TRAW.CVR Note

The spec expects a single TRAW.CVR share in U22076329 to appear as "ungrouped stock." The YTD fixture does not contain TRAW.CVR in the Trades section (it pre-dates the window). It may appear in OpenPositions — to be verified with inception sync.

## Recommended Next Steps

1. **Yash edits the Flex Query** to cover `fromDate=20250901` (or earlier) and include all 4 accounts
2. **Re-run the fixture pull** to get full inception data
3. **Re-run this reconciliation** — expected outcome: 0 frozen tickers, all active cycles match IBKR open positions
4. **Verify TRAW.CVR** appears in the ungrouped stock list
5. **Phase 1 gate**: Yash reviews reconciliation with inception data
