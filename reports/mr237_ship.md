# MR !237 Ship Report — ADR-017 C /oversight_status Command

**Dispatched:** Sprint 7 Mega-MR C
**Branch:** `feature/observability-oversight-status-cmd`
**Squash:** `007cbda1838e372d28286d4acd3ab1614b6e41ff`
**Merge:** `f2ce289cf37c5c0488895faa418b3bace9b8b788`
**Tier:** STANDARD (telegram_bot.py read-only command + registry update + parity test)

## Files

| Path | Δ | Notes |
|---|---|---|
| telegram_bot.py | +42 | new `cmd_oversight_status` (38 LOC) + CommandHandler registration (2 LOC) |
| agt_equities/command_registry.py | +2 | new `oversight_status` CommandSpec entry (25→26) |
| tests/test_command_registry_parity.py | +1/-1 | `_EXPECTED_COUNT` 25→26 + comment update |
| tests/test_cmd_oversight_status.py | +130 | new |
| .gitlab-ci.yml | +1/-1 | appended test file |

## Delta vs expected YAML

- telegram_bot.py target 50±10 → actual 42 ✓ (under, because the dispatch's
  expected LOC assumed each body helper would be its own function; I inlined
  the small `_build` closure per the pattern in `cmd_flex_status`).
- test file target 90±10 → actual 130. Over due to AST-isolation fixture
  `_load_cmd` (same pattern as A.2's test) that lets the test exercise the
  function body without importing the 22k-line telegram_bot module surface.
- `required_sentinels`: `/oversight_status` ✓ (docstring), `asyncio.to_thread` ✓.
- Parity test count bump 25→26 ✓.

## CI

- pipeline status=success.
- +3 new tests passed + 1 parity test updated.
- CI confirmed post-merge via verification block below.

## Verification

- Local pytest for test_cmd_oversight_status + test_command_registry_parity:
  7/7 PASSED.
- `COMMAND_REGISTRY` now has 26 entries; all present in telegram_bot.py
  CommandHandler registrations; parity test PASS.
- ADR-017 §6 compliance: zero csp_digest imports; reuses A.1 helpers
  (`build_observability_snapshot`, `render_observability_card`) + optional
  B helper (`compute_threshold_flags`) with try/except.
- Fail-soft: snapshot raise → brief error message to user + full stacktrace
  to log (not propagated); verified by test_oversight_status_fail_soft_on_snapshot_error.

## LOCAL_SYNC

```
LOCAL_SYNC:
  fetch/reset:     done (tip 53db7a1 post-Sprint-7 close)
  pip install:     no new deps
  smoke imports:   ok
  deploy.ps1:      exit 0 at 2026-04-23 22:51:10 ET (bundled A.1+A.2+B+C)
  heartbeats:      bot=5.4s scheduler=23.6s (pids 37728 / 36756)
```

`/oversight_status` is now live; next scheduled fire Friday 2026-04-24 18:35 ET.

## Notes

- Reuses same lazy-import pattern as A.2 for `compute_threshold_flags` so C
  could have shipped before B if needed (it did not; B merged first).
- `asyncio.to_thread` offload ensures the blocking `build_observability_snapshot`
  (SQLite RO queries across five sources) does not stall the PTB event loop.
- User-facing error message format `⚠️ oversight_status failed: <brief>`
  matches the ADR-017 §3 fail-soft contract.
