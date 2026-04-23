# MR !214 ship report — invariants runner defaults (Sprint 3 MR 4, E-M-1 only)

## Status
MERGED. Squash `9c114762`, merge `2733904e`.

## Scope reduction vs original dispatch

Dispatch's MR 4 combined E-M-1 (invariants defaults) + E-M-4 (`__file__` DB_PATH fallback elimination). This MR ships **E-M-1 only**. E-M-4 punted to follow-on because:
- Removing the `__file__` fallback in `agt_equities/db.py:48` requires lazy-resolve refactoring + ~15 test updates.
- Preflight confirmed both NSSM services have `AGT_DB_PATH` set (`C:\AGT_Runtime\state\agt_desk.db`), so no operational urgency.

## E-M-1 scope (shipped)

`agt_equities/invariants/runner.py:67-77`: `AGT_LIVE_ACCOUNTS` env default derives from `agt_equities.config.ACCOUNT_TO_HOUSEHOLD.keys()` instead of a hardcoded literal. Single source of truth for account → household routing.

Paper default left unchanged (no canonical paper map in config yet — scope creep; deferred per dispatch latitude).

## Delta
+9 / -3 net +6. Per-MR dispatch fence: `reports/sprint3_mr4_dispatch.md` — GATE PASS.

## Verification
- 72 tests pass across `test_invariants.py` / `test_invariants_heartbeat.py` / `test_invariants_tick.py`
- `ast.parse` clean
- Sentinel: `ACCOUNT_TO_HOUSEHOLD` import + E-M-1 marker comment present

## CI
Pipeline 2474444973: compliance + sprint_a_unit_tests both green (139s).

## LOCAL_SYNC
```
LOCAL_SYNC:
  fetch/reset:     done (Coder worktree at 2733904)
  pip install:     no new deps
  smoke imports:   deferred — STANDARD tier
  deploy.ps1:      deferred — batched redeploy at sprint close for all 6 MRs
  heartbeats:      n/a
```
