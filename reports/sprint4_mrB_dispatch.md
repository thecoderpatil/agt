# Sprint 4 Mega-MR B — Flex-sync freshness watchdog + ADR-FLEX_FRESHNESS_v1

Per `reports/overnight_sprint_4_dispatch_20260424.md` Mega-MR B section.

## Scope

1. Author `docs/adr/ADR-FLEX_FRESHNESS_v1.md` (ACCEPTED 2026-04-24) documenting
   Proposal C (fail-open data + fail-closed paging) and the 6h threshold
   rationale.
2. `agt_equities/flex_sync_watchdog.py` — external watchdog module. Queries
   `master_log_sync` via RO connection; stale ⇒ enqueue `FLEX_SYNC_MISSED` +
   write sentinel; fresh ⇒ delete sentinel.
3. `agt_scheduler.py` — register `flex_sync_watchdog` cron at 18:00 ET Mon-Fri.
4. `agt_equities/alerts.py:format_alert_text` — new `FLEX_SYNC_MISSED` branch.
5. `telegram_bot.py` — `/flex_status` read-only command.
6. **Zero touches to prohibited `flex_sync.py`.**
7. **`desk_state.md` banner deferred** per dispatch — flex_sync.py regenerates
   that file and a post-processor here would race on file handles.

## Expected delta

```yaml expected_delta
files:
  agt_equities/alerts.py:
    added: 11
    removed: 0
    net: 11
    tolerance: 5
    required_sentinels:
      - "FLEX_SYNC_MISSED"
      - "/flex_status"
  agt_scheduler.py:
    added: 26
    removed: 0
    net: 26
    tolerance: 5
    required_sentinels:
      - "flex_sync_watchdog"
      - "run_flex_sync_watchdog"
  telegram_bot.py:
    added: 55
    removed: 0
    net: 55
    tolerance: 10
    required_symbols:
      - cmd_flex_status
    required_sentinels:
      - "ADR-FLEX_FRESHNESS_v1"
      - "flex_status"
  .gitlab-ci.yml:
    added: 1
    removed: 1
    net: 0
    tolerance: 2
    required_sentinels:
      - "test_flex_sync_watchdog.py"
  agt_equities/flex_sync_watchdog.py:
    added: 178
    removed: 0
    net: 178
    tolerance: 20
    required_symbols:
      - run_flex_sync_watchdog
      - query_latest_sync
    required_sentinels:
      - "DEFAULT_STALE_THRESHOLD_HOURS"
      - "flex_sync_stale.flag"
      - "zero touches to agt_equities/flex_sync.py"
  docs/adr/ADR-FLEX_FRESHNESS_v1.md:
    added: 133
    removed: 0
    net: 133
    tolerance: 30
    required_sentinels:
      - "ACCEPTED 2026-04-24"
      - "Proposal C"
  tests/test_flex_sync_watchdog.py:
    added: 213
    removed: 0
    net: 213
    tolerance: 25
    required_sentinels:
      - "pytest.mark.sprint_a"
      - "run_flex_sync_watchdog"
      - "format_alert_text_flex_sync_missed"
```

## Tests

8 new tests pass locally in `tests/test_flex_sync_watchdog.py`. sprint_a
marker, registered in CI.

## Reasoning latitude applied

- **6h threshold chosen.** 36h alternative was considered and rejected because
  it ignores single-day silent failures.
- **SMTP/SMS sentinel-paging** deferred to a future operational MR (Task
  Scheduler configuration is out of scope for a code-only MR).
- **`desk_state.md` banner** deferred per dispatch — `flex_sync.py` regen
  race risk, no coordination point available without touching prohibited file.
