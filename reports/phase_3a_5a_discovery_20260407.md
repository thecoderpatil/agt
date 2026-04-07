# Phase 3A.5a Discovery Report

**Date:** 2026-04-07
**Status:** STOP — awaiting Architect review before implementation
**Tests:** 170/170 (no changes yet)

---

## TASK 0: R2 v9 Spec Verification

### Result: MATCH

The R2 evaluator at `rule_engine.py:129-135` uses `_VIX_EL_TABLE_V9`:

```python
_VIX_EL_TABLE_V9 = [
    (0,   20, 0.80, 0.20),   # VIX <20:  retain 80%, deploy 20%
    (20,  25, 0.70, 0.30),   # VIX 20-25: retain 70%, deploy 30%
    (25,  30, 0.60, 0.40),   # VIX 25-30: retain 60%, deploy 40%
    (30,  40, 0.50, 0.50),   # VIX 30-40: retain 50%, deploy 50%
    (40, 999, 0.40, 0.60),   # VIX 40+:   retain 40%, deploy 60%
]
```

Cell-by-cell comparison against Rulebook v9.md lines 663-669:

| VIX Tier | v9 Spec Min Retain | Code Min Retain | v9 Spec Max Deploy | Code Max Deploy | Match? |
|----------|-------------------|-----------------|-------------------|-----------------|--------|
| <20      | 80%               | 0.80            | 20%               | 0.20            | YES    |
| 20-25    | 70%               | 0.70            | 30%               | 0.30            | YES    |
| 25-30    | 60%               | 0.60            | 40%               | 0.40            | YES    |
| 30-40    | 50%               | 0.50            | 50%               | 0.50            | YES    |
| 40+      | 40%               | 0.40            | 60%               | 0.60            | YES    |

**5 tiers, 60% deploy cap.** The variable is named `_VIX_EL_TABLE_V9` — it was already built against v9. No v8 remnants.

**No fix needed.** R2 is clean.

---

## Pre-Flight Findings

### Files Found
- `HANDOFF_CODER_latest.md` — at `reports/handoffs/HANDOFF_CODER_latest.md`
- `rule_engine.py` — 331 lines, R1/R2/R3/R11 real, R4/R5/R6/R7/R8/R9/R10 stubs
- `mode_engine.py` — 237 lines, GlidePath, LeverageHysteresisTracker, compute_mode
- `seed_baselines.py` — 10 glide paths (2 leverage, 8 concentration), UBER sector override
- `test_phase3a.py` — 56 tests, existing helpers: `_mock_cycle()`, `_make_ps()`, `_get_test_db()`

### Files NOT Found
- `desk_state.md` — does NOT exist at `C:\AGT_Telegram_Bridge\desk_state.md`. The `desk_state_writer.py` exists but has apparently never been run to generate the file. This is expected per handoff: "Phase 3B: desk_state.md full integration".

### Existing Contract for Real Evaluators

Real evaluators (R1, R3, R11) follow this contract:
1. **Pure function** — takes `PortfolioState` + `household: str`, returns `RuleEvaluation` or `list[RuleEvaluation]`
2. **Zero I/O** — no DB, no network, no imports outside stdlib + dataclasses
3. **RuleEvaluation return shape** — `rule_id`, `rule_name`, `household`, `ticker`, `raw_value`, `status`, `message`, `cure_math`, `detail`
4. **Status values** — `GREEN` / `AMBER` / `RED` / `PENDING`
5. **Glide path awareness** — NOT handled inside evaluators. The mode_engine applies glide paths post-evaluation via `evaluate_glide_path()`. Evaluators report raw truth; glide paths soften at the mode layer.

**Key insight: evaluators do NOT take glide_paths as a parameter.** The task spec asks for glide_path parameters in the evaluator signatures. This conflicts with the existing architecture where mode_engine handles glide path softening. Decision needed.

### R6 Stub: Already Partially Implemented

The current R6 "stub" at `rule_engine.py:266-289` is actually a **real evaluator** — it's not just returning PENDING. It:
- Returns PENDING only for non-Vikram households or missing EL data
- For Vikram with EL data: computes `el/nlv`, returns GREEN (≥20%), AMBER (10-20%), RED (<10%)

However it's simpler than what the task spec calls for:
- Missing: CRITICAL tier for <10% (spec says Rule 5 override authorized)
- Missing: 25% AMBER warning band (spec says 20-25% is approaching floor)
- The existing thresholds use 20/10 vs the spec's 25/20/10 bands

**This is a refinement, not a from-scratch build.**

---

## Architecture Decisions Needed

### Decision 1: MarketDataProvider + Pure Evaluator Conflict

The task spec asks for R4 and R6 evaluators that call `provider.get_historical_daily_bars()` and `provider.get_account_summary()`. But the existing rule_engine.py header says:

> "Pure functions. Zero DB, zero network, zero side effects."

And the existing contract passes data via `PortfolioState` — a pre-fetched immutable snapshot. Making evaluators call a provider would violate this purity guarantee.

**Options:**
- **A) Keep purity.** Add correlation data to `PortfolioState` (e.g., `correlations: dict[tuple[str,str], float]`). Provider fetches data upstream; evaluator stays pure. This is consistent with R1/R2/R3/R11.
- **B) Break purity for R4.** Let R4 call the provider. Creates two classes of evaluators: pure (R1/R2/R3/R11) and impure (R4/R6). Adds test complexity.
- **C) Hybrid.** Create a `PortfolioStateExtended` that includes correlation matrix and per-account summaries, populated by a provider-aware factory function. Evaluators stay pure.

