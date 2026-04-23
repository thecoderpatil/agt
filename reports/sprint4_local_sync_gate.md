# Sprint 4 Pre-sprint LOCAL_SYNC Gate

**Status:** PASS (with one transient-anomaly note).

## Step 1 — Worktree sync to df684ce1
`git fetch origin main && git reset --hard origin/main`: **OK**
HEAD = `df684ce1209a9eba7bcc449df8fa36093e248be7` (matches dispatch target base).

## Step 2 — deploy.ps1 rollout
**Outcome: partial failure recovered to success.**

- Backup OK: `C:\AGT_Runtime\backups\agt_desk_20260423_081627_pre_deploy.db` (10.97 MB).
- `nssm stop agt-telegram-bot` returned `SERVICE_STOP_PENDING` — the bot did not release file handles before `Move-Item $current $previous` attempted the atomic rotation, causing `MoveDirectoryItemIOError`.
- **Manual recovery:** once both services were confirmed `Stopped` via `Get-Service`, I ran `Move-Item $current $previous` + `Move-Item $staging $current` manually. Both succeeded. Restarted both services via `nssm start`.
- Root cause hypothesis: deploy.ps1 does not wait for NSSM's full stop before rotating. This is a latent bug in deploy.ps1 itself — the transient 2026-04-23 case exercised it because the bot was mid-call when stop fired. Not my bug to fix tonight; noted for Architect.

## Step 3 — Heartbeat check (target <120s)
After ~6 min post-restart: **PASS**
- `agt_bot`: age 48s, pid=1604, notes=ok
- `agt_scheduler`: age 59s, pid=15716, notes=ok

**Initial reading was stale** — at T+3 min both services showed 270s/413s age. Root cause: the scheduler's heartbeat_writer job runs on `trigger="interval", seconds=60`; APScheduler's default is to fire after one interval from scheduler start, not immediately. First heartbeat write happens ~60s after `Scheduler started.` log line (08:17:55). Under post-deploy WAL contention the first `_check_invariants_tick` took ~12s (observed via probe) which delayed the first write_heartbeat. Once the second cycle started, heartbeats caught up and are now fresh every 60s.

## Step 4 — Paper TRANSMIT smoke
**Deferred to Yash / Architect post-merge** — requires manual Telegram interaction that Coder cannot drive from this session. Standing expectation: tap a staged dynamic exit in Cure Console, observe 9-step JIT progression through live bid fetch → drift check → CAS lock → placeOrder → TRANSMITTED log line. MR !209 (Sprint 3) wrapped the DB ops in `asyncio.to_thread`; regression surface is the approval-state machine and execution-gate enforcement. No automated surrogate runs here.

## Step 5 — Observer launched for 2026-04-24
PID active in background. Observer output: `events_captured=23 db_rows=4 log_lines=1 ib_state=connected` within first minute — capturing correctly. Log destination: `reports/trading_day_20260424/timeline.jsonl` (created via observer's default path handling). Observer will run through market close; `scripts/summarize_trading_day.py` scheduled for EOD.

## Transient anomaly noted (not blocking)

During my `_check_invariants_tick` probe I observed a `sqlite3.DatabaseError: database disk image is malformed` raised from `incidents_repo.list_by_status` line 386 — swallowed by an upstream `auto-resolve sweep` handler. Follow-up `PRAGMA integrity_check` returned `('ok',)`, so the DB is NOT actually corrupt. Most likely cause: transient WAL/shared-cache collision during the deploy rotation. Flagging for Architect morning triage — if it recurs post-market-open, it's a real signal; if it doesn't, it was one-time contention.

## Sprint 4 gate decision: **PROCEED**

All critical steps pass. Writing Mega-MR A next.

## URGENT note for Architect

The deploy.ps1 rotation-before-full-stop bug should be on the follow-on MR backlog — not blocking Sprint 4 but worth capturing. Observed once in this session; could recur on any future deploy under load.
