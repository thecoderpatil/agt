# Phase 3A.5b Discovery Report — Rule 9 (Red Alert)

**Date:** 2026-04-07
**Status:** STOP — awaiting Architect review before implementation
**Tests:** 235/235 (no changes)

---

## 1. Pipeline Order Today

There is NO automated evaluate -> soften -> compose -> mode pipeline in production.

**What exists:**

- `rule_engine.evaluate_all(ps, household)` — runs all raw evaluators, returns flat list of `RuleEvaluation` (`rule_engine.py:378-390`)
- `mode_engine.evaluate_glide_path(gp, actual, date)` — evaluates a single glide path against an actual value (`mode_engine.py:92-161`)
- `mode_engine.compute_mode(rule_evaluations)` — computes worst-status mode from a list of evaluations (`mode_engine.py:165-185`)
- `mode_engine.get_current_mode(conn)` — reads latest mode from `mode_history` table (`mode_engine.py:215-225`)

**How they're used in production:**

| Consumer | What it does | Softening? |
|----------|-------------|------------|
| `agt_deck/main.py:_build_cure_data()` (line 456) | Calls `evaluate_all()` for raw evals. Then iterates glide paths and calls `evaluate_glide_path()` per glide. **Softening is for display only** — the status badges in the Cure Console. Mode is read from `mode_history` table (line 527), NOT computed from evaluations. | Display only |
| `telegram_bot.py:_get_current_desk_mode()` (line 171) | Reads `mode_history` table directly. No evaluation, no softening. | None |
| `scripts/day1_baseline.py` (Phase 3A.5a) | The ONLY place that does evaluate -> soften -> compute_mode. Standalone script, not production. | Full pipeline |

**Key finding:** Mode transitions in production are MANUAL — via `/declare_wartime` and `/declare_peacetime` Telegram commands. The mode engine's `compute_mode()` function exists but is not called by any production code path.

---

## 2. Where R9 Inserts

R9 is a compositor that reads OTHER rules' statuses. It must run AFTER:
1. Raw evaluation of R1, R2, R6 (and optionally Condition D)
2. Glide path softening of those rules

Two options:

### Option A: R9 inside evaluate_all() (current stub location)
- R9 reads raw statuses from sibling evaluators
- Problem: evaluate_all() runs before softening. R9 would see raw REDs and fire immediately.
- **Does NOT work with the "R9 reads softened statuses" requirement.**

### Option B: R9 as a post-softening compositor
- R9 is called AFTER the soften step, not inside evaluate_all()
- Takes the softened evaluation list as input
- Returns a single RuleEvaluation for R9
- Pipeline becomes: evaluate_all() -> soften via glide paths -> evaluate_rule_9_compositor(softened_evals) -> compute_mode(softened_evals + r9_result)
- **This is the clean insertion point.**

The R9 PENDING stub in evaluate_all() remains for backward compatibility (returns PENDING status, no mode impact). The real R9 logic lives in a separate compositor function called after softening.

**Pipeline restructuring needed:** Minimal. No existing code needs to change. A new `evaluate_rule_9_composite()` function is added and called between softening and mode computation. The only consumers that need updating are:
1. `scripts/day1_baseline.py` — add R9 compositor call
2. `agt_deck/main.py:_build_cure_data()` — IF we want Cure Console to display R9 status (currently display-only, no mode impact)
3. Future: when automated mode computation lands (Phase 3B+)

**No restructuring of existing pipeline required.** R9 slots in cleanly as a post-softening step.

---

## 3. Mode Classification Source

v9 Condition D requires knowing whether each position is Mode 1 (below cost basis) or Mode 2 (at/above cost basis).

**Existing data:** Walker cycles expose `paper_basis` (and `adjusted_basis`). PortfolioState has `spots`. Mode can be derived: `spot >= basis → Mode 2, spot < basis → Mode 1`.

**No new PortfolioState field needed.** The R9 compositor can compute Mode 1/Mode 2 inline by comparing `ps.spots[ticker]` vs `cycle.paper_basis` for each active cycle. This is a pure computation on existing data.

However, this only answers "is the position above/below basis" — NOT "can it generate 30% annualized at a strike at/above basis." The 30% annualized check requires option chain data (see Condition D below).

---

## 4. Condition D Recommendation: DEFER to 3A.5c

v9 Condition D: "No position can generate 30% annualized at a strike at/above cost basis (all names in Mode 1)."

This requires:
1. Knowing which positions are Mode 2 (spot >= basis) — available now
2. For Mode 2 positions: checking if any CC strike meets the 30%/130% annualized framework — requires `IBKRProvider.get_option_chain()` which raises `NotImplementedError` until Phase 3A.5c

**Recommendation: Option (a) — DEFER Condition D to Phase 3A.5c.**

Rationale:
- R9 is operationally complete with conditions A/B/C for the current phase
- Condition D is the "all names in Mode 1" check, which is the MOST unlikely trigger given the current portfolio has positions above basis
- Approximating it would silently change the rule's semantics
- When 3A.5c ships `get_option_chain()`, Condition D becomes a one-line addition

**Proposed implementation:**
- R9 in 3A.5b uses conditions A, B, C only
- Fire threshold: **2-of-3** (not 2-of-4)
- Clear threshold: **all 3 must be false** (asymmetric)
- Condition D slot: returns `False` (condition not met) with a code comment and `detail["condition_d"] = "DEFERRED_3A5C"`
- When 3A.5c lands, the slot gets populated and thresholds change to 2-of-4 fire / all-4 clear