**Recommendation: Option A or C.** Keep evaluator purity. The provider abstraction is still valuable but lives at the data-fetching layer, not the evaluation layer.

### Decision 2: R6 Scope — Account-Level vs Household-Level

R6 spec says "Vikram IND (U22388499)" — one specific account. But the current evaluator signature is `evaluate_rule_6(ps, household)` where household = "Vikram_Household". The current `PortfolioState` has `household_el` keyed by household, not account.

Since Vikram_Household only has one account (U22388499), this works today. But if another account were added, we'd need account-level EL. For now, household-level is correct.

### Decision 3: R5 Dual Nature

The task spec correctly identifies R5 as both:
1. A **sell-side gate** (evaluate before any sell action)
2. A **status grid placeholder** (always GREEN — no portfolio-level violation state)

The sell gate has a different signature than other evaluators. This is fine — it's a separate function. But it needs a clear integration story for Phase 3A.5b/c.

**R5 sell gate call sites (discovery — DO NOT wire in 3A.5a):**
- `telegram_bot.py` `/exit` command handler (Dynamic Exit)
- `telegram_bot.py` sell-related order paths
- Any future Cure Console "force sell" action

### Decision 4: Glide Path in Evaluator Signature

Task spec puts `glide_paths: list[GlidePath] | None` in R4/R6 signatures. But existing architecture handles glide paths in `mode_engine.evaluate_glide_path()`, called after all evaluators run. No existing evaluator takes glide paths.

**Recommendation:** Do NOT add glide_paths to evaluator signatures. Follow existing pattern — evaluators report raw status, mode_engine applies softening. This keeps the separation of concerns clean.

---

## Implementation Plan (pending Architect approval)

### TASK 1: Data Provider Scaffold
- Create `agt_equities/data_provider.py` with ABC + IBKRProvider + FakeProvider
- `get_provider()` singleton, `.env` flags
- Provider used **upstream** to populate PortfolioState, not inside evaluators
- FakeProvider at `tests/fixtures/fake_provider.py`

### TASK 2: R4 Correlation Evaluator
- Add `correlations: dict[tuple[str,str], float]` field to PortfolioState (or a new dataclass)
- Pure evaluator: `evaluate_rule_4(ps, household) -> list[RuleEvaluation]`
- One result per breaching/warning pair
- Skip Rule 10 excluded tickers (SPX, SLS, GTLB, negligible)
- GREEN (all ≤0.55) / AMBER (any 0.55-0.60) / RED (any >0.60)
- Correlation computation happens in provider layer, not evaluator

### TASK 3: R5 Sell Gate
- New function `evaluate_rule_5_sell_gate()` with SellException enum
- Separate `evaluate_rule_5()` status placeholder returning GREEN always
- Discovery of call sites (report only, no wiring)

### TASK 4: R6 Refinement
- Upgrade existing R6 from 2-tier to 4-tier: GREEN (≥25%), AMBER (20-25%), RED (10-20%), CRITICAL (<10%)
- Add CRITICAL status to RuleEvaluation status Literal type
- Handle provider failure → AMBER (not GREEN)

### TASK 5: Register + Kill Stubs
- R4/R5/R6 stubs replaced by real evaluators
- R7/R8/R9/R10 stubs remain
- Update HANDOFF_CODER_latest.md gotchas

### TASK 6: Tests
- ~25 new tests using FakeProvider / synthetic data
- All evaluators tested pure (no IBKR connection)

### TASK 7: Day 1 Baseline
- Cannot run live Day 1 baseline without IBKR connection + real market data
- Can verify against synthetic data matching handoff state
- Full live verification requires Phase 3B (live IBKR feed wiring)

---

## Surprises / Gotchas

1. **R6 is not really a stub** — it already has real logic, just needs tier refinement
2. **170 tests, not 91** — handoff says 170 total (91 walker + 23 property + 56 phase3a + others)
3. **Archive/test_cio_simulation.py causes a collection error** — not in `tests/`, so excluded, but noise in pytest output
4. **No existing `data_provider.py`** — greenfield file, clean
5. **PortfolioState is frozen=True** — adding fields means all existing test helpers need updating (they construct PortfolioState). Manageable but must be done carefully.
6. **CRITICAL status** — not in the current `Literal["GREEN", "AMBER", "RED", "PENDING"]` type. Adding it has mode_engine implications: what priority does CRITICAL get? Suggest treating CRITICAL as a RED variant (same mode escalation) with extra metadata.

---

## Questions for Architect

1. **Evaluator purity:** Confirm Option A (keep pure, add correlation data to PortfolioState) vs Option B (impure R4). I recommend Option A.
2. **Glide paths in evaluator signatures:** Confirm drop from signatures and follow existing mode_engine pattern. I recommend yes.
3. **CRITICAL status for R6:** Add to the Literal union, or just use RED + a `detail["severity"]` field? I recommend the detail field approach to avoid mode_engine changes.
4. **Day 1 live baseline:** Defer to Phase 3B (needs live IBKR), or attempt with synthetic data matching handoff numbers?
5. **R4 correlation computation location:** Provider layer (upstream, pre-fetched into PortfolioState) or standalone utility function called before evaluate_all()?

---

Phase 3A.5a discovery | tests: 170/170 | R2: MATCH | STOP for Architect review | reports/phase_3a_5a_discovery_20260407.md
