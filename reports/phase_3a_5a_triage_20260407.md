# Phase 3A.5a Triage Report — HALTED AT STEP 5

**Date:** 2026-04-07
**Status:** HARD STOP — Day 1 baseline WARTIME (glide path micro-drift)
**Tests:** 221/221

---

## Executive Summary

Sprint halted at Step 4 per hard stop protocol. Yash R2 under Reading 2 (margin-eligible NLV denominator) computes 42.1% EL retention — **well below the 50% threshold for Case CRITICAL.** This is a "stop everything and have a real conversation" condition per the Architect's explicit instructions.

Steps 2.5 (R3 Rule 10 fix) and 3 (glide path seeding) completed successfully. Steps 5-11 are blocked until Architect reviews this finding.

---

## Completed Steps

### Step 2.5 — R3 Rule 10 Fix (DONE)

Added `RULE_10_EXCLUDED_FROM_SECTOR` frozenset to `rule_engine.py`:
- SLS, GTLB (legacy personal picks)
- SPX (box spread financing)
- TRAW.CVR (negligible/contingent value right)

Plus `is_negligible` attribute check (with MagicMock-safe `is True` guard).

**Tests added:** 4 (test_phase3a5a.py::TestRule3Rule10Exclusions)
- test_rule_3_excludes_legacy_picks_from_sector_count
- test_rule_3_excludes_negligible_holdings
- test_rule_3_excludes_spx_box_spreads
- test_rule_3_legacy_picks_dont_save_real_breach

**Test count:** 217 -> 221/221

### Step 3 — Glide Path Seeding (DONE)

Two new glide paths seeded in `seed_baselines.py` and written to live DB:

**Vikram R2 (EL Retention):**
- rule_id: rule_2, household: Vikram_Household
- baseline: 0.542, target: 0.70, timeline: 12 weeks (by 2026-06-30)
- Linked to Vikram R11 leverage glide (same 12w timeline)
- Reason: Silent breach surfaced by denominator fix

**ADBE-CRM R4 (Pairwise Correlation):**
- rule_id: rule_4, household: Yash_Household + Vikram_Household
- baseline: 0.6915, target: 0.55, timeline: 20 weeks (by 2026-08-25)
- Linked to ADBE concentration 20w glide
- Reason: Both Software-Application, correlation cures as concentration cures

**Full glide path inventory after seeding: 13 entries** (8 R1 + 2 R11 + 1 R2 + 2 R4). Duplicates from NULL-key INSERT cleaned.

---

## Step 4 — HARD STOP: Yash R2 Case CRITICAL

### Raw Data

| Field | Value | Source |
|-------|-------|--------|
| Account | U21971297 (Individual) | MARGIN_ELIGIBLE_ACCOUNTS config |
| Margin EL | $44,267.48 | Live IBKR accountSummary |
| Margin NLV | $105,186.65 | Live IBKR accountSummary |
| Timestamp | 2026-04-07 18:57:51 UTC | IBKRProvider |
| EL/NLV ratio | 0.4208 (42.1%) | Computed |
| VIX assumption | 22.0 | Handoff context (no live feed) |
| Required retain (VIX 20-25) | 70% | _VIX_EL_TABLE_V9 |

### Classification: **CRITICAL** (ratio < 0.50)

Per Architect's Step 4 decision tree:
> "CASE CRITICAL: yash_r2_ratio < 0.50 — Material finding. STOP IMMEDIATELY."

### Diagnostic: Why Is Yash Margin EL Only 42%?

Yash Individual (U21971297) NAV breakdown from Flex 20260406:
- Stock long: $267,742
- Options long: $1,034,875 (SPX box spreads — long leg)
- Options short: -$1,162,091 (SPX box spreads — short leg)
- Cash: -$31,286 (margin debit)
- **Total NLV: $109,218** (per Flex), **$105,187** (live, intraday drop)

The Individual account has ~$268K in equity positions but also carries a significant margin debit (-$31K cash) and large option positions (SPX box spreads: ~$1M long / $1.16M short — these are margin financing instruments per Rule 10).

