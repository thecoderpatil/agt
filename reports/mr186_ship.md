# MR !186 Ship Report — broker_mode + approval_policy (MR 4)

**Date**: 2026-04-20  
**Branch**: mr4-broker-mode-approval-policy-20260421  
**Commit**: 13d25aa8eb25537a6c09c25a11276c4aa3676ae2

## Files

| File | Delta | Notes |
|------|-------|-------|
| agt_equities/runtime.py | +62/-0 | broker_mode + engine fields, build_run_context factory |
| agt_equities/approval_policy.py | +54/-0 | NEW: needs_csp_approval, needs_liquidate_approval, needs_roll_approval |
| agt_equities/config.py | +7/-3 | AGT_BROKER_MODE with AGT_PAPER_MODE fallback |
| agt_equities/ib_conn.py | +11/-6 | _resolve_default_ports via AGT_BROKER_MODE |
| agt_equities/invariants/runner.py | +2/-1 | build_context via AGT_BROKER_MODE |
| agt_equities/roll_scanner.py | +23/-11 | LiquidateResult: paper auto-stage, live page; ctx param added |
| telegram_bot.py | +32/-0 | 11 RunContext sites: broker_mode + engine |
| dev_cli.py | +10/-0 | 5 RunContext sites: broker_mode via os.environ |
| scripts/shadow_scan.py | +2/-0 | 1 RunContext site: broker_mode="paper" hardcoded |
| .gitlab-ci.yml | +1/-1 | test_approval_policy.py appended to sprint_a line |
| tests/test_approval_policy.py | +49/-0 | NEW: 7 sprint_a tests |
| tests/test_allocator_writes_csp_decisions.py | +6/-0 | broker_mode+engine in RunContext |
| tests/test_b5c_scan_bridge.py | +3/-0 | broker_mode+engine in RunContext |
| tests/test_cc_decision_sink.py | +9/-0 | broker_mode+engine in RunContext |
| tests/test_csp_allocator.py | +3/-0 | broker_mode+engine in RunContext |
| tests/test_csp_allocator_shadow_mode.py | +6/-0 | broker_mode+engine in RunContext |
| tests/test_csp_approval_gate.py | +6/-0 | broker_mode+engine in RunContext |
| tests/test_csp_harvest.py | +3/-0 | broker_mode+engine in RunContext |
| tests/test_csp_harvest_shadow_mode.py | +9/-0 | broker_mode+engine in RunContext |

**Total net: +276 lines (declared +180 to +300 — PASS)**

## Verification

- LOC gate: GATE PASS: all expectations satisfied
- Smoke test: 7/7 tests/test_approval_policy.py passed locally
- Sentinels verified via GitLab API:
  - runtime.py: broker_mode field + build_run_context present
  - approval_policy.py: 3 def needs_* functions present
  - telegram_bot.py: 11 broker_mode= occurrences
  - .gitlab-ci.yml: test_approval_policy.py appended
  - test_approval_policy.py: 49 lines

## MR

- MR: !186
- URL: https://gitlab.com/agt-group2/agt-equities-desk/-/merge_requests/186

## CI

- Pipeline: 2466743932 — **FAILED** (3 failed, 1000 passed, 8 deselected)
- Delta vs baseline: 0 new failures
- Failing tests are pre-existing baseline failures in test_csp_allocator.py:
  - test_route_partial_when_household_cannot_fit_all (sqlite3.OperationalError — CI env, pre-existing)
  - test_route_spills_from_ira_to_margin_when_ira_full (sqlite3.OperationalError — CI env, pre-existing)
  - test_orchestrator_mutates_snapshot_between_candidates (assert 0 >= 1 — pre-existing)
- Same 3 failures confirmed in pipelines 2466666827 and 2466592474 (pre-MR4 baseline)
- 2 prior test_cc_decision_sink.py failures RESOLVED (fixed in !185)
- **Net: 0 regressions introduced**

## LOCAL_SYNC

Pending — will run after Yash approves and merge completes.

## Notes

- roll_scanner.py: _dispatch_eval_result signature extended with `ctx: "RunContext"` kwarg; call site updated at scan_and_stage_defensive_rolls
- dev_cli.py: 5 RunContext sites found (dispatch estimated 3) — all 5 patched
- dev_cli.py has CRLF line endings; normalized to LF via .replace('\r\n', '\n') before patch, output re-encoded as bytes (LF only)
- AGT_BROKER_MODE env var now authoritative; AGT_PAPER_MODE falls back silently during migration window
- Awaiting: merge yes from Yash/Architect → PUT /merge → LOCAL_SYNC (CRITICAL tier mandatory)
