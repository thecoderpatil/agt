# MR !217 ship report — flex-sync freshness watchdog + ADR-FLEX_FRESHNESS_v1 (Sprint 4 Mega-MR B)

## Status
MERGED. Squash `36a8080d`, merge `c5b95c46`. Deployed via `scripts/deploy/deploy.ps1` at 2026-04-23 09:02:15 ET; services restarted cleanly. Scheduler log confirms `Registered 12 job(s): [..., 'flex_sync_watchdog']` on boot.

## Scope shipped

- **ADR:** `docs/adr/ADR-FLEX_FRESHNESS_v1.md` (ACCEPTED 2026-04-24). Documents
  Proposal C (fail-open data + fail-closed paging) and the 6h threshold.
- **Watchdog module:** `agt_equities/flex_sync_watchdog.py` — `query_latest_sync`
  (RO), `run_flex_sync_watchdog` (cron body), sentinel file management (atomic
  `os.replace` write, best-effort delete).
- **Scheduler:** `agt_scheduler.py` registers `flex_sync_watchdog` cron at 18:00 ET
  Mon-Fri.
- **Alert rendering:** `agt_equities/alerts.py:format_alert_text` new
  `FLEX_SYNC_MISSED` branch.
- **Telegram command:** `/flex_status` — read-only freshness query; DB read
  wrapped in `asyncio.to_thread` per Sprint 3 MR 1 pattern.
- **Command registry:** `agt_equities/command_registry.py` entry for `flex_status`
  so `test_command_registry_parity` is green.

## Follow-up commits on the same branch

**Pipeline 1 (2474597480)** failed with 2 test-fixture mismatches:

1. `test_agt_scheduler::test_register_jobs_a5e_set` — fixture expected 11-entry
   list; appended `flex_sync_watchdog`.
2. `test_command_registry_parity::test_all_registered_commands_are_in_registry`
   — `flex_status` was missing from `COMMAND_REGISTRY`; added entry.

Fixes shipped as follow-up commit `be172a58`.

**Pipeline 2 (2474615552)** still failed — `test_registry_has_expected_command_count`
pinned `_EXPECTED_COUNT = 24` (now 25 with flex_status). Fixed as follow-up
commit `402adec2`.

**Pipeline 3 (2474626562)** went green on rerun with all three fixture updates.

## Deferred

- `desk_state.md` freshness banner (race with `flex_sync.py` regen).
- SMTP/SMS sentinel-based paging (Task Scheduler config).

## Delta vs origin/main

- `agt_equities/alerts.py`: +11 LOC
- `agt_scheduler.py`: +26 LOC
- `telegram_bot.py`: +55 LOC
- `.gitlab-ci.yml`: +1 / -1
- `agt_equities/flex_sync_watchdog.py`: +178 (new)
- `docs/adr/ADR-FLEX_FRESHNESS_v1.md`: +133 (new)
- `tests/test_flex_sync_watchdog.py`: +213 (new)
- `tests/test_agt_scheduler.py`: +2 (fixture update)
- `agt_equities/command_registry.py`: +1 (registry entry)

## Verification

- 8 new tests pass locally + CI
- 2 fixture updates verified local + CI
- `ast.parse` clean on all modified files
- Zero touches to `agt_equities/flex_sync.py` (prohibited)
- Zero touches to other prohibited files

## LOCAL_SYNC
```
LOCAL_SYNC:
  fetch/reset:     done (Coder worktree at c5b95c46)
  pip install:     no new deps
  smoke imports:   ok (deploy.ps1 exit 0)
  deploy.ps1:      exit 0 at 2026-04-23 09:02:15 ET (pre-flight backup 10.97 MB captured)
  heartbeats:      agt_bot=45s pid=36428; agt_scheduler=51s pid=31776 (both FRESH post-restart)
  scheduler log:   "Registered 12 job(s): [..., 'flex_sync_watchdog']" confirmed
```
