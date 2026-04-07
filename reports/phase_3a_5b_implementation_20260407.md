# Phase 3A.5b Implementation Report — Rule 9 Red Alert Compositor

**Date:** 2026-04-07
**Tests:** 255/255 (235 + 20 new)
**Day 1 Mode:** PEACETIME
**R9:** Yash OFF, Vikram OFF

---

## 1. Schema Migration — red_alert_state

DDL in `schema.py` (idempotent via `CREATE TABLE IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS red_alert_state (
    household TEXT PRIMARY KEY,
    current_state TEXT NOT NULL CHECK (current_state IN ('OFF', 'ON')),
    activated_at TEXT, activation_reason TEXT,
    conditions_met_count INTEGER NOT NULL DEFAULT 0,
    conditions_met_list TEXT,
    last_updated TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Seeded with `INSERT OR IGNORE` for both households = OFF. Bucket 3. Verified on live DB.

---

## 2. evaluate_rule_9_composite

**File:** `rule_engine.py` (after stub evaluators, before `evaluate_all`)

**Signature:** `evaluate_rule_9_composite(softened_evals, household, conn) -> RuleEvaluation`

Non-standard signature (takes softened eval list + DB connection for hysteresis). This is intentional — R9 is a compositor, not a standard evaluator.

**Condition mapping:**

| Condition | Source | Field Read | Threshold |
|-----------|--------|-----------|-----------|
| A | R1 softened evals | count of status=="RED" for household | >= 3 |
| B | R2 softened eval | status == "RED" for household | True |
| C | R6 softened eval | status == "RED" for Vikram only | True |
| D | DEFERRED | always False | 3A.5c |

**Hysteresis flow:**
1. Load current_state from `red_alert_state` table
2. If OFF: fire if conditions_count >= 2 (2-of-3)
3. If ON: clear only if conditions_count == 0 (all-3 cleared)
4. Persist if state changed

Helper functions: `_load_red_alert_state()` (returns 'OFF' on any error), `_save_red_alert_state()` (non-fatal on error, uses `datetime.now(timezone.utc)` — no utcnow deprecation).

---

## 3. Pipeline Integration

The existing evaluate_all() R9 PENDING stub **remains unchanged** for display compatibility. The real R9 runs as a separate post-softening step:

```
1. evaluate_all(ps, hh)         → raw evals (R1-R11 including R9 PENDING stub)
2. evaluate_glide_path() loop   → softened evals  
3. evaluate_rule_9_composite()  → R9 result (replaces PENDING in display)
4. compute_mode()               → overall mode
```

No restructuring of existing pipeline. R9 slots in between step 2 and 4. The day1_baseline.py script demonstrates the correct call order.

---

## 4. Test Results — 255/255

| Suite | Count | Delta |
|-------|-------|-------|
| test_walker.py | 91 | 0 |
| property tests | 23 | 0 |
| test_phase3a.py | 58 | 0 |
| test_phase3a5a.py | 63 | 0 |
| **test_rule_9.py** | **20** | **+20** |
| **Total** | **255** | **+20** |

Test breakdown (test_rule_9.py):
- Hysteresis state: 7 tests (load/save/failsafe)
- Composition logic: 12 tests (fire/clear/stays, per-household, condition D deferred)
- Day 1 baseline: 1 test

---

## 5. Day 1 R9 Baseline

**Yash_Household:**
- Condition A (3+ R1 RED): FALSE (0 R1 REDs post-softening)
- Condition B (R2 RED): FALSE (R2 softened to GREEN via 38w glide)
- Condition C: N/A for Yash
- Conditions met: 0/3
- R9 status: **GREEN (Red Alert OFF)**

**Vikram_Household:**
- Condition A: FALSE (0 R1 REDs post-softening)
- Condition B: FALSE (R2 softened to GREEN via 38w glide)
- Condition C (R6 RED): FALSE (R6 raw GREEN, 54.3% > 20%)
- Conditions met: 0/3
- R9 status: **GREEN (Red Alert OFF)**

---

## 6. red_alert_state Table Contents

| household | current_state | activated_at | conditions_met_count |
|-----------|--------------|--------------|---------------------|
| Yash_Household | OFF | NULL | 0 |
| Vikram_Household | OFF | NULL | 0 |

---

## 7. ADR-003

Created at `docs/adr/ADR-003-r9-reporting-only-scope.md`. Covers reporting-only scope, softened-status reading, condition D deferral, and Phase 3B wiring plan.

---

## 8. HANDOFF_CODER_latest.md Diff

**Removed from stubs:** R9 (was in "R8/R9 stubs")
**R8 remains** as sole PENDING stub.

**Added gotchas:**
- 1z: R9 real evaluator details
- 19: R9 reads SOFTENED statuses
- 20: R9 is REPORTING-ONLY in 3A.5b
- 21: R9 condition D deferred to 3A.5c

**Updated:** Test count 215 -> 255, added test_rule_9.py to key files.

---

## 9. Final Mode: PEACETIME

Verified via live IBKR + Flex data. All rules GREEN or PENDING post-softening. R9 OFF for both households.

---

## 10. Surprises, Gotchas, Anomalies

1. **No automated mode pipeline in production** — discovered during pre-flight. Mode is manually declared via Telegram. compute_mode() exists but unused in production. R9 can compute but can't trigger transitions until Phase 3B.

2. **Module docstring updated** — rule_engine.py header now notes R9 compositor is the exception to the "zero DB" purity contract.

3. **Top-level imports added** — json, sqlite3, datetime/timezone added to rule_engine.py top-level for R9. All other evaluators remain pure.

4. **IBKR fractional shares (5 IBKR shares in Vikram)** — these don't affect R9 because R1 evaluates them as 0.4% concentration (well below 20%), so they never contribute to Condition A.

---

## Files Created

| File | Purpose |
|------|---------|
| `tests/test_rule_9.py` | 20 R9 tests |
| `docs/adr/ADR-003-r9-reporting-only-scope.md` | R9 scope ADR |

## Files Modified

| File | Change |
|------|--------|
| `agt_equities/rule_engine.py` | R9 compositor + helpers, top-level imports, docstring |
| `agt_equities/schema.py` | red_alert_state table DDL + seed |
| `reports/handoffs/HANDOFF_CODER_latest.md` | R9 gotchas, test count, stubs list |

---

```
Phase 3A.5b done | tests: 255/255 | R9: shipped (3-condition, condition D
deferred) | Day 1 R9: Yash OFF, Vik OFF | red_alert_state: seeded
| ADR-003 written | Day 1 mode: PEACETIME | STOP
| reports/phase_3a_5b_implementation_20260407.md
```
