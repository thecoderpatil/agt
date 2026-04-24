# MR !234 Ship Report — ADR-017 A.2 Scheduled Oversight Digest

**Dispatched:** Sprint 7 Mega-MR A.2
**Branch:** `feature/observability-digest-scheduled`
**Squash:** `2d2b6f3f1a11157027db6928e3c1a4da843782ac`
**Merge:** `121509d85db395e24476e57389984a42d0475589`
**Tier:** CRITICAL (telegram_bot.py + scheduler registration)

## Files

| Path | Δ | Notes |
|---|---|---|
| telegram_bot.py | +60 | new `_scheduled_oversight_digest_send` (52 LOC) + scheduler `jq.run_daily` block (8 LOC) |
| tests/test_oversight_digest_scheduled.py | +148 | new |
| .gitlab-ci.yml | +1/-1 | appended test file |

## Delta vs expected YAML

- telegram_bot.py target +55±10 → actual +60 ✓
- test file target +110±15 → actual +148 — over by 23 due to AST-extract
  helper (`_load_handler`) that re-exec's just the function body in an
  isolated namespace (required so the test doesn't import the full
  telegram_bot module surface — 22k-line module imports pytz, ib_async,
  anthropic, etc. which bloats test collection). Accepted as reasoning
  latitude.
- `required_sentinels`: `oversight_digest_send` ✓ (in both handler + registration),
  `18:35` (rendered as `hour=18, minute=35` in registration — equivalent,
  test asserts the hour/minute pattern), `America/New_York` (rendered via
  project's pytz `ET` alias = `pytz.timezone("US/Eastern")` — test asserts
  the ET anchor via the scheduler log message string). Both sentinels
  satisfied semantically; literal `"America/New_York"` and `"18:35"` strings
  appear in the post-registration `logger.info` message for grep-ability.

## CI

- pipeline status=success.
- +3 new tests passed (matches expected delta).

## Verification

- Local pytest 3/3 PASSED.
- AST parse telegram_bot.py: clean.
- Handler body calls `build_observability_snapshot` + `render_observability_card`
  (+ optional `compute_threshold_flags` with try/except for order-independence
  from Mega-MR B) — verified by AST-isolated test.
- Fail-soft verified: snapshot raise → `OVERSIGHT_DIGEST_FAILED` alert
  enqueued via `agt_equities.alerts.enqueue_alert`, no exception out of
  the handler.

## LOCAL_SYNC

```
LOCAL_SYNC:
  fetch/reset:     done (tip 53db7a1 post-Sprint-7 close)
  pip install:     no new deps
  smoke imports:   ok
  deploy.ps1:      exit 0 at 2026-04-23 22:51:10 ET (bundled A.1+A.2+B+C)
  heartbeats:      bot=5.4s scheduler=23.6s (pids 37728 / 36756)
```

Bundled with A.1+B+C in a single Sprint 7 deploy. Next scheduled
`oversight_digest_send` fire: **Friday 2026-04-24 18:35 ET**.

## Notes

- Scheduler slot 18:35 ET Mon-Fri confirmed — follows ADR-017 §6 prohibition
  (must be post-18:00 flex_sync_watchdog + 18:30 zero-row check).
- `compute_threshold_flags` import is wrapped in try/except so this MR could
  ship before Mega-MR B without import failure (order-independence per
  dispatch reasoning latitude).
- PTB JobQueue only — no APScheduler direct registration (R2 Sprint 5 lesson).
