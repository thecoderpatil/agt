# ADR-FLEX_FRESHNESS_v1 — Flex-sync freshness watchdog

**Status:** ACCEPTED 2026-04-24
**Sprint:** 4, MR B
**Author:** Architect (Opus 4.7), implemented by Coder (Opus 4.6/4.7)
**Supersedes:** n/a (new capability)
**Supersededy by:** n/a

## Context

`agt_equities/flex_sync.py` is the IBKR Flex Query sync path that pulls trades,
dividends, and position snapshots into `master_log_*`. It is the upstream feed
for Walker-generated cycles and every downstream consumer (rule engine, CC
ladder, CSP allocator). Sprint 3 Investigation D (`reports/flex_sync_eod_timeout_assessment.md`)
confirmed the data path itself is atomic-txn-safe (F4 verification — partial
rows don't land; status is either `running`, `success`, or `error`).

Operator-notification path is best-effort. Three truly-silent failure modes
were identified:

1. **Empty XML response from IBKR** — the sync writes a `success` row with
   zero rows_received. Downstream consumers never see a failure signal.
2. **Bot-side alert consumer down** — the drain loop at `alerts.py`
   `drain_for_bot` is the bot-side consumer. If the bot is stopped or hung,
   `FLEX_SYNC_DIGEST` alerts are produced but never rendered to Telegram.
3. **Scheduler daemon stopped** — no sync runs at all. `master_log_sync`
   stops growing; nobody notices until the next downstream consumer hits
   stale data.

**Blast radius:** 1-day staleness is cosmetic. 7+ days is high-impact — the
CSP allocator's 90d IVR/VRP percentile math degrades, Walker cycles lose
accuracy on latest corporate actions, and any end-of-day report is wrong.

`agt_equities/flex_sync.py` is **prohibited outside Decoupling Sprint A**
per `CLAUDE.md` — so any freshness-guard must be implemented EXTERNAL to it.

## Decision

**Ship Proposal C from the assessment:** fail-open on data (never block a
downstream consumer on staleness alone) + fail-closed on paging (surface
every silent-failure mode via three redundant paths).

Three detection paths ship in MR B:

1. **Scheduler cron `flex_sync_watchdog`** in `agt_scheduler.py` — fires
   18:00 ET Mon-Fri. Queries `master_log_sync` via `get_ro_connection`;
   if the most-recent `success`/`running` row is older than the threshold,
   enqueues `FLEX_SYNC_MISSED` at severity `crit` and writes the sentinel
   file. If fresh, deletes the sentinel.

2. **Sentinel file `C:\AGT_Telegram_Bridge\state\flex_sync_stale.flag`** —
   atomic `os.replace` write. Present iff the last cron tick detected
   staleness. Consumed by any external observer (operator grep, SMTP
   daemon, future Task Scheduler paging job).

3. **`/flex_status` Telegram command** in `telegram_bot.py` — on-demand
   read-only query. Renders: last sync timestamp, age, status, sentinel
   presence. DB read wrapped in `asyncio.to_thread` per Sprint 3 MR 1
   pattern. No DB writes.

## Threshold

**6 hours** default (`DEFAULT_STALE_THRESHOLD_HOURS`). Rationale:

- Production flex_sync runs are weekday-daily around 16:30 ET. At 18:00 ET
  (the watchdog's cron), a successful sync is <2h old — well within the
  threshold.
- 6h tolerates typical intraday drift (e.g., a single sync deferred to the
  following morning) without paging.
- Weekend false positives are avoided because the cron is Mon-Fri only.
  The Friday 18:00 ET tick reads the Friday 16:30 ET sync (< 2h); Monday
  18:00 ET reads the Friday sync (~72h) — but we do NOT run the cron on
  weekends, so Monday's tick's baseline is the Monday morning sync, not
  the Friday one.

**Alternative considered:** 36h threshold (weekend-safe, only catches
multi-day outages). Rejected because it ignores single-day silent failures
which are the most common mode.

## Consequences

### Positive

- Catches all three silent-failure modes via redundant paths. Even if the
  bot alert consumer is down, the sentinel file is visible to any external
  observer. Even if the scheduler is stopped, the sentinel file's absence
  (never updated) combined with the age of the last alert in the bus
  surfaces the failure within 24 hours.
- Zero touches to prohibited `flex_sync.py`.
- Does NOT block downstream consumers on staleness — gate-behavior deferred
  to a follow-up ADR where we per-consumer review what "stale data" means
  (e.g., `csp_allocator` reading 3-day-old positions is worse than
  `dashboard_renderer` showing 3-day-old PnL — the gating policy must be
  consumer-specific).

### Negative

- The separate Windows scheduled task that pages on sentinel-stale-for-1h
  (SMTP/SMS backup notification) is explicitly deferred — that requires
  operational NSSM/Task-Scheduler work outside of code-only MR B.
- `desk_state.md` freshness banner is deferred. `flex_sync.py` regenerates
  that file, and a post-processor here would race on file handles. The
  `/flex_status` command covers the operator-facing freshness surface
  until the banner can be added in a later Decoupling-Sprint-A scoped MR.

### Follow-on work queued

1. **Per-consumer staleness gating ADR** — decides when stale data should
   block a downstream run vs fire an informational warning. E.g., the CSP
   allocator's IVR calculation uses 90 days of history; a 1-day gap is
   noise, but a 7-day gap changes ranks meaningfully.
2. **Windows Task Scheduler sentinel-based pager** — reads the sentinel
   file hourly; SMTP-pages Yash if present >1h. Outside of code MR scope.
3. **`desk_state.md` freshness banner** — requires a coordination point
   with `flex_sync.py`'s regen (e.g., both reading the same source of
   truth so no file-handle race).

## Tests

`tests/test_flex_sync_watchdog.py` (sprint_a marker, CI-registered):

- Cron alert on stale (with + without existing rows)
- Sentinel delete on fresh
- `/flex_status` rendering paths (no rows, fresh, stale, DB-error)
- Idempotency: consecutive stale runs do not duplicate alerts beyond one
  per cron tick

## References

- Source investigation: `reports/flex_sync_eod_timeout_assessment.md`
- Sprint 4 dispatch: `reports/overnight_sprint_4_dispatch_20260424.md`
- Per-MR fence: `reports/sprint4_mrB_dispatch.md`
- Prohibition source: `CLAUDE.md` §"Prohibited file touches"
