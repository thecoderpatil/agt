# Phase 3A.5a Final Implementation Report

**Date:** 2026-04-07
**Status:** Complete
**Tests:** 235/235 (170 baseline + 65 new)
**Day 1 Mode:** PEACETIME

---

## 1. Executive Summary

Phase 3A.5a replaced 3 PENDING rule evaluator stubs (R4, R5, R6) with real evaluators, built the MarketDataProvider abstraction with working IBKRProvider, and verified the full evaluator stack against the live portfolio. The sprint surfaced and fixed a critical R2 denominator bug (Reading 1 vs Reading 2), added glide path noise tolerance, seeded 6 new glide paths, and refreshed the LLM rulebook context to v9.

Day 1 baseline: **PEACETIME.** All raw REDs softened via glide paths. All 14 glide paths loading correctly.

---

## 2. Track A: R2 Denominator Fix + Re-baseline

### R2 Fix (Reading 2)
- **Bug:** Stage 1 R2 used all-account NLV as denominator, including $152K Roth. v9 says "margin-eligible accounts only."
- **Fix:** `MARGIN_ELIGIBLE_ACCOUNTS` map in rule_engine.py. Yash=[U21971297], Vikram=[U22388499].
- **Impact:** Yash R2 ratio changed from ~17% (Reading 1) to 42.1% (Reading 2). Both readings show a breach vs 70% retain at VIX 22, but Reading 2 is the correct measurement.

### R2 Tests Updated
| Test | Old (Reading 1) | New (Reading 2) | Why |
|------|----------------|-----------------|-----|
| test_el_sufficient | household_nlv=200K | account_el margin_nlv=100K | Denominator now margin-only |
| test_el_insufficient | household_nlv=200K | account_el margin_nlv=100K | Same |
| test_reading_2_vs_reading_1_divergence | (new) | At VIX 28: GREEN under R2, would be RED under R1 | Proves the divergence |
| test_fallback_to_household_el | (new) | Falls back to account_nlv when no account_el | Backward compat |

### Glide Paths Seeded (6 new, 14 total)
| Rule | Household | Baseline | Target | By | Accel |
|------|-----------|----------|--------|----|-------|
| R2 | Yash | 0.421 | 0.70 | 2026-12-31 | thesis_deterioration |
| R2 | Vikram | 0.542 | 0.70 | 2026-12-31 | thesis_deterioration |
| R4 | Yash | 0.6915 | 0.55 | 2026-08-25 | -- |
| R4 | Vikram | 0.6915 | 0.55 | 2026-08-25 | -- |

Plus 8 existing R1 concentration + 2 existing R11 leverage = 14 total.

### Day 1 Baseline: PEACETIME

All rules post-glide-path softening:

| Rule | Yash | Vikram |
|------|------|--------|
| R1 | GREEN (5 tickers glide-pathed) | GREEN (5 tickers glide-pathed) |
| R2 | GREEN (42.1% raw RED, glide softened) | GREEN (54.2% raw RED, glide softened) |
| R3 | GREEN | GREEN |
| R4 | GREEN (ADBE-CRM 0.69 RED, glide softened) | GREEN (same) |
| R5 | GREEN (placeholder) | GREEN |
| R6 | GREEN (N/A) | GREEN (54.3%) |
| R7-R10 | PENDING | PENDING |
| R11 | GREEN (1.60x RED, glide softened) | GREEN (2.17x RED, glide softened) |

---

## 3. Track B: Denominator Audit

| Rule | Status | Finding |
|------|--------|---------|
| R1 | CORRECT | Denominator = household NLV (all accounts) per v9 Definitions |
| R3 | CORRECT | COUNT rule, no denominator. Rule 10 gap fixed (Step 2.5) |
| R11 | AMBIGUOUS, accepted | v9 says household_NLV. Handoff numbers confirm all-account. Logged for v10. |

---

## 4. ADR-001 + ADR-002

- `docs/adr/ADR-001-r2-denominator-reading.md` -- R2 Reading 2 decision
- `docs/adr/ADR-002-glide-path-tolerance-band.md` -- Symmetric tolerance band

---

## 5. Rulebook Condensed Refresh (Step 8)

- **Old:** Derived from v8 (missing Rule 11, had 7-tier R2 table with 75% max deploy)
- **New:** Derived from v9 (Rule 11 added, 5-tier R2 table with 60% cap, EL denominator note)
- **No additional v8-v9 deltas found** beyond the two known changes. Appendices/checklists were never in the condensed format (pre-existing omission, not a v9 change).
- **Bot loads:** `rulebook_llm_condensed.md` at `telegram_bot.py:144`. Sonnet/Opus calls receive it as system context. Haiku does not. Effective on next bot restart.

---

## 6. IBKRProvider Smoke Test (Step 9)

**TWS:** Reachable on 127.0.0.1:4001

| Test | Result | Data |
|------|--------|------|
| A: accountSummary("U22388499") | PASS | EL=$42,798, NLV=$78,943 |
| B: historicalBars("ADBE", 180) | PASS | 180 bars, 2025-07-21 to 2026-04-07, $367.68 to $239.66 |
| C: option_chain stub | PASS | NotImplementedError("3A.5c scope") |
| C: fundamentals stub | PASS | NotImplementedError("3A.5c scope") |
| C: earnings_date stub | PASS | NotImplementedError("3A.5c scope") |

