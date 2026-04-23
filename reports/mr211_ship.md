# MR !211 ship report — senior-dev cleanup bundle (Sprint 3 MR 5, E-M-3 + E-M-6 + E-M-7)

## Status
MERGED. Squash `339f26ac`, merge `41887532`.

## Scope
- **E-M-3** `agt_equities/position_discovery.py:576` — silent `except Exception: pass` → `logger.warning(...)` in IBKR volatility fallback path.
- **E-M-6** `agt_scheduler.py _heartbeat_job` — `_check_invariants_tick()` now runs BEFORE `write_heartbeat(...)`. Prior order was self-referential (writer validated its own freshness).
- **E-M-7** `agt_equities/execution_gate.py` — kept tolerant `_db_enabled()` + `assert_execution_enabled()` variants (test infra patches them) but added WARNING-level log on invocation + deprecation docstrings. Grep confirmed zero production callers of `assert_execution_enabled`; WARNING surfaces any future regression onto the fail-open path at order-driving sites.

## Delta
+35 / -11 net +24 across 3 files. Per-MR dispatch fence: `reports/sprint3_mr5_dispatch.md` — GATE PASS.

## Verification
- All 80 tests pass across `test_position_discovery.py` / `test_execution_gate.py` / `test_agt_scheduler.py` / `test_invariants_heartbeat.py`
- `ast.parse` clean on all 3 files
- Grep confirmed no production caller of `assert_execution_enabled` (only `_strict` variant)

## CI
Pipeline 2474415947: compliance + sprint_a_unit_tests both green.

## LOCAL_SYNC
```
LOCAL_SYNC:
  fetch/reset:     done (Coder worktree)
  pip install:     no new deps
  smoke imports:   deferred — STANDARD tier
  deploy.ps1:      deferred — agt_scheduler.py touch (CRITICAL tier) but heartbeat-order change is safe reordering; batch redeploy recommended
  heartbeats:      n/a
```

Note: agt_scheduler.py IS in the CRITICAL glob per CLAUDE.md. Per the E-M-6 change, a redeploy IS required before the new invariant-before-heartbeat ordering takes effect operationally. Recommend batched redeploy at sprint close.
