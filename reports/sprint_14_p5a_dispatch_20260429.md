# Sprint 14 P5a — Oversight-noise hygiene (Bugs 1, 3, 4)
**Date:** 2026-04-29
**Branch:** feature/sprint-14-p5a-oversight-noise
**MR:** !280 (estimated)
**Tier:** CRITICAL

## Scope

Three self-contained patches across 5 files. No live-capital path touched.

## expected_delta

```yaml expected_delta
files:
  agt_equities/invariants/checks.py:
    added: 2
    removed: 0
    net: 2
    tolerance: 1
    required_symbols: ["check_no_unapproved_live_csp"]
    required_sentinels: ["if ctx.paper_mode:"]
  tests/test_invariants.py:
    added: 24
    removed: 6
    net: 18
    tolerance: 6
    required_symbols: ["ctx_live", "test_no_unapproved_live_csp_skips_in_paper_mode"]
    required_sentinels: ["paper_mode=False"]
  agt_equities/roll_scanner.py:
    added: 2
    removed: 2
    net: 0
    tolerance: 2
    required_sentinels: ["━━ V2 Router ━━"]
  tests/test_v2_state_router.py:
    added: 4
    removed: 4
    net: 0
    tolerance: 2
    required_sentinels: ["━━ V2 Router ━━"]
  agt_scheduler.py:
    added: 2
    removed: 22
    net: -20
    tolerance: 4
    required_sentinels: ["def _flex_sync_eod_job"]
shrinking:
  - file: agt_scheduler.py
    reason: "Delete success-path FLEX_SYNC_DIGEST enqueue block (owned by flex_sync.run_sync)"
    expected_net: -20
```