The EL of $44K reflects what's left after IBKR's maintenance margin requirements consume the rest. With $268K in equity and only $109K NLV, the account is heavily deployed. The 42% EL/NLV ratio means only 42% of the margin-account NLV is available as excess liquidity.

Under Reading 1 (old, all-account NLV): $44K / $261K = 17% — this was even worse but wasn't computed because Yash had no EL data in the old evaluator (returned PENDING).

Under Reading 2 (new, margin-only NLV): $44K / $105K = 42% — a real breach vs the 70% retain requirement at VIX 22.

**Key insight:** This is not a new condition. Yash Individual has been at this deployment level. The breach was invisible because:
1. R2 evaluator returned PENDING for Yash (no EL data in el_snapshots table)
2. Even if EL were available, Reading 1 would have used $261K denominator, masking severity

### Impact on Day 1 Baseline

If Step 5 were run now:
- Yash R2 would compute RED (42% < 70%)
- No glide path exists for Yash R2 (we were told to STOP before seeding one)
- Raw RED with no glide path = RED in mode output
- Overall mode = WARTIME (at least one un-softened RED)
- Day 1 PEACETIME hard stop would trigger

### Architect Decision Required

1. **Yash R2 glide path timeline:** What duration? Tied to what? The existing R11 Yash leverage glide is only 4 weeks (1.60x -> 1.50x). But R2 needs EL to go from 42% to 70% — a much larger gap. Is 4 weeks realistic, or should this be 12-20 weeks?

2. **VIX sensitivity:** At VIX <20, retain = 80% (even worse breach). At VIX 25-30, retain = 60% (still breached at 42%). At VIX 30-40, retain = 50% (still breached). Only at VIX 40+ (retain = 40%) would Yash be GREEN. The breach persists across almost all VIX tiers.

3. **Is this actually a "stop everything" finding or an expected condition?** Yash has been operating at this deployment level. The Architect may decide this is a known condition that the deployment governor is correctly flagging, and the appropriate response is a long-duration glide path with an aggressive premium-collection cure plan.

4. **Should Steps 5-11 proceed with a Yash R2 glide path?** Or does this finding change the sprint scope?

---

## Test Count

| Suite | Count |
|-------|-------|
| test_walker.py | 91 |
| property tests | 23 |
| test_phase3a.py | 58 (56 + 2 new R2 Reading 2 tests) |
| test_phase3a5a.py | 49 (45 + 4 R3 Rule 10 tests) |
| **Total** | **221/221 passing** |

---

## Files Modified This Sprint

| File | Change |
|------|--------|
| `agt_equities/rule_engine.py` | R2 denominator fix (Reading 2), R3 Rule 10 exclusions, account_nlv field |
| `agt_equities/seed_baselines.py` | Vikram R2 + ADBE-CRM R4 glide paths |
| `tests/test_phase3a.py` | R2 tests updated for Reading 2 |
| `tests/test_phase3a5a.py` | 4 R3 Rule 10 tests added |

## Files Created (Prior Sprint, Included in This Report)

| File | Purpose |
|------|---------|
| `agt_equities/data_provider.py` | MarketDataProvider ABC + IBKRProvider |
| `agt_equities/state_builder.py` | Upstream populator |
| `tests/fixtures/fake_provider.py` | FakeProvider for tests |

---

## Step 4.5 — Yash R2 Glide Path (DONE per Architect Revision)

Yash R2 glide path seeded per Architect decision:
- baseline: 0.421, target: 0.70, by 2026-12-31 (38 weeks)
- DECOUPLED from R11 4w glide
- accelerator_clause: thesis_deterioration

## Step 5 — Day 1 Re-Baseline: HARD STOP (WARTIME)

### Full Status Grid (raw, pre-softening)

| Rule | Yash_Household | Vikram_Household |
|------|---------------|-----------------|
| R1 ADBE | RED 46.7% | RED 60.5% |
| R1 MSFT | RED 28.5% | RED 46.2% |
| R1 PYPL | RED 39.9% | RED 45.0% |
| R1 UBER | GREEN 19.3% | RED 26.8% |
| R1 CRM | GREEN 7.1% | RED 22.9% |
| R1 QCOM | GREEN 14.4% | GREEN 15.6% |
| R2 | **RED 42.1%** | **RED 54.0%** |
| R3 | GREEN (all sectors <=2) | GREEN |
| R4 ADBE-CRM | RED 0.692 | RED 0.692 |
| R4 others | GREEN | GREEN |
| R5 | GREEN (placeholder) | GREEN |
| R6 | GREEN (N/A) | GREEN 54.0% |
| R11 | RED 1.60x | RED 2.17x |

