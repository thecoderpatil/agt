# Sprint 4 Mega-MR C — PUNTED to Sprint 5

Per `reports/overnight_sprint_4_dispatch_20260424.md` Mega-MR C punt-if-tight
criterion: "if at 2h into MR C timebox the test-update count has ballooned
past 20, write `reports/mrC_dbpath_blocked.md` with findings and punt".

## Reason for punt

**Test-update count: 25** (grep `DB_PATH\|AGT_DB_PATH\|_resolve_db_path` across
`tests/`). This exceeds the 20-test ceiling.

Context: the dispatch estimated ~15 tests; actual count is 25 because:
- Multiple test files patch `agt_equities.db.DB_PATH` as a module attribute
  (the tripwire fixture pattern).
- Several tests patch via `monkeypatch.setenv("AGT_DB_PATH", ...)` and expect
  eager resolution at import.
- Some tests construct DB_PATH from `Path(__file__).resolve().parent` for
  their own fixture data (dev_cli dryrun, first_live_sync scripts).
- `tests/test_file_anchor_overrides.py` explicitly exercises the fallback as
  a feature — converting it to "expect RuntimeError" per dispatch latitude
  would be a semantic change for that specific test.

## Test files that touch DB_PATH / AGT_DB_PATH

```
tests/conftest.py                          (autouse fixture — tripwire)
tests/run_dryrun.py                        (dev helper, not a test)
tests/run_first_live_sync.py               (dev helper, not a test)
tests/test_a4_init_db_lazy.py              (tests lazy init — relevant)
tests/test_alerts.py
tests/test_bootstrap_canonical.py          (tests canonical path)
tests/test_boot_contract.py                (tests boot contract)
tests/test_bot_heartbeat.py
tests/test_circuit_breaker_halt_vix.py
tests/test_circuit_breaker_heartbeat.py
tests/test_cmd_rem_redirect.py
tests/test_csp_allocator.py
tests/test_csp_allocator_dedup_guard.py
tests/test_csp_digest_wiring.py            (shipped this sprint in MR A)
tests/test_dump_rules_smoke.py
tests/test_excluded_sectors_hardgate.py
tests/test_file_anchor_overrides.py        (explicitly tests fallback)
tests/test_flex_sync_atomic.py
tests/test_flex_sync_watchdog.py           (shipped this sprint in MR B)
tests/test_mr110_autoresolve_sweep.py
tests/test_nav_freshness.py
tests/test_paper_auto_execute.py
tests/test_runtime_fingerprint.py
tests/test_self_healing_write_path.py
tests/test_shadow_scan_plumbing.py
```

## Non-code preconditions confirmed — safe to ship in Sprint 5

- **NSSM `AGT_DB_PATH` set on both services**: `agt-telegram-bot` and
  `agt-scheduler` both have
  `AGT_DB_PATH=C:\AGT_Runtime\state\agt_desk.db` in their
  `AppEnvironmentExtra`. Verified during Sprint 4 pre-sprint gate (see
  `reports/sprint4_local_sync_gate.md`).
- No boot-halt risk on deploy after the fallback elimination — services will
  resolve via env var exactly as they do now.

## Files to touch (Sprint 5 MR C re-scoped)

- `agt_equities/db.py:43-51` — lazy-resolve + `DB_PATH = None` at import; raise
  `RuntimeError` in `_resolve_db_path` if override + env both missing.
- `agt_scheduler.py:63` — just BASE_DIR; confirmed usage is LOG_DIR only, not
  DB path. Either leave alone or migrate to explicit env var `AGT_LOG_DIR`.
- `telegram_bot.py:167-169` — delete the `__file__` fallback; DB_PATH becomes
  `Path(os.environ["AGT_DB_PATH"])` with explicit KeyError on missing.
- `pxo_scanner.py:34-37` — same.
- `tests/conftest.py` autouse fixture — already uses env-var patching; confirm
  monkeypatch order is safe under lazy-resolve.
- `tests/test_file_anchor_overrides.py` — adjust to assert `RuntimeError`
  instead of the fallback resolution.
- ~15 other tests that implicitly depend on the fallback via module import —
  audit each, either inject `db_path=` kwarg or `monkeypatch.setenv` before
  import.

## Recommended Sprint 5 approach

Ship as a pair of small MRs, not one big one:

**MR C.1 (code-only, ~30 LOC):**
- Convert `agt_equities/db.py` to lazy-resolve.
- Delete `__file__` fallback in `telegram_bot.py`, `pxo_scanner.py`.
- Update `agt_scheduler.py` BASE_DIR → explicit AGT_LOG_DIR env var.
- Expect several CI test failures; that's by design — triaged in MR C.2.

**MR C.2 (test updates, ~50 LOC of fixture changes):**
- Update all 15+ tests to inject `db_path=` or set env var early.
- Convert `test_file_anchor_overrides.py` to expect `RuntimeError`.
- Green CI confirms the migration landed cleanly.

Splitting lets MR C.1 ship mechanically and MR C.2 focus on test-fixture
correctness, which is where the real risk lives.

## No URGENT

This is a latent bug (footgun, not active breakage). Production NSSM reliably
sets AGT_DB_PATH, so nothing is broken today. Sprint 5 ship window is
acceptable.
