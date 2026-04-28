# MR !278 Ship Report — Sprint 14 P4: proof_report tz fix + lifecycle partial→filled promotion

**Date:** 2026-04-28  
**MR:** !278  
**Branch:** sprint-14-p4-lifecycle-proof-report  
**Squash SHA:** 66ebb3e9839a  
**Merge SHA:** f902ccf60c03  

## Changes

Three files shipped:

1. `agt_equities/order_lifecycle/proof_report.py` (+1 line net) — Replace `AND created_at >= ?` with `AND staged_at_utc IS NOT NULL AND staged_at_utc >= ?` in 5 SQL queries (`_count_pending_in_window`, `_terminal_count`, `_missing_audit_evidence_count`, `_engine_activity`, `_g3_g4_telemetry`). Fixes Day 1 forensic: ET-local `created_at` vs UTC `migration_iso` string comparison was misclassifying 10 Phase B orders as pre-migration.

2. `telegram_bot.py` (+20 lines) — Add `full_fill_promotion` block in `_r5_on_exec_details`: when `new_status == PARTIALLY_FILLED and fill_qty >= ordered_qty`, call `append_status(conn, order_id, FILLED, 'full_fill_promotion')`. Fixes IB paper engine omitting the final Filled callback after PartiallyFilled sequence.

3. `tests/test_sprint14_p4_lifecycle_proof_report.py` (NEW, 236 lines) — 3 tests:
   - `test_count_pending_uses_staged_at_utc_not_created_at`
   - `test_partial_fill_promotes_to_filled_at_ordered_qty`
   - `test_partially_filled_to_filled_is_valid_transition`

## CI

```
pipeline: 2486784740   6 failed / 1462 passed / 1 skipped / 8 deselected
delta vs baseline:     +0 new failures  (6 pre-existing test_news_adapters)
```

CI passes. Pre-existing baseline failures only.

## LOCAL_SYNC

```
fetch/reset:     done  HEAD=f902ccf
pip install:     no new requirements (dep-free MR)
smoke imports:   agt_scheduler OK, telegram_bot OK
deploy.ps1:      exit 0  backup=agt_desk_20260428_193737_pre_deploy.db  smoke=pass
heartbeats:      agt_bot=26s  agt_scheduler=0s
```

## Verification

```
sentinel (bridge-current):
  full_fill_promotion in telegram_bot.py:        1 occurrence ✓
  staged_at_utc IS NOT NULL in proof_report.py:  5 occurrences ✓
walker.py:  untouched (36136 bytes, no changes) ✓
```

## Stuck Orders (ids 428–437) — Post-Deploy Status

```
id  | ticker | status           | fill_qty | ordered_qty
----|--------|------------------|----------|-------------
428 | ARM    | partially_filled | 5        | 5
429 | ARM    | partially_filled | 5        | 5
430 | ARM    | partially_filled | 5        | 5
431 | EXPE   | partially_filled | 4        | 4
432 | EXPE   | partially_filled | 4        | 4
433 | EXPE   | partially_filled | 4        | 4
434 | INTC   | partially_filled | 14       | 14
435 | INTC   | partially_filled | 13       | 13
436 | INTC   | partially_filled | 13       | 13
437 | WDAY   | partially_filled | 9        | 9
```

**Assessment:** FIX 2 is forward-looking. These 10 rows require IB to send new `execDetails` callbacks; IB will not spontaneously re-send 2026-04-27 paper callbacks. Manual reconciliation still required per forensic report (UPDATE status → `filled` + `operator_interventions` kind=`direct_sql`). Requires Architect approval.

## Notes

- Day 2 proof report (2026-04-29 07:30 ET) will now correctly count Phase B orders via `staged_at_utc` — FIX 1 live.
- FIX 2 prevents any future Phase B run from producing stuck `partially_filled` orders when IB paper engine omits the final Filled callback.
- `AGT_CSP_TIMEOUT_DEFAULT` env var still unset — sweeper will default to `auto_reject` at first 10:10 ET fire tomorrow (2026-04-29). Architect to confirm desired default.
- Pre-migration orphan id=411 (AAPL SELL, 100/400 filled, pre-Phase-B) still awaiting Architect decision on reconciliation.
