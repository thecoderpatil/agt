# Sprint 3 MR 1 — Telegram asyncio.to_thread wraps

Per `reports/overnight_sprint_3_dispatch_20260424.md` MR 1 section.

## Scope

- B-1 (HIGH): wrap `circuit_breaker.run_all_checks()` in `_pre_trade_gates` + `cmd_daily` (both async, same HTTP-heavy pattern). Second site folded per reasoning-latitude ("<10 LOC additional sync blocker").
- B-2 (MED): wrap `assert_execution_enabled_strict(in_process_halted=_HALTED)` at 3 sites (`telegram_bot.py:9201, 12777, 13653`).
- B-3 (MED): `handle_csp_approval_callback` — 3 DB blocks offloaded via module-scope `_sync_db_read_one` + `_sync_db_write`. Minor scope expansion: the two write blocks originally used bare `conn.commit()` (DEFERRED tx); now use `tx_immediate` in the helper. Fits E-M-5 direction; documented here.
- B-4 (MED): `handle_approve_callback` — 3 DB phases (reject_all, claim, fetch) offloaded. Original `with tx_immediate` preserved inside helper thread.
- B-5 (MED): `handle_dex_callback` — 7 DB blocks offloaded. CAS lock (Step 6) + Step 8 retain atomicity via `SQL WHERE final_status = 'ATTESTED'` + `tx_immediate` inside the helper thread (no split awaits). Step 2 `is_ticker_locked(conn, ticker)` handled via closure-based offload since it takes a conn.

Skipped per audit: B-6/B-7 LOW findings (cold paths, masked UX).

## Design decision

Two module-level generic helpers (`_sync_db_read_one`, `_sync_db_write`) rather than 10 named ones. Rationale: every B-3/B-4/B-5 site is a single SQL statement; named helpers would add ~100 LOC of wrappers with no semantic benefit over the generic pair. Callers remain readable because the SQL itself is the intent. Dispatch latitude: "If extracting a helper... introduces a ref-counting or closure issue you can't resolve in 20 min, fall back..." — the problem didn't materialize; we shipped the full wrap without needing the punt option.

## Expected delta

```yaml expected_delta
files:
  telegram_bot.py:
    added: 160
    removed: 189
    net: -29
    tolerance: 30
    required_symbols:
      - _sync_db_read_one
      - _sync_db_write
    required_sentinels:
      - "await asyncio.to_thread(_cb_run)"
      - "_sync_db_read_one"
      - "_sync_db_write"
  tests/test_telegram_async_offload.py:
    added: 183
    removed: 0
    net: 183
    tolerance: 20
    required_symbols:
      - test_read_helper_returns_row_or_none
      - test_write_helper_returns_rowcount
      - test_to_thread_does_not_block_event_loop
    required_sentinels:
      - "pytest.mark.sprint_a"
      - "asyncio.to_thread"
  .gitlab-ci.yml:
    added: 1
    removed: 1
    net: 0
    tolerance: 2
    required_sentinels:
      - "test_telegram_async_offload.py"
shrinking:
  - file: telegram_bot.py
    reason: "Replacing verbose `with closing(...) as conn: with tx_immediate(conn):` blocks with single-call asyncio.to_thread(_sync_db_write, ...) is net-negative by design"
    expected_net: -29
```

## CI

Expected +5 passed (new tests in `test_telegram_async_offload.py`, registered in sprint_a_unit_tests).
