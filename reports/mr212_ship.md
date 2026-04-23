# MR !212 ship report — csp_approval_gate polling hygiene (Sprint 3 MR 6, E-M-2)

## Status
MERGED. Squash `954fd5e5`, merge `d8cf0ba2`.

## Scope
- Replace `time.sleep(_POLL_INTERVAL_SECONDS)` with `_STOP_EVENT.wait(timeout=...)` where `_STOP_EVENT` is a module-level `threading.Event()`. Cancellable on daemon shutdown.
- Expose public `set_stop_flag()` + `clear_stop_flag()` API for shutdown-hook callers (bot or scheduler).
- Add 1-retry on `row is None` before fail-closing. Prior: single transient DB miss during poll cost the operator their approval tap (return `[]`).

## Delta
+45 / -3 net +42. Per-MR dispatch fence: `reports/sprint3_mr6_dispatch.md` — GATE PASS.

## Verification
- All 15 tests in `tests/test_csp_approval_gate.py` pass unchanged
- `ast.parse` clean
- Cancellable-wait + retry paths are additive; happy-path semantics preserved

## CI
Pipeline 2474422713: compliance + sprint_a_unit_tests both green.

## LOCAL_SYNC
```
LOCAL_SYNC:
  fetch/reset:     done (Coder worktree)
  pip install:     no new deps
  smoke imports:   deferred — agt_equities/** module; STANDARD tier per dispatch
  deploy.ps1:      deferred — csp_approval_gate polling is live-capital path; recommend batched redeploy at sprint close
  heartbeats:      n/a
```

Note: csp_approval_gate is in `agt_equities/**` (CRITICAL glob per CLAUDE.md). Batched redeploy at sprint close along with the other hygiene MRs (3, 5, 6, 8, and 4 if it merges).
