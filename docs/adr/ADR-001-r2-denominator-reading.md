# ADR-001: Rule 2 Denominator — Margin-Eligible NLV (Reading 2)

**Status:** Accepted
**Date:** 2026-04-07
**Phase:** 3A.5a triage

## Context
Rule 2 (Minimum Excess Liquidity by VIX) is the deployment governor. v9 lines 709-712: "IBKR Current Excess Liquidity (see Definitions), measured across margin-eligible accounts only (Individual + Vikram IND). Roth IRA net liquidation value is excluded because IRA accounts cannot deploy margin or sell naked CSPs."

Two readings were possible:
- **Reading 1:** EL excludes Roth EL (trivially true). Denominator = full household NLV.
- **Reading 2:** Both EL and NLV exclude Roth. Denominator = margin-eligible NLV only.

Phase 3A Stage 1 implemented Reading 1. The bug surfaced during Phase 3A.5a Day 1 baseline verification.

## Decision
Reading 2 is correct. The spec says "Roth IRA net liquidation value is excluded" — NLV is the denominator. Including Roth NLV dilutes the deployment ratio and authorizes more margin deployment based on capital that cannot deploy margin. That inverts the rule's intent.

## Implementation
rule_engine.py defines MARGIN_ELIGIBLE_ACCOUNTS:
- Yash_Household: [U21971297] (Individual only)
- Vikram_Household: [U22388499] (Single account)

evaluate_rule_2() sums EL and NLV across margin-eligible accounts only.

## Consequences
- Yash R2 ratio computed against $109K (Individual NLV), not $261K (full household). Ratios ~2.4x higher than Reading 1.
- Vikram R2 unaffected (single-account household).
- Stage 1 R2 tests rewritten for Reading 2 denominators.
- R11 (leverage) intentionally uses all-account NLV per v9 Definitions. R2 and R11 use different denominators by design.

## Related
- HANDOFF_CODER_latest.md gotchas 14, 15
- ADR-002: Glide Path Tolerance Band
- v10 review backlog: R11 denominator philosophy
