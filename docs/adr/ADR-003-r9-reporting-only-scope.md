# ADR-003: Rule 9 Red Alert — Reporting-Only Scope in Phase 3A.5b

**Status:** Accepted
**Date:** 2026-04-07
**Phase:** 3A.5b

## Context
Rule 9 (Red Alert) is a composite meta-rule that activates when multiple individual rules are simultaneously breached. v9 specifies 4 activation conditions; condition D requires option chain math via IBKRProvider.get_option_chain() which is NotImplementedError until Phase 3A.5c.

Two architectural choices were considered:
- Option 1: Wire R9 to automatic mode transitions (R9 fires -> auto-WARTIME)
- Option 2: R9 reports status only, mode transitions remain manual

## Decision
Option 2. R9 ships as a REPORTING compositor in 3A.5b. It computes its status, persists to red_alert_state table, surfaces in desk_state.md, but does NOT trigger automatic mode transitions.

Additionally, R9 reads SOFTENED (post-glide-path) rule statuses, not raw evaluator output. A rule on an on-track glide path is by design NOT in violation.

## Rationale
1. First-time R9 computation — wiring directly to auto-WARTIME on first ship risks false-positive desk lockdowns.
2. Phase 3B delivers the full automated pipeline. R9 wiring belongs there.
3. R1/R2/R6 are independently monitored via /health and /mode. Manual escalation path is intact.
4. Condition D is deferred to 3A.5c. Wiring R9 to automation before condition D would mean auto-firing on a 67%-relative-threshold version of itself.

## Implementation
- evaluate_rule_9_composite() in rule_engine.py — post-softening compositor
- red_alert_state table — per-household hysteresis (Bucket 3)
- Thresholds: 2-of-3 fire (condition D deferred), all-3 clear (asymmetric)
- PENDING stub in evaluate_all() remains for display compatibility

## Consequences
- R9 status visible in desk_state.md grid and Cure Console
- Manual /declare_wartime remains the only WARTIME path
- Phase 3B must wire R9 to automated mode pipeline
- When condition D lands in 3A.5c, thresholds change from 2-of-3 to 2-of-4

## Related
- ADR-001: R2 Denominator Reading
- ADR-002: Glide Path Tolerance Band
- Phase 3A.5b discovery + implementation reports
