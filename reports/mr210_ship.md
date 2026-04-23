# MR !210 ship report — tx_immediate sweep (Sprint 3 MR 3, E-M-5)

## Status
MERGED. Squash `003a7ea2`, merge `ec01c0ec`.

## Scope
`with conn:` (DEFERRED) → `with tx_immediate(conn):` across 12 write sites in 3 files:
- `agt_equities/incidents_repo.py` — 4 sites + import
- `agt_equities/remediation.py` — 6 sites + import
- `agt_equities/author_critic.py` — 2 sites + import

## Delta
+15 / -15 net 0 across 3 files. Per-MR dispatch fence: `reports/sprint3_mr3_dispatch.md` — GATE PASS.

## Verification
- All 75 tests across `test_incidents_repo.py` / `test_remediation.py` / `test_author_critic.py` pass unchanged
- `ast.parse` clean on all 3 files
- Sentinel: zero `with conn:` remaining in modified files, `tx_immediate` count now 5/7/3 respectively

## CI
Pipeline 2474404384: compliance + sprint_a_unit_tests both green.

## LOCAL_SYNC
```
LOCAL_SYNC:
  fetch/reset:     done (Coder worktree)
  pip install:     no new deps
  smoke imports:   deferred — non-telegram_bot modules, STANDARD tier
  deploy.ps1:      deferred — non-CRITICAL-tier MR (no telegram_bot/agt_scheduler touch)
  heartbeats:      n/a
```

Batch redeploy recommended at end of sprint rather than per-MR for the hygiene-only MRs (3, 4, 5, 6, 8).
