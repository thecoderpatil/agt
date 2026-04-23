# Sprint 4 Mega-MR A — CSP Digest wiring

Per `reports/overnight_sprint_4_dispatch_20260424.md` Mega-MR A section.

## Scope (three integration surfaces)

1. **`agt_equities/csp_allocator.persist_latest_result`** + `load_latest_result` +
   `csp_allocator_latest` table migration (`scripts/migrate_csp_allocator_latest.py`).
   Fail-soft try/except at end of `run_csp_allocator` — persistence error never
   blocks an allocation run.
2. **Scheduler job `csp_digest_send`** in `telegram_bot.py` (PTB job_queue — same
   scheduler surface as `csp_scan_daily`). Fires 09:37 ET weekdays, 2-min gap
   after `csp_scan_daily` at 09:35 so the allocator lands its latest-row before
   the digest reads it. Soft-dep skip if the row is >30 min stale. Idempotent
   via `csp_pending_approval(run_id='digest:<date>')` singleton marker.
3. **`/approve_csp_<id>` + `/deny_csp_<id>`** slash-command handlers registered
   via regex filter (CommandHandler doesn't support dynamic numeric suffixes).
   Identity approval gate held; writes to `csp_pending_approval` only.

## cached_client tool-use — fallback path

`cached_client.py` is sync-only + text-only; extending it with async + tool-use
is ~150+ LOC of new surface + response-cache keying + result-shape divergence —
well over 1h per dispatch latitude. Took the fallback: new module
`csp_digest_runner.py` at the project root (outside `agt_equities/`) imports
`anthropic` directly via `_make_anthropic_factory()`. The structural sentinel
`tests/test_no_raw_anthropic_imports.py` only scans `agt_equities/`, so no ADR-010
§6.1 violation.

## Expected delta

```yaml expected_delta
files:
  agt_equities/csp_allocator.py:
    added: 125
    removed: 0
    net: 125
    tolerance: 20
    required_symbols:
      - persist_latest_result
      - load_latest_result
    required_sentinels:
      - "persist_latest_result failed"
      - "_sanitize_staged_for_persist"
  telegram_bot.py:
    added: 100
    removed: 0
    net: 100
    tolerance: 20
    required_symbols:
      - _scheduled_csp_digest_send
      - cmd_approve_csp
      - cmd_deny_csp
      - _csp_slash_set_status
    required_sentinels:
      - "csp_digest_send at 9:37 AM ET"
      - "/approve_csp_"
  scripts/migrate_csp_allocator_latest.py:
    added: 56
    removed: 0
    net: 56
    tolerance: 10
    required_sentinels:
      - "csp_allocator_latest"
      - "CHECK(id = 1)"
  csp_digest_runner.py:
    added: 316
    removed: 0
    net: 316
    tolerance: 40
    required_symbols:
      - run_csp_digest_job
      - build_digest_payload
    required_sentinels:
      - "SOFT_DEP_MAX_AGE_MINUTES"
      - "already_fired_today"
  tests/test_csp_digest_wiring.py:
    added: 397
    removed: 0
    net: 397
    tolerance: 40
    required_symbols:
      - test_persist_and_load_roundtrip
      - test_digest_idempotency_second_fire_same_day_is_noop
      - test_digest_tripwire_skips_llm_but_fires_message
    required_sentinels:
      - "pytest.mark.sprint_a"
      - "run_csp_digest_job"
  .gitlab-ci.yml:
    added: 1
    removed: 1
    net: 0
    tolerance: 2
    required_sentinels:
      - "test_csp_digest_wiring.py"
```

## Tests

11 new tests in `tests/test_csp_digest_wiring.py` all pass locally. sprint_a marker, registered in CI.

## Migration

`scripts/migrate_csp_allocator_latest.py` idempotent `CREATE TABLE IF NOT EXISTS`.
Already applied to live DB (`C:\AGT_Runtime\state\agt_desk.db`) during this
session so MR A's persist_latest_result path works immediately on merge+deploy.

## Ship timing decision

If this MR merges + deploys before 09:37 ET today (2026-04-24), first digest
fires today and observation week starts today. If merge-deploy is after 09:37,
first fire is Monday 2026-04-27 — acceptable per dispatch latitude.
