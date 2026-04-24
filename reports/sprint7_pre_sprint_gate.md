# Sprint 7 Pre-Sprint Gate

**Date:** 2026-04-23 (post-Sprint 6 close)
**Base tip:** `8badd612` (Sprint 6 final — `8badd612 Merge branch 'feature/sprint6-adr011-first-subdispatch' into 'main'`)
**Target:** ADR-017 §9 first sub-dispatch — 4 Mega-MRs (A.1, A.2, B, C)

## Anchor verification

| Anchor | File | Confirmed |
|---|---|---|
| csp_digest package exists (boundary marker) | `agt_equities/csp_digest/` | ✓ (approval_gate, cost_ledger, formatter) — NOT imported per §6 prohibition |
| incidents_digest is script, not library | `scripts/incidents_digest.py` | ✓ (do not import main) |
| flex_sync_watchdog public API | `agt_equities/flex_sync_watchdog.py` | ✓ `run_flex_sync_watchdog`, `query_latest_sync`, `check_zero_row_suspicion` |
| paper_baseline API | `agt_equities/paper_baseline.py` | ✓ `evaluate_all(engine, *, window_days=14, db_path=None) -> list[GateResult]` |
| promotion_gates module | `agt_equities/promotion_gates.py` | ✓ (consumed via paper_baseline) |
| engine_state / pregateway | unchanged from dispatch | Sprint 6 Mega-MR 5 shipped `engine_state.py` + `pregateway.py` stubs; not blockers |
| incidents canonical columns | `incidents` table | ✓ `severity`, `scrutiny_tier`, `severity_tier`, `error_budget_tier` all present; B uses `error_budget_tier` |
| incidents_repo.list_architect_only / list_authorable | `agt_equities/incidents_repo.py:398, :495` | ✓ (kwargs: `statuses`, `manifest`, `db_path`) — **no** `since_utc` kwarg, adapter in A.1 |

## API-shape adaptations flagged

1. `incidents_repo.list_authorable` / `list_architect_only` take `statuses` + `manifest` + `db_path` — no `since_utc`/`for_date`. Sprint 7 A.1 renders current active (open/rejected) rows; "today's" filtering is implicit in the stabilized-incident semantic.
2. `paper_baseline.evaluate_all` is per-engine; A.1 loops over `["entry", "exit", "harvest", "roll"]`.
3. `cross_daemon_alerts` schema: `(kind, severity, created_ts REAL, payload_json)`. B queries by `kind = 'FLEX_SYNC_EMPTY_SUSPICIOUS'` with `created_ts` in today's UTC window.
4. `daemon_heartbeat` schema: `(daemon_name, last_beat_utc TEXT, pid, client_id, notes)`. A.1 snapshot uses `MAX(last_beat_utc) GROUP BY daemon_name`.
5. PTB JobQueue uses `_time(hour=18, minute=35, tzinfo=ET)` per existing `csp_digest_send` registration at telegram_bot.py:22346 — not `ZoneInfo`. Dispatch reasoning-latitude clause permits adapting.
6. Command registry count currently 25; adding `oversight_status` bumps to 26.

## Services / HEAD

- `git reset --hard origin/main` applied; tip `8badd612`.
- Services check deferred to post-deploy (covered by LOCAL_SYNC block in each ship report).

## Ship order

1. A.1 first (library; blocks C).
2. B (pure, no dependency on A.1's module; but its flags render through A.1's card — integrates fine because A.1 accepts `threshold_flags` arg).
3. A.2 (calls A.1 build + B compute + A.1 render).
4. C (command — calls same trio as A.2).

Ready to ship.
