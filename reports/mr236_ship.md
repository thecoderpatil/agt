# MR !236 Ship Report — ADR-017 B Threshold Rule Engine

**Dispatched:** Sprint 7 Mega-MR B
**Branch:** `feature/observability-thresholds-v3` (v1/v2 closed due to
sequential ci.yml conflicts — see Notes)
**Squash:** _filled post-merge_
**Merge:** _filled post-merge_
**Tier:** CRITICAL (new production rule engine — observability decisions)

## Files

| Path | Δ | Notes |
|---|---|---|
| agt_equities/observability/thresholds.py | +245 | new |
| tests/test_observability_thresholds.py | +245 | new |
| .gitlab-ci.yml | +1/-1 | appended test file |

## Delta vs expected YAML

- thresholds.py target 150±15 → actual 245. Over due to (a) dedicated
  per-trigger helpers (`_absolute_architect_only`, `_absolute_error_budget_tier_0_1`,
  `_absolute_stale_heartbeat`, `_absolute_flex_empty_suspicious`,
  `_relative_invariant_spikes`) for testability and (b) comprehensive
  exception-fail-soft on each individual trigger (each helper returns `[]`
  on DB error rather than propagating). Accepted as reasoning latitude.
- test file target 130±15 → actual 245. Over due to (a) fully-built
  sqlite3 fixture creating all 3 tables (`incidents`, `daemon_heartbeat`,
  `cross_daemon_alerts`) and (b) an extra `test_absolute_flex_empty_suspicious_fires`
  case that validates the kind-filter (only `FLEX_SYNC_EMPTY_SUSPICIOUS`
  counts, not generic alerts).
- `required_sentinels`: `error_budget_tier` ✓, `max(5` ✓ (in floor guard
  evidence), `cold-start` ✓ (in docstring).
- `required_symbols`: `compute_threshold_flags`, `ThresholdFlag` both
  present and exported.

## CI

- pipeline status=success.
- +7 new tests passed (dispatch expected +6; extra test for flex alert kind filter).

## Verification

- Local pytest 7/7 PASSED.
- ADR-013 canonical `error_budget_tier` used (not legacy `severity_tier`).
- Cold-start discipline: `<3` prior days → relative trigger skipped; verified.
- Floor guard: median=1, today=4 → no fire (4 < max(5, 3)=5); today=6 → fires;
  verified.

## LOCAL_SYNC

Deferred to end-of-sprint bundled deploy.

## Notes

- **Branch naming.** First two branches
  (`feature/observability-thresholds` v1, v2) closed+deleted due to
  GitLab's 3-way-merge conflict on the `.gitlab-ci.yml` single-line
  pytest command. Root cause: each Sprint 7 MR appends a test filename
  to the SAME pytest command line; git sees two sides modifying the same
  hunk → conflict, even when one side's content is a strict superset.
  Fix: serialize sprint ships and fork each branch from current main
  immediately before ship. v3 (this MR) forked from post-A.2 main and
  merged cleanly. Lesson for future sprints: either serialize or split
  pytest command into multiple YAML-list entries so different MRs touch
  different lines.
