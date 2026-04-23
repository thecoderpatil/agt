# Sprint 3 MR 6 — csp_approval_gate polling hygiene (E-M-2)

Per `reports/overnight_sprint_3_dispatch_20260424.md` MR 6 section.

## Scope

- Replace `time.sleep(_POLL_INTERVAL_SECONDS)` with `_STOP_EVENT.wait(timeout=...)` where `_STOP_EVENT` is a module-level `threading.Event`.
- Expose `set_stop_flag()` + `clear_stop_flag()` for shutdown callers.
- On `_STOP_EVENT.is_set()`, fail-closed with log, return `[]`.
- Add 1-retry on `row is None` before fail-closing the approval. Prior behavior lost already-approved operator taps on transient DB hiccups.

## Expected delta

```yaml expected_delta
files:
  agt_equities/csp_approval_gate.py:
    added: 45
    removed: 3
    net: 42
    tolerance: 10
    required_symbols:
      - set_stop_flag
      - clear_stop_flag
    required_sentinels:
      - "_STOP_EVENT.wait(timeout=_POLL_INTERVAL_SECONDS)"
      - "1-retry on transient DB miss"
      - "threading.Event()"
```

## Tests

All 15 existing tests in `tests/test_csp_approval_gate.py` pass unchanged. The
cancellable-wait + retry paths are additive and don't change the happy-path
semantics — the existing tests exercise the poll-then-return-list flow.

## Reasoning latitude

- Windows SIGTERM registration is fragile — dispatch suggested fallback. Took the
  fallback: expose `set_stop_flag()` as a module-level function callable from a
  shutdown-hook-style coordinator (bot or scheduler). Signal-handler wiring is
  left to the caller.
- Retry interval is the full poll interval (5s), not a 500ms sub-delay — the
  approval flow's timeout budget is 30 minutes; an extra 5s on first-retry miss
  is cheap and keeps the helper simple (reuses the existing `_poll_row_status`).
