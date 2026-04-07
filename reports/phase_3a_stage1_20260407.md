# Phase 3A Stage 1 Implementation Report — Foundation

**Date:** 2026-04-07
**Author:** Coder (Claude Code)
**Status:** COMPLETE — awaiting Yash review before Stage 2
**Tests:** 156/156 (114 existing + 42 new)
**Runtime:** 17.50s

---

## Files Created/Changed

| File | Change |
|------|--------|
| `agt_equities/rule_engine.py` | **NEW** — PortfolioState, RuleEvaluation dataclasses + 11 evaluators (4 real: R1/R2/R3/R11, 7 stubs: R4-R10) + `compute_leverage_pure()` |
| `agt_equities/mode_engine.py` | **NEW** — 3-mode state engine, LeverageHysteresisTracker, glide path evaluation, mode transition logging, DB helpers |
| `agt_equities/seed_baselines.py` | **NEW** — Idempotent seed script for glide_paths, sector_overrides, initial mode |
| `agt_equities/schema.py` | Added 4 new tables: `glide_paths`, `mode_history`, `el_snapshots`, `sector_overrides` |
| `agt_deck/desk_state_writer.py` | **NEW** — `generate_desk_state()` pure function + `write_desk_state_atomic()` with temp+rename |
| `agt_equities/flex_sync.py` | Wired desk_state.md regeneration after successful sync (non-fatal) |
| `tests/test_phase3a.py` | **NEW** — 42 unit tests |

---

## Architecture Summary

```
                    ┌─────────────────────┐
                    │   PortfolioState     │  ← frozen snapshot
                    │ (NLV, EL, cycles,   │
                    │  spots, betas, etc.) │
                    └────────┬────────────┘
                             │
                    ┌────────▼────────────┐
                    │   rule_engine.py     │  ← PURE functions, zero side effects
                    │ evaluate_rule_1..11  │
                    │ → list[RuleEvaluation]│
                    └────────┬────────────┘
                             │
              ┌──────────────▼──────────────┐
              │      mode_engine.py          │  ← stateful layer
              │ evaluate_glide_path()        │  (reads glide_paths table)
              │ compute_mode()               │  (reads/writes mode_history)
              │ LeverageHysteresisTracker    │  (in-memory hysteresis)
              └──────────────┬──────────────┘
                             │
              ┌──────────────▼──────────────┐
              │   desk_state_writer.py       │  → desk_state.md
              │   (atomic write)             │    (single source of truth)
              └─────────────────────────────┘
```

---

## Rule Engine Detail

### Real Evaluators (4)

| Rule | Function | Status | Threshold | Cure math |
|------|----------|--------|-----------|-----------|
| R1 | `evaluate_rule_1()` | RED if >20% NLV | 20% (drift 30%) | shares_to_sell |
| R2 | `evaluate_rule_2()` | RED if EL% < VIX retain | VIX v9 table | PENDING when EL unavailable |
| R3 | `evaluate_rule_3()` | RED if >2 per industry | 2 names | Reads `sector_overrides` first |
| R11 | `evaluate_rule_11()` | RED ≥1.50x, AMBER ≥1.30x | 1.50x limit | notional_to_reduce |

### Stub Evaluators (7)

| Rule | Status | Reason |
|------|--------|--------|
| R4 | PENDING | Requires 6-month price history |
| R5 | PENDING | Requires per-cycle annualized return calc |
| R6 | PENDING (real for Vikram when EL available) | EL data pending live IBKR feed |
| R7 | PENDING | Procedural rule, not compliance metric |
| R8 | PENDING | Per-cycle decision tool |
| R9 | PENDING | Meta-rule over R1-R8 |
| R10 | PENDING | Config rule, handled by Walker |

### Pure Leverage Wrapper

`compute_leverage_pure(active_cycles, spots, betas, household_nlv, household) -> float`

No hysteresis, no module state mutation. Hysteresis lives in `LeverageHysteresisTracker` class in mode_engine.py — instantiated by mode engine, not rule evaluator.

---

## Glide Path Math (Verified)

```
expected_today = baseline + (target - baseline) * min(days_elapsed / total_days, 1.0)
delta = actual - expected_today
weekly_rate = abs(target - baseline) / total_days * 7

GREEN:  delta <= 0 (on or ahead)
AMBER:  delta > 0 AND abs(delta) < weekly_rate * 2
RED:    abs(delta) >= weekly_rate * 2 OR actual worsened past baseline
PAUSED: pause_conditions.paused == true → always GREEN
```

