# ADR-002: Glide Path Tolerance Band (Symmetric)

**Status:** Accepted
**Date:** 2026-04-07
**Phase:** 3A.5a triage

## Context
Day 1 baseline on 2026-04-07 triggered WARTIME on sub-percent intraday NLV drift (Vikram R2 and R11). First fix applied tolerance to the WORSENED check only, which prevented RED but the BEHIND check still fired on the same noise, flipping mode to AMBER. Noise-driven mode thrashing is worse than either extreme.

## Decision
Tolerance applies symmetrically to BOTH the WORSENED (RED) and BEHIND (AMBER) checks. A sub-tolerance drift in either direction is classified ON_TRACK (GREEN). Tolerance is noise rejection, not one-sided leniency.

GLIDE_PATH_TOLERANCE per rule:
- R1: 0.01 (1pp concentration)
- R2: 0.01 (1pp EL retention)
- R4: 0.02 (2bp correlation)
- R6: 0.01 (1pp Vikram EL)
- R11: 0.02 (2bp leverage)
- Default: 0.01

## Semantic Contract
- WORSENED (RED): actual beyond baseline by MORE than tolerance
- BEHIND (AMBER): actual behind expected trajectory by MORE than tolerance
- ON_TRACK (GREEN): actual within tolerance of expected, or better
- WORSENED takes precedence over BEHIND when both fire

## Consequences
- Day 1 baseline computes PEACETIME cleanly
- Mode transitions require drift > tolerance in any direction
- Tolerance values are locked: widening requires a new ADR

## Related
- ADR-001: R2 Denominator Reading
- HANDOFF_CODER_latest.md gotcha 17