---

## 7. State Builder Verification (Step 10)

| Field | Populated by state_builder? | Status |
|-------|---------------------------|--------|
| `ps.correlations` | YES (build_correlation_matrix) | OK |
| `ps.account_el` | YES (build_account_el_snapshot per account) | OK |
| `ps.account_nlv` | **NO** | GAP |

**Gap:** `state_builder.py` has no `build_account_nlv()` or `build_portfolio_state()` orchestrator. Callers must manually query NLV from `master_log_nav` and wire into PortfolioState. The day1_baseline.py script does this correctly. No KeyError risk (all `.get()` with defaults).

**Not blocking:** This is an architectural completeness gap, not a functional bug. R2 falls back gracefully when account_nlv is missing. Recommend adding `build_portfolio_state()` in Phase 3B when the full state pipeline is wired.

---

## 8. Test Count Delta

| Suite | Before | After | Delta |
|-------|--------|-------|-------|
| test_walker.py | 91 | 91 | 0 |
| property tests | 23 | 23 | 0 |
| test_phase3a.py | 56 | 58 | +2 (R2 Reading 2 tests) |
| test_phase3a5a.py | 0 | 63 | +63 (R4:10, R5:11, R6:16, R3-Rule10:4, provider:4, corr:3, baseline:1, tolerance:8, amber-tolerance:6) |
| **Total** | **170** | **235** | **+65** |

---

## 9. Final Mode: PEACETIME

Verified via `scripts/day1_baseline.py` against live IBKR data + Flex positions.

---

## 10. ADBE-CRM Correlation

**Live value:** 0.6915 (n=179 trading days, full confidence, IBKR daily bars)
**Status:** RED raw, GREEN after 20-week glide path softening
**Expected:** RED (both Software-Application). Confirmed.

---

## 11. Surprises, Gotchas, Anomalies

1. **4400-share typo** — Architect prompt used illustrative 4400 ADBE shares; Flex has 500. Gotcha added to handoff.
2. **R2 denominator** — Reading 1 vs Reading 2 was not caught by tier-value verification. Gotcha + ADR added.
3. **Glide path Day 0 micro-drift** — Intraday NLV movement (0.2-0.4%) triggered mode transitions. Required symmetric tolerance band (ADR-002).
4. **R6 was already partially real** — Only needed 2-tier to 4-tier refinement, not a from-scratch build.
5. **TRAW.CVR + IBKR** — Negligible holdings had to be added to correlation exclusion set to prevent data-gap AMBERs.
6. **SQLite NULL != NULL** — UNIQUE constraint on glide_paths doesn't deduplicate rows with ticker=NULL. Cleaned manually.
7. **MagicMock is_negligible** — `getattr(mock, 'is_negligible', False)` returns a truthy MagicMock. Fixed with `is True` check.

---

## 12. Architect Review Queue

Items for Architect attention on return:
- **state_builder gap:** `account_nlv` population missing. Recommend Phase 3B.
- **v10 backlog:** R11 denominator philosophy review.
- **Condensed rulebook:** Effective on next bot restart. Consider restarting bot to pick up v9 context.
- **R5 sell gate wiring:** evaluate_rule_5_sell_gate() exists but is not wired to `/exit` or any sell path. Phase 3A.5b/c.

---

## Files Created

| File | Purpose |
|------|---------|
| `agt_equities/data_provider.py` | MarketDataProvider ABC + IBKRProvider |
| `agt_equities/state_builder.py` | Upstream populator |
| `tests/fixtures/fake_provider.py` | FakeProvider + synthetic bar generators |
| `tests/test_phase3a5a.py` | 63 tests |
| `scripts/day1_baseline.py` | Live Day 1 baseline runner |
| `docs/adr/ADR-001-r2-denominator-reading.md` | R2 Reading 2 decision |
| `docs/adr/ADR-002-glide-path-tolerance-band.md` | Symmetric tolerance band |

## Files Modified

| File | Change |
|------|--------|
| `agt_equities/rule_engine.py` | PortfolioState extensions, R2 fix, R3 Rule 10, R4/R5/R6 real evaluators |
| `agt_equities/mode_engine.py` | GlidePath accelerator_clause, GLIDE_PATH_TOLERANCE, symmetric tolerance |
| `agt_equities/schema.py` | accelerator_clause migration |
| `agt_equities/seed_baselines.py` | R2 + R4 glide paths, accelerator_clause support |
| `rulebook_llm_condensed.md` | v8 -> v9 (Rule 11, R2 table) |
| `tests/test_phase3a.py` | R2 tests updated, stub test updated, glide_paths schema |
| `reports/handoffs/HANDOFF_CODER_latest.md` | 6 new gotchas, test count, hard stop update |

---

```
Phase 3A.5a triage done | R2 reading 2 fixed | R3 Rule 10 fix shipped
| Vikram R2 glide 38w | Yash R2 glide 38w | ADBE-CRM glide 20w
| Yash R2: 42.1% (expected, glided) | Day 1: PEACETIME | tests: 235/235
| ADR-001 + ADR-002 written | Condensed: v9 | Smoke: ALL PASS
| reports/phase_3a_5a_final_20260407.md
```