### Post-Glide-Path Softening

Most REDs soften to GREEN (Day 0: actual ~= baseline, delta ~= 0). **BUT:**

| Rule | Household | Raw | After Glide | Issue |
|------|-----------|-----|------------|-------|
| R2 | Vikram | RED 0.5397 | **RED** | actual 0.5397 < baseline 0.542 |
| R11 | Vikram | RED 2.1738 | **RED** | actual 2.1738 > baseline 2.170 |

**Overall mode: WARTIME** (two un-softened REDs for Vikram)

### Root Cause: Day 0 Micro-Drift

The glide path baselines were seeded from point-in-time snapshots:
- Vikram R2 baseline 0.542 = $42,701 / $80,787 (first live reading ~18:20 UTC)
- Vikram R2 actual 0.5397 = $42,207 / $78,211 (second live reading ~19:00 UTC)
- **Drift: -0.4% due to intraday NLV movement** ($80,787 -> $78,211)

Same for R11:
- Vikram R11 baseline 2.17 (from Flex mark prices)
- Vikram R11 actual 2.1738 (live NLV changed denominator slightly)
- **Drift: +0.17%**

The `evaluate_glide_path` function treats ANY movement past baseline in the wrong direction as `worsened = True` -> RED. At Day 0 this means even sub-1% intraday noise prevents softening.

### This is NOT a code bug

The glide path math is correct. The issue is **stale baselines vs live readings**. Two possible fixes:

**Option A: Re-seed baselines to match live Day 0 readings**
- Vikram R2: 0.542 -> 0.5397
- Vikram R11: 2.17 -> 2.1738
- This IS "adjusting glide paths to force PEACETIME" — but at a noise-level delta, not a structural change

**Option B: Add a Day 0 tolerance band to evaluate_glide_path**
- If days_elapsed == 0 and abs(delta) < 2% of baseline, return GREEN
- More principled but changes the glide path engine

**Option C: Use Flex snapshot values consistently (no live NLV)**
- Both baseline and actual from same Flex date -> delta = 0 -> GREEN
- Day 1 verification becomes "Flex-consistent" not "live-consistent"

### Architect Decision Required

1. Which option (A/B/C) for resolving Day 0 micro-drift?
2. Is sub-1% intraday drift an acceptable reason to re-seed baselines?
3. Should evaluate_glide_path have a Day 0 tolerance band?

---

## Glide Path Inventory (14 entries)

| # | Rule | Household | Ticker | Baseline | Target | By | Accel |
|---|------|-----------|--------|----------|--------|----|-------|
| 1 | R1 | Yash | ADBE | 46.70 | 25.00 | 2026-08-25 | — |
| 2 | R1 | Yash | MSFT | 28.50 | 25.00 | 2026-08-25 | — |
| 3 | R1 | Yash | PYPL | 39.90 | 25.00 | 2026-08-25 | paused/earnings |
| 4 | R1 | Vikram | ADBE | 60.50 | 25.00 | 2026-08-25 | — |
| 5 | R1 | Vikram | MSFT | 46.20 | 25.00 | 2026-08-25 | — |
| 6 | R1 | Vikram | PYPL | 45.00 | 25.00 | 2026-08-25 | paused/earnings |
| 7 | R1 | Vikram | UBER | 26.80 | 25.00 | 2026-08-25 | — |
| 8 | R1 | Vikram | CRM | 22.90 | 20.00 | 2026-08-25 | — |
| 9 | R11 | Yash | (all) | 1.60 | 1.50 | 2026-05-05 | — |
| 10 | R11 | Vikram | (all) | 2.17 | 1.50 | 2026-06-30 | — |
| 11 | R2 | Yash | (all) | 0.421 | 0.70 | 2026-12-31 | thesis_deterioration |
| 12 | R2 | Vikram | (all) | 0.542 | 0.70 | 2026-12-31 | thesis_deterioration |
| 13 | R4 | Yash | (all) | 0.6915 | 0.55 | 2026-08-25 | — |
| 14 | R4 | Vikram | (all) | 0.6915 | 0.55 | 2026-08-25 | — |

