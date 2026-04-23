# Sprint 4 Rollup — 2026-04-24

**Status:** PARTIAL WIN. A + B shipped (critical path). C punted per dispatch. Investigation F delivered 9 HIGH / 9 MED / 6 LOW (24 total findings).

## Pre-sprint gate

- **LOCAL_SYNC deploy:** PASS (with one transient-anomaly note — manual recovery from deploy.ps1's SERVICE_STOP_PENDING race during rotation). Full detail: `reports/sprint4_local_sync_gate.md`.
- **Tip:** started df684ce1 (post-Sprint-3).
- **Heartbeats:** 48s / 59s post-restart (both under 120s threshold).
- **Observer PID:** background task launched for 2026-04-24; captured events within first minute.
- **Paper TRANSMIT smoke:** deferred to Yash (manual Telegram interaction required).

## Shipped MRs

| MR | Branch | Squash | Merge | LOC |
|----|--------|--------|-------|-----|
| !216 | feature/csp-digest-wiring | 8e028ad1 | cb223a89 | +995 new + modified |
| !217 | feature/flex-sync-freshness-watchdog | 36a8080d | c5b95c46 | +619 new + modified (+3 fixture updates) |

**Final tip:** `c5b95c46`.
**Deploy:** `deploy.ps1` exit 0 at 2026-04-23 09:02:15 ET. Backup 10.97 MB captured.
**Post-deploy heartbeats:** agt_bot pid=36428 age=45s; agt_scheduler pid=31776 age=51s (both FRESH).
**Scheduler boot log:** `Registered 12 job(s): [..., 'flex_sync_watchdog']` — new cron live.

## Reports written

- `reports/sprint4_local_sync_gate.md` — pre-sprint gate outcome
- `reports/sprint4_mrA_dispatch.md` — MR A LOC-gate fence
- `reports/sprint4_mrB_dispatch.md` — MR B LOC-gate fence
- `reports/mr216_ship.md` — MR A ship report (full)
- `reports/mr217_ship.md` — MR B ship report (full)
- `reports/mrC_dbpath_blocked.md` — MR C punt justification + Sprint 5 re-scope
- `reports/opus_bug_hunt_round_2_overnight.md` — Investigation F synthesis
- `reports/investigation_f1_telegram_bot_audit.md` — sub-agent F.1
- `reports/investigation_f2_agt_deck_audit.md` — sub-agent F.2
- `reports/investigation_f3_scanner_asyncio_audit.md` — sub-agent F.3
- `docs/adr/ADR-FLEX_FRESHNESS_v1.md` — MR B embedded ADR

## Paper observation week status

**Clock starts Friday 2026-04-24 09:37 ET** (the dispatch's "TODAY 2026-04-24"; system time at merge was Thursday 2026-04-23 09:02 post-07:37-slot, so the NEXT cron occurrence is Friday per PTB's scheduler next_run which the bot log confirms: `csp_scan_daily ... next run at: 2026-04-24 09:35:00 EDT`). Expected observation window:

- First digest fire: **Fri 2026-04-24 09:37 ET** (paper mode; identity gate held)
- Observation week end: Fri 2026-05-01 09:37 ET (5 trading days after first fire)
- ADR §5 step 4 live-flip dispatch: queue for 2026-05-01

**Correction to earlier draft of this rollup:** I initially wrote "Mon 2026-04-27" — off by 3 trading days. Architect flagged; verified the scheduler's next_run at 2026-04-24 09:35 EDT and corrected.

What to verify on first fire:
1. Telegram message received with PAPER header + per-ticker lines (or graceful
   "no candidates" if empty list).
2. `csp_pending_approval` has one new row: `run_id='digest:2026-04-27'`,
   `household_id='digest'`, `status='pending'`.
3. `llm_cost_ledger` has one row (or a `budget_exceeded` row if tripwire fired).
4. Paper auto-exec proceeds regardless of any approval taps.

## URGENT flags for Architect

**None live-capital today.** Investigation F surfaced notable findings; Architect wake-time review queue (from `reports/opus_bug_hunt_round_2_overnight.md`):

1. **F1-H-1 (CC dashboard)** — decide if dead callbacks are intentional.
2. **F1-H-2 (`pending` vs `staged` status)** — which semantic is correct for NL-staged tickets?
3. **F3-H-2 (`circuit_breaker.os.chdir`)** — design decision on whether to remove module-scope chdir.
4. **F2-H-2 (auth token in URL)** — coordinate Tailscale bookmark change with Yash.

Additionally, one **transient anomaly** from pre-sprint gate: `sqlite3.DatabaseError: database disk image is malformed` raised once during my `_check_invariants_tick` probe, swallowed by `auto-resolve sweep` handler. `PRAGMA integrity_check` returned `('ok',)` immediately after, so DB is NOT corrupt. Likely WAL contention during deploy rotation. If it recurs post-market-open, it's a real signal; if not, one-time.

## Deferred / blocked

- **MR C (E-M-4, `__file__` DB_PATH fallback elimination):** PUNTED to Sprint 5 per dispatch punt-if-tight. 25 tests touch `DB_PATH`/`AGT_DB_PATH` — over the 20-test ceiling. Recommended Sprint 5 split: MR C.1 (code-only) + MR C.2 (test updates). `reports/mrC_dbpath_blocked.md` has full plan.

- **Trivial HIGH-severity fixes from Investigation F:** 7 candidate MRs queued for Sprint 5 triage. 3 need Architect ruling first (F1-H-1, F1-H-2, F3-H-2); others are mechanical pattern extensions of MR 3 Sprint 3's tx_immediate sweep.

## Market-open canary (09:30 ET Friday 2026-04-24)

1. Paper CSP scan runs at 09:35 ET without `csp_allocator.skip_inactive` log line.
2. **CSP digest fires at 09:37 ET (MR A) — first fire of observation week.** Yash should see a Telegram message with PAPER header + per-ticker lines (or graceful "No candidates staged today" if empty list).
3. CC scan at 09:45 ET — picker enforces OTM floor + weekly DTE (MR !197).
4. Heartbeats <60s on both services through open.
5. Watchdog cron at 18:00 ET Mon-Fri (MR B) — first fire Friday evening; confirm `agt_scheduler.log` shows `flex_sync_watchdog:` log line with a `status=fresh` dict.

**Known soft-miss from today (2026-04-23 Thursday):** bot restarted at 09:02:29 ET; scheduler miss-fired today's 09:35 csp_scan_daily slot by 6.5s due to APScheduler misfire_grace. Also, `bot_heartbeat` (30s interval) is chronically missing by 1-2s per tick — pre-existing executor-pool load, not a Sprint 4 regression. Tomorrow's 09:35 scan should fire cleanly (scheduler up 24h+ by then, executor idle). If tomorrow's slot ALSO misses, that's the real signal to escalate.

## Notes for next Sprint

- MR A's cached_client-fallback path (`csp_digest_runner.py` at project root) is
  technical debt — acceptable for observation week, but a cleaner long-term
  direction is extending cached_client with async + tool_use support in a
  focused MR (~150 LOC + tests). Architect should weigh that against the
  Proposal C alternative (keep the project-root module as-is).
- Pre-sprint gate's transient `database disk image is malformed` should be
  covered by a proactive `PRAGMA integrity_check` after any deploy-cycle
  that touches NSSM services; add to `scripts/deploy/deploy.ps1` post-start
  sanity in a future MR.
- Sprint 3 MR 5 E-M-6 (invariant tick before heartbeat) was observed to delay
  the first post-restart heartbeat by ~4 minutes due to WAL contention on
  `_check_invariants_tick`. Not a regression — steady-state works fine once
  the initial cycle is past — but worth noting that the order change tightened
  the coupling between heartbeat liveness and invariant-tick latency.
