# MR 4 — broker_mode + approval_policy

**Dispatched by**: Architect  
**Date**: 2026-04-20  
**Priority**: CRITICAL tier, MANDATORY CI poll  
**Branch**: mr4-broker-mode-approval-policy-20260421

## Summary

Add `broker_mode` + `engine` discriminator fields to `RunContext`. Add
`approval_policy.py` with `needs_csp_approval`, `needs_liquidate_approval`,
`needs_roll_approval`. Wire `AGT_BROKER_MODE` env var in config.py, ib_conn.py,
invariants/runner.py. Patch 11 RunContext sites in telegram_bot.py, 5 in
dev_cli.py, 1 in shadow_scan.py. Add `build_run_context()` factory. Fix
roll_scanner LiquidateResult branch. Patch 8 test files + add test_approval_policy.py.

Declared LOC range: +180 to +300 net lines.

```yaml expected_delta
files:
  agt_equities/runtime.py:
    added: 62
    removed: 0
    net: 62
    tolerance: 15
    required_symbols:
      - RunContext
      - build_run_context
  agt_equities/approval_policy.py:
    added: 54
    removed: 0
    net: 54
    tolerance: 10
    required_symbols:
      - needs_csp_approval
      - needs_liquidate_approval
      - needs_roll_approval
  agt_equities/config.py:
    added: 7
    removed: 3
    net: 4
    tolerance: 8
  agt_equities/ib_conn.py:
    added: 11
    removed: 6
    net: 5
    tolerance: 8
  agt_equities/invariants/runner.py:
    added: 2
    removed: 1
    net: 1
    tolerance: 5
  agt_equities/roll_scanner.py:
    added: 23
    removed: 11
    net: 12
    tolerance: 10
  telegram_bot.py:
    added: 32
    removed: 0
    net: 32
    tolerance: 10
  dev_cli.py:
    added: 10
    removed: 0
    net: 10
    tolerance: 5
  scripts/shadow_scan.py:
    added: 2
    removed: 0
    net: 2
    tolerance: 3
  .gitlab-ci.yml:
    added: 1
    removed: 1
    net: 0
    tolerance: 2
  tests/test_approval_policy.py:
    added: 49
    removed: 0
    net: 49
    tolerance: 10
  tests/test_allocator_writes_csp_decisions.py:
    added: 6
    removed: 0
    net: 6
    tolerance: 5
  tests/test_b5c_scan_bridge.py:
    added: 3
    removed: 0
    net: 3
    tolerance: 4
  tests/test_cc_decision_sink.py:
    added: 9
    removed: 0
    net: 9
    tolerance: 5
  tests/test_csp_allocator.py:
    added: 3
    removed: 0
    net: 3
    tolerance: 4
  tests/test_csp_allocator_shadow_mode.py:
    added: 6
    removed: 0
    net: 6
    tolerance: 5
  tests/test_csp_approval_gate.py:
    added: 6
    removed: 0
    net: 6
    tolerance: 5
  tests/test_csp_harvest.py:
    added: 3
    removed: 0
    net: 3
    tolerance: 4
  tests/test_csp_harvest_shadow_mode.py:
    added: 9
    removed: 0
    net: 9
    tolerance: 5
```
