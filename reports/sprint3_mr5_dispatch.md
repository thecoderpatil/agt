# Sprint 3 MR 5 — Senior-dev cleanup bundle (E-M-3 + E-M-6 + E-M-7)

Per `reports/overnight_sprint_3_dispatch_20260424.md` MR 5 section.
Source findings: `reports/opus_bug_hunt_overnight.md` E-M-3, E-M-6, E-M-7.

## Scope

- **E-M-3** `agt_equities/position_discovery.py:576-578` — replace silent
  `except Exception: pass` with `logger.warning(...)` in the IBKR fallback path.
- **E-M-6** `agt_scheduler.py:269-282` — reorder `_heartbeat_job` so
  `_check_invariants_tick()` runs BEFORE `write_heartbeat(...)`. Removes the
  self-reference (writer validated its own freshness).
- **E-M-7** `agt_equities/execution_gate.py:33-44` — keep the tolerant
  `_db_enabled()` variant but add a WARNING log on invocation + deprecation
  docstrings on both `_db_enabled` and `assert_execution_enabled`. Grep confirmed
  zero production callers of `assert_execution_enabled` (only `_strict` variant
  is used at order-driving sites). Deletion would break `tests/test_execution_gate.py:44,60`
  which patch these functions; the warn-on-call approach is safer per dispatch
  latitude ("Prefer deletion if grep confirms zero remaining production callers",
  but tests ARE production-infra callers — patching requires the symbol to exist).

## Expected delta

```yaml expected_delta
files:
  agt_equities/position_discovery.py:
    added: 8
    removed: 2
    net: 6
    tolerance: 3
    required_sentinels:
      - "ibkr_price_volatility fallback failed"
  agt_scheduler.py:
    added: 8
    removed: 8
    net: 0
    tolerance: 3
    required_sentinels:
      - "E-M-6"
      - "invariants tick BEFORE heartbeat"
  agt_equities/execution_gate.py:
    added: 19
    removed: 1
    net: 18
    tolerance: 5
    required_sentinels:
      - "DEPRECATED (E-M-7 Sprint 3 MR 5)"
      - "tolerant fail-open"
```

## Tests

Existing 80 tests across `test_position_discovery.py`, `test_execution_gate.py`,
`test_agt_scheduler.py`, `test_invariants_heartbeat.py` pass unchanged. The
semantic fixes are:
- E-M-3: logger.warning emits are exercised indirectly; no new assertions needed.
- E-M-6: heartbeat+invariants still both run; `test_invariants_heartbeat.py` still passes.
- E-M-7: tolerant function unchanged behaviorally; callers unchanged; new WARNING
  log does not affect return values.

## Reasoning latitude applied

- E-M-7 deletion path was NOT taken because `tests/test_execution_gate.py` uses
  `patch.object(execution_gate, "_db_enabled", ...)` — deletion would break the
  test infra (even though no production code calls the symbol). Warn-on-call
  approach leaves the symbol in place for test patchability while surfacing any
  future production regression in logs.