**Day 1 verification (live DB):**
- Yash rule_11: baseline=1.60, expected=1.60, actual=1.60, delta=0.00 → **GREEN** ✓
- Vikram rule_11: baseline=2.17, expected=2.17, actual=2.17, delta=0.00 → **GREEN** ✓

---

## Tables Created (4, all Bucket 3)

| Table | Rows | Purpose |
|-------|------|---------|
| `glide_paths` | 10 | Per-rule forward-looking progress trackers |
| `mode_history` | 1 | Desk mode transitions (initial: PEACETIME) |
| `el_snapshots` | 0 | Live IBKR EL readings (pending API wire) |
| `sector_overrides` | 1 | Manual industry corrections (UBER seeded) |

---

## Baseline Seed Data (Live DB)

### Glide Paths (10 rows)

| Household | Rule | Ticker | Baseline | Target | Start | Due |
|-----------|------|--------|----------|--------|-------|-----|
| Yash | rule_11 | — | 1.60 | 1.50 | 2026-04-07 | 2026-05-05 (4wk) |
| Vikram | rule_11 | — | 2.17 | 1.50 | 2026-04-07 | 2026-06-30 (12wk) |
| Yash | rule_1 | ADBE | 46.7 | 25.0 | 2026-04-07 | 2026-08-25 (20wk) |
| Vikram | rule_1 | ADBE | 60.5 | 25.0 | 2026-04-07 | 2026-08-25 (20wk) |
| Yash | rule_1 | PYPL | 39.9 | 25.0 | 2026-04-07 | 2026-08-25 | PAUSED |
| Vikram | rule_1 | PYPL | 45.0 | 25.0 | 2026-04-07 | 2026-08-25 | PAUSED |
| Yash | rule_1 | MSFT | 28.5 | 25.0 | 2026-04-07 | 2026-08-25 |
| Vikram | rule_1 | MSFT | 46.2 | 25.0 | 2026-04-07 | 2026-08-25 |
| Vikram | rule_1 | UBER | 26.8 | 25.0 | 2026-04-07 | 2026-08-25 |
| Vikram | rule_1 | CRM | 22.9 | 20.0 | 2026-04-07 | 2026-08-25 |

### Sector Override

| Ticker | Sector | Sub-sector | Source |
|--------|--------|------------|--------|
| UBER | Consumer Cyclical | Travel Services | manual |

---

## Tests (42 new)

| Class | Tests | Area |
|-------|-------|------|
| TestComputeLeveragePure | 4 | Pure leverage wrapper |
| TestEvaluateRule1 | 3 | Concentration evaluator |
| TestEvaluateRule2 | 3 | EL deployment evaluator |
| TestEvaluateRule3 | 2 | Sector evaluator + override |
| TestEvaluateRule11 | 3 | Leverage evaluator |
| TestStubEvaluators | 2 | Stub returns + evaluate_all coverage |
| TestLeverageHysteresis | 1 | Breach/release cycle |
| TestGlidePathMath | 6 | Day 0, halfway, behind-1wk, behind-3wk, paused, ahead |
| TestComputeMode | 5 | All transitions + trigger info |
| TestModeTransitionDB | 3 | Log/read/multiple |
| TestDeskStateWriter | 3 | Generate content, atomic write, no-partial |
| TestSeeds | 7 | Glide paths, idempotent, overrides, initial mode, Day 1 GREEN |
| **Total** | **42** | |

---

## Constraints Verified

- [x] No Bucket 2 writes — all new tables are Bucket 3
- [x] No LLM calls — pure Python throughout
- [x] No yfinance in execution paths — betas passed as input, default 1.0
- [x] Walker purity preserved — rule_engine reads Walker output, never mutates
- [x] Day 1 computes GREEN — verified on live DB
- [x] Backward compatible — existing commands all still function
- [x] All SQL idempotent — IF NOT EXISTS / INSERT OR REPLACE
- [x] Try/except on flex_sync desk_state write — non-fatal

---

## Followups (NOT in Stage 1)

- **EL snapshots writer:** `el_snapshots` table created but no writer yet. Needs live IBKR `AccountValue` API feed in desk_state_writer.
- **5-min APScheduler job:** Not wired yet. Needs to be added to Deck FastAPI startup.
- **Stage 2:** Cure Console UI (route, template, top-strip mode badge)
- **Stage 3:** Telegram integration (mode commands, CSP blocker via mode engine)
- **Stage 4:** Validation (full live read-only pass)
- **CORP_ACTION property tests:** Logged as deferred followup from W3.7.

---

**STOP. Awaiting Yash review before Stage 2.**
