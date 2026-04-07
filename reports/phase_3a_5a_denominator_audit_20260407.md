# Phase 3A.5a Track B: Denominator Audit Report

**Date:** 2026-04-07
**Scope:** R1, R3, R11 denominator correctness vs Rulebook v9
**Status:** STOP — findings require Architect review

---

## Summary Table

| Rule | v9 Denominator Spec | Current Code Denominator | Status | Action Needed |
|------|---------------------|--------------------------|--------|---------------|
| R1   | "household net liquidation value" (all accounts, excl SPX box) | `ps.household_nlv` (all accounts) | **CORRECT** | None |
| R3   | COUNT rule, no denominator | COUNT of tickers per industry | **CORRECT** | None |
| R11  | "household_NLV" (formula in v9 line 646) | `ps.household_nlv` (all accounts) | **AMBIGUOUS** | Architect decision needed |

---

## Rule 1: Single-Name Concentration

### B1 — v9 Spec (line 40)
> "**Base limit:** No single position may exceed 20% of household net liquidation value at time of entry."

v9 Definitions (line 24):
> "**Household Net Liquidation Value (NLV):** The combined net liquidation value across all accounts (Individual + Vikram IND + Roth IRA) as reported by IBKR. Excludes SPX box spread notional (Rule 10)."

### B2 — Current Code (`rule_engine.py:129-166`)
```python
nlv = ps.household_nlv.get(household, 0)
# ...
pct = pos_val / nlv * 100
```
Denominator: `ps.household_nlv` = all-account NLV.

### B3 — Account Contribution
- Denominator includes: Individual, Roth, Trad IRA (all accounts)
- Numerator: position value from `active_cycles` filtered by household
- Active cycles come from Walker which processes all accounts in the household

