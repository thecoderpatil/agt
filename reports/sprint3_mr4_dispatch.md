# Sprint 3 MR 4 — Config hygiene: invariants runner defaults (E-M-1 only)

Per `reports/overnight_sprint_3_dispatch_20260424.md` MR 4 section.

## Scope reduction vs. original dispatch

Dispatch combined E-M-1 (invariants runner defaults) + E-M-4 (`__file__`-anchored
DB_PATH fallback elimination) into one MR. This MR ships **E-M-1 only**. E-M-4 is
punted to a follow-on MR.

## Rationale for the E-M-4 punt

- Removing the `__file__` fallback from `agt_equities/db.py:48` requires making
  `DB_PATH` lazy-resolve, which ripples into ~15 tests that either patch
  `DB_PATH` or rely on import-time resolution. The correct fix is achievable
  but benefits from a dedicated round of CI iteration.
- The preflight gate showed both NSSM services have `AGT_DB_PATH` set
  correctly, so the latent bug has no present operational manifestation. Safe
  to defer.
- Sprint 3 timebox has been spent; de-risking the E-M-4 refactor by
  sequencing it separately is cleaner than bundling.

## E-M-1 scope (shipped in this MR)

- `agt_equities/invariants/runner.py:67-77` — `AGT_LIVE_ACCOUNTS` env-var
  default derived from `agt_equities.config.ACCOUNT_TO_HOUSEHOLD.keys()` rather
  than a hardcoded literal. Prior hardcoded list `"U21971297,U22076329,U22076184,U22388499"`
  was a second source of truth that could drift from `config.HOUSEHOLD_MAP` on
  any account rename.
- Paper default left unchanged (no canonical paper map in config yet — per
  dispatch latitude).

## Expected delta

```yaml expected_delta
files:
  agt_equities/invariants/runner.py:
    added: 9
    removed: 3
    net: 6
    tolerance: 3
    required_sentinels:
      - "ACCOUNT_TO_HOUSEHOLD"
      - "E-M-1 (Sprint 3 MR 4)"
```

## Tests

72 tests pass across `test_invariants.py`, `test_invariants_heartbeat.py`,
`test_invariants_tick.py`. The default-derivation change is additive; existing
tests that set `AGT_LIVE_ACCOUNTS` explicitly still override the new default
identically.