**Note on the 2-of-3 vs 2-of-4 question:** With only 3 conditions, 2-of-3 is a higher relative bar (67%) than 2-of-4 (50%). This is MORE conservative — R9 fires less easily. Acceptable because Condition D (all Mode 1) is the catastrophic scenario and its absence means we're missing the worst case, not over-triggering.

---

## 5. Hysteresis State Storage

R9 has asymmetric thresholds: 2-of-N to activate, all-N to deactivate. This means R9 must remember whether it's currently active.

**Options:**

### Option X: New `red_alert_state` table
- Single-row table: `(is_active BOOLEAN, activated_at TEXT, activation_reason TEXT, deactivated_at TEXT)`
- Clean, minimal, purpose-built

### Option Y: Use existing `mode_history` table
- R9 activation would be logged as a mode transition (PEACETIME -> ... with trigger_rule="rule_9")
- Problem: mode_history is currently manually managed via `/declare_wartime`. Mixing automated R9 with manual declarations creates ambiguity.

### Option Z: Stateless (compute fresh each time)
- The asymmetric hysteresis means R9 can't be computed fresh — once activated, it stays active until ALL conditions clear, even if fewer than 2 are still true
- **Stateless does NOT work for asymmetric hysteresis**

**Recommendation: Option X — new minimal table.** Clean separation from mode_history. The R9 evaluator reads this table to determine current state, then applies the correct threshold (2-of-N if currently inactive, all-N if currently active).

Schema:
```sql
CREATE TABLE IF NOT EXISTS red_alert_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- single-row constraint
    is_active INTEGER NOT NULL DEFAULT 0,
    activated_at TEXT,
    activation_reason TEXT,
    deactivated_at TEXT
)
```

---

## 6. Day 1 Projection

### Raw (pre-softening)

| Condition | Yash | Vikram |
|-----------|------|--------|
| A: 3+ positions > 20% | **TRUE** (ADBE 46.7%, MSFT 28.5%, PYPL 39.9%) | **TRUE** (ADBE 60.5%, MSFT 46.2%, CRM 22.9%, PYPL 45.0%, UBER 26.8%) |
| B: All-book EL < VIX retain | **TRUE** (42.2% < 70%) | **TRUE** (54.0% < 70%) |
| C: Vikram EL < 20% floor | N/A for Yash | **FALSE** (54.3% > 20%) |

Raw R9 (if it read raw): Yash has A+B (2-of-3) → **Red Alert ON**. Vikram has A+B (2-of-3) → **Red Alert ON**.

### Post-softening

| Condition | Yash (softened) | Vikram (softened) |
|-----------|----------------|------------------|
| A: 3+ positions > 20% (softened) | **FALSE** — all R1 REDs softened to GREEN via glide paths | **FALSE** — all R1 REDs softened to GREEN |
| B: All-book EL < VIX retain (softened) | **FALSE** — R2 RED softened to GREEN via 38w glide | **FALSE** — R2 RED softened to GREEN via 38w glide |
| C: Vikram EL < 20% | N/A | **FALSE** (54.3% > 20%, raw GREEN) |

Post-softening R9: Yash 0-of-3, Vikram 0-of-3 → **Red Alert OFF**.

### Day 1 Prediction: R9 = GREEN (Red Alert OFF)

This confirms the architectural decision: R9 MUST read softened statuses. If it read raw statuses, it would fire immediately and put the desk into a state that contradicts the entire glide path system.

---

## 7. Estimated Test Count Delta

New tests needed (~10-12):

- test_r9_all_conditions_false_green (baseline)
- test_r9_one_condition_true_green (below 2-of-3 threshold)
- test_r9_two_conditions_true_red_alert (A+B)
- test_r9_two_conditions_true_A_C_red_alert
- test_r9_all_three_true_red_alert
- test_r9_hysteresis_stays_active_with_one_condition (deactivation requires ALL clear)
- test_r9_hysteresis_deactivates_when_all_clear
- test_r9_reads_softened_not_raw (critical: verify glide-softened statuses used)
- test_r9_condition_d_deferred_returns_false
- test_r9_non_vikram_household_skips_condition_c

Estimated: 235 -> ~247 tests.

---

## 8. Surprises / Coupling

1. **No automated mode pipeline in production.** Mode is manually declared. compute_mode() is unused in production. R9 can't trigger mode transitions until the automated pipeline is wired (Phase 3B+).

2. **R9 is a compositor, not a standard evaluator.** It doesn't fit the `evaluate_rule_N(ps, household)` pattern because it reads OTHER rules' post-softened results. It needs a different signature: `evaluate_rule_9_composite(softened_evals, household, hysteresis_state)`.

3. **Per-household vs portfolio-wide:** R9 spec says "3+ positions exceed 20% concentration limit" without specifying per-household. Current R1 evaluates per-household. R9 should also be per-household (each household assessed independently).

4. **Condition A counts positions, not tickers.** If Yash has ADBE at 46% and Vikram has ADBE at 60%, that's 1 position per household, not 2. R9 per-household means Yash's Condition A counts only Yash's positions.

5. **The PENDING stub in evaluate_all() should remain.** It provides a slot in the status grid display. The real R9 compositor runs separately and its result can replace the PENDING stub's output in the display pipeline.

---

```
Phase 3A.5b discovery | pipeline: clean (no restructure needed)
| condition D: defer to 3A.5c | Day 1 R9 projection: OFF (GREEN)
| STOP for Architect review | reports/phase_3a_5b_discovery_20260407.md
```
