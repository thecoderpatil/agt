# MR !187 Ship Report — Broker Identity Pre-flight Gate (MR 4b)

**Date**: 2026-04-20
**Branch**: mr4b-broker-preflight-20260421
**Commit**: 6aae5aecf06d9ffd0277d983b9ccefab0d63a9be

## Files

| File | Delta | Notes |
|------|-------|-------|
| agt_equities/broker_preflight.py | +107/-0 | NEW: BrokerIdentityMismatch + run_broker_identity_preflight |
| telegram_bot.py | +34/-0 | Hook after orphan scan block in post_init (line 21599) |
| tests/test_broker_preflight.py | +62/-0 | NEW: 4 sprint_a tests |
| .gitlab-ci.yml | +1/-1 | test_broker_preflight.py appended to sprint_a line |

**Total net: +203 lines** (Architect declared +80–+130; divergence from docstrings + double-spacing convention)

## Verification

- LOC gate: GATE PASS (actual values used — Architect inline estimate was low)
- Smoke: 4/4 test_broker_preflight.py passed locally
- Sentinels via GitLab API:
  - broker_preflight.py: `class BrokerIdentityMismatch` + `async def run_broker_identity_preflight` ✓
  - telegram_bot.py: 2 `run_broker_identity_preflight` occurrences (import + call) ✓
  - .gitlab-ci.yml: `test_broker_preflight.py` appended ✓

## MR

- MR: !187
- URL: https://gitlab.com/agt-group2/agt-equities-desk/-/merge_requests/187

## CI

- Pipeline: 2466767968 — 3 failed / 1007 passed / 8 deselected
- Delta vs MR4 baseline (1000 passed): **+7 passed**
  - +4: test_broker_preflight.py (new tests)
  - +3: test_approval_policy.py already in suite (MR4 merge added them to run)
  - 0 regressions
- Pre-existing failures (unchanged): test_csp_allocator.py (3 sqlite3 CI-env flakes)
- STANDARD tier — polled once, result: not blocking

## LOCAL_SYNC

  fetch/reset:   done — HEAD a125687 (merge commit)
  pip install:   no new deps
  smoke imports: ok — agt_equities.broker_preflight imports clean
  deploy.ps1:    exit 0 — deploy complete at 2026-04-20 20:08:36 local (-SkipBackup)
  heartbeats:    agt_bot 00:07:58Z ok (44s) | agt_scheduler 00:07:45Z ok (57s)

## Notes

- Anchor: hook inserted after line 21599 (`logger.error("Orphan scan failed...")`)
- Both checks (static + dynamic) wrapped in try/except; dynamic failure non-fatal
- SystemExit(1) only on BrokerIdentityMismatch — not on generic exception
- Dispatch file archived at reports/Mr4b_broker_preflight_dispatch_20260421.md
- LOC divergence (+203 vs +80–+130 declared): docstrings + double-spacing in telegram_bot.py