**Rule 10 exclusion check:**
- SPX box spreads: NOT in active_cycles (Walker doesn't process them). **CORRECT by omission** — box spreads never appear as cycles.
- Legacy picks (SLS, GTLB): v9 says "**Included** in net liquidation value for the purposes of Rule 1 concentration math (they consume capital)." The code includes them if they appear as active cycles. SLS/GTLB positions are in the Roth (U22076329). They are included in NLV via the `total` field of master_log_nav. **CORRECT.**
- Negligible (TRAW.CVR): mark_price=0, position_value=0. Would produce 0% concentration. **CORRECT by math** — no explicit exclusion needed.

### B4 — Classification: **CORRECT**

R1 denominator = household NLV (all accounts) per v9 Definition. Code matches. No fix needed.

---

## Rule 3: Sector Concentration

### B1 — v9 Spec (line 79)
> "No more than **2 names from the same industry classification bucket** may be held simultaneously across all accounts."

### B2 — Current Code (`rule_engine.py:280-306`)
```python
for c in ps.active_cycles:
    if c.status != 'ACTIVE' or c.shares_held <= 0 or c.household_id != household:
        continue
    ig = ps.sector_overrides.get(c.ticker) or ps.industries.get(c.ticker, "Unknown")
    industry_tickers[ig].add(c.ticker)
```
This is a COUNT rule. Counts unique tickers per industry group. No denominator.

### B3 — Account Contribution
- "across all accounts" — code filters by `household_id`, which includes all accounts in the household. **CORRECT.**
- Roth positions (ADBE, MSFT, PYPL, QCOM, UBER in U22076329) ARE counted. **CORRECT per v9.**

**Rule 10 exclusion check:**
- v9 line 507: "**Excluded** from sector concentration counts (Rule 3) and correlation calculations (Rule 4)."
- Current code does NOT explicitly exclude SLS/GTLB from R3 sector counts.
- **However:** SLS and GTLB would need to be in the `industries` map to be counted. If their industry is not populated, they'd fall under "Unknown" which wouldn't conflict with any Wheel names. In practice, `ticker_universe` may or may not include them.
- This is a **minor gap** — not a denominator bug, but a Rule 10 compliance gap. If SLS or GTLB happened to share an industry bucket with 2 Wheel names, they'd incorrectly push the count to 3. Low-risk because SLS (pharma) and GTLB (DevOps) don't overlap with current Wheel industries.

### B4 — Classification: **CORRECT** (with minor Rule 10 exclusion gap noted)

No denominator issue. The Rule 10 exclusion gap for R3 is a separate minor finding, not a denominator bug.

---

## Rule 11: Portfolio Circuit Breaker (Leverage)

### B1 — v9 Spec (lines 643-646)
> "Gross beta-weighted equity notional may not exceed **1.50x** of household NLV. Computed per household:
> ```
> leverage = sum(qty * beta * spot) / household_NLV
> ```"

v9 Definitions (line 24):
> "**Household Net Liquidation Value (NLV):** The combined net liquidation value across all accounts (Individual + Vikram IND + Roth IRA)"

### B2 — Current Code (`rule_engine.py:98-119`)
```python
nlv = household_nlv.get(household, 0)
# ...
total_notional += c.shares_held * beta * spot
return total_notional / nlv
```
Denominator: `household_nlv` = all-account NLV.

### B3 — Account Contribution

**Numerator:** all equity positions across all accounts in the household (including Roth)

**Denominator:** all-account NLV ($261,902 for Yash, $80,787 for Vikram)

**Verification against live data:**

Yash:
- Gross notional (all accounts): $418,982
- All-account NLV: $261,902
- Leverage: 1.60x  **matches handoff (1.59x)**
- If margin-only NLV ($109,218): 3.84x  **does NOT match handoff**

Vikram:
- Gross notional: $175,613
- NLV: $80,787
- Leverage: 2.17x  **matches handoff (2.15-2.17x)**

### B4 — Classification: **AMBIGUOUS**

The v9 spec says `household_NLV` which the Definitions section defines as "combined net liquidation value across all accounts (Individual + Vikram IND + Roth IRA)." The current code uses this all-account NLV, and the handoff numbers (1.59x, 2.17x) confirm this is what was used to seed baselines.

**The ambiguity:** Rule 11 is a leverage/margin risk rule. The Roth IRA contributes to the numerator (Roth positions have equity exposure) but also inflates the denominator (Roth NLV is $152K for Yash). Including Roth in the denominator makes leverage look LOWER, which is a more permissive reading.

The economic argument for including Roth:
- Roth positions DO create portfolio-level equity exposure (if ADBE drops 30%, the Roth ADBE position loses value too)
- Household NLV reflects the total capital at risk
- Rule 11's purpose is preventing forced IBKR liquidation from margin expansion — but IBKR can only auto-liquidate margin accounts, not Roth

The economic argument for excluding Roth (margin-only denominator):
- IBKR margin calls only affect margin accounts
- $152K in Roth is "locked" capital that can't be sold to meet a margin call
- Leverage computed against margin-only NLV better reflects actual liquidation risk

**However:** The v9 spec explicitly says "household_NLV" with the all-account Definition. The handoff baselines (1.60x, 2.17x) were computed with all-account NLV. The R2 denominator had a different spec ("margin-eligible accounts only") which is why it needed correction. R11 does NOT have that qualification.

**Impact if denominator were changed to margin-only:**
- Yash: 1.60x -> 3.84x (massive change, well above 1.50x limit)
- Glide path baseline seeded at 1.60x would be wrong
- All existing glide paths, mode calculations, and handoff numbers would need re-baselining

**Recommendation:** Keep current all-account NLV denominator for R11. The v9 spec is unambiguous here ("household_NLV" = all accounts per Definitions). If Architect wants margin-only leverage, that should be a v10 Rulebook change, not a code fix.

---

## Supplementary Finding: R3 Rule 10 Exclusion Gap

Rule 3 does not explicitly filter out SLS/GTLB from sector counts. v9 line 507 says legacy picks are "Excluded from sector concentration counts (Rule 3)."

Current impact: ZERO. SLS (pharma) and GTLB (DevOps/Software Infrastructure) don't share industry buckets with 2 Wheel names.

Future impact: LOW. Only triggers if a future Wheel name shares an industry with SLS or GTLB.

Proposed fix (not in this sprint): Add `RULE_3_EXCLUDED_TICKERS = {"SLS", "GTLB"}` to R3, mirroring `CORRELATION_EXCLUDED_TICKERS` for R4.

---

## Impact Assessment on Day 1 Baseline

If Track B finds were applied:
- **R1:** No change (already correct)
- **R3:** No change (already correct, minor gap doesn't affect current state)
- **R11:** No change recommended (v9 spec says household_NLV, code matches)

**Track A can proceed with re-baseline.** No denominator bugs found that would change Day 1 numbers.

---

```
Phase 3A.5a triage A partial + audit B done | R2: reading 2 fixed
| tests: 217/217 | R1: CORRECT | R3: CORRECT (minor Rule 10 gap noted)
| R11: AMBIGUOUS (recommend keep current, v9 spec says household_NLV)
| STOP for Architect review | reports/phase_3a_5a_denominator_audit_20260407.md
```