---

```
Phase 3A.5a triage HALTED at Step 5 (2nd run) | Day 1: AMBER (not PEACETIME)
| Tolerance band shipped | Yash R2 38w + Vik R2 38w glides seeded | tests: 229/229
| Single AMBER: Vikram R11 2.1738 vs baseline 2.170 (intraday NLV micro-drift)
| STOP for Architect review | reports/phase_3a_5a_triage_20260407.md
```

---

## Step 5 Second Run (with tolerance band)

### Tolerance Band Applied
- `GLIDE_PATH_TOLERANCE` map added to `mode_engine.py`
- Worsened check now requires exceeding tolerance, not just any movement
- `behind` RED check uses `max(two_weeks_worth, tolerance)` threshold
- 8 tolerance tests added, 229/229 passing

### Fixes Applied Since First Run
- R4: Added TRAW.CVR and IBKR to CORRELATION_EXCLUDED_TICKERS (negligible holdings)
- Tolerance band: Vikram R2 (0.5425 vs baseline 0.542) now softens to GREEN (within 0.01 tolerance)
- Tolerance band: Vikram R11 not worsened (2.1738 vs baseline 2.170, +0.0038 within 0.02 tolerance)

### Result: AMBER (not PEACETIME)

**Single remaining AMBER:** Vikram R11 actual=2.1738, baseline=2.170, delta=+0.0038.

The tolerance band prevents RED (worsened threshold). But the `behind and abs(delta) > 0` check produces AMBER for any behind-schedule delta, no matter how small. At Day 0 with intraday NLV drift ($80,787 Flex close → $79,054 live), the leverage denominator shifts and produces this micro-delta.

**This is NOT a tolerance problem.** The tolerance correctly prevents RED. The AMBER comes from the `behind > 0` catch-all, which has no tolerance floor by design (AMBER is informational, not actionable).

### Architect Options

**Option 1: Accept AMBER mode on Day 0.**
AMBER mode allows `/cc` (exits/rolls) but blocks `/scan` (new CSP entries). Since this is Day 0 initialization and no trading is happening, AMBER is operationally equivalent to PEACETIME for the bootstrap. The first Flex EOD sync will refresh NLV and the delta will resolve.

**Option 2: Add tolerance floor to AMBER check.**
Change `behind and abs(delta) > 0` to `behind and abs(delta) > tolerance`. This would suppress AMBER from sub-tolerance noise. But it weakens the early-warning signal for real behind-schedule drift.

**Option 3: Use Flex-consistent NLV for baseline computation.**
Run the baseline with Flex NLV ($80,787) as the R11 denominator instead of live NLV ($79,054). Same-source data → delta = 0 → GREEN. The Flex-based computation already produces 2.17x which matches the baseline exactly.

**Recommendation:** Option 1. AMBER on Day 0 is operationally harmless and structurally correct. The first Flex EOD sync will close the gap.

### Full Post-Softening Status Grid

| Rule | Yash | Vikram |
|------|------|--------|
| R1 ADBE | GREEN (glide) | GREEN (glide) |
| R1 MSFT | GREEN (glide) | GREEN (glide) |
| R1 PYPL | GREEN (paused) | GREEN (paused) |
| R1 UBER | GREEN | GREEN (glide) |
| R1 CRM | GREEN | GREEN (glide) |
| R1 QCOM | GREEN | GREEN |
| R2 | GREEN (glide) | GREEN (glide) |
| R3 | GREEN | GREEN |
| R4 | GREEN (glide) | GREEN (glide) |
| R5 | GREEN | GREEN |
| R6 | GREEN (N/A) | GREEN (54.3%) |
| R7-R10 | PENDING | PENDING |
| R11 | GREEN (glide) | **AMBER** (2.1738, +0.0038 behind) |
