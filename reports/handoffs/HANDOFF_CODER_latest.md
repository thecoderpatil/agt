# AGT Equities — Coder (Claude Code) Handoff

**Last updated:** 2026-04-07
**Status:** Phase 3A Stage 2 complete. Stage 3 (Telegram integration) next.
**Tests:** 156/156 passing. Runtime: 17.50s.

---

## You are Claude Code, executing on Yash's Windows machine

You take prompts from Architect (Claude chat in the AGT Equities project). Yash is the bus between Architect and you. Your role: precise execution, report findings, never auto-fix without Architect approval.

**Operating rules:**
1. Read this file at the start of every session before any work.
2. Read `desk_state.md` at `C:\AGT_Telegram_Bridge\desk_state.md` for current portfolio state.
3. Report-first on every task. Discovery -> STOP -> wait for Architect review -> implementation.
4. Worked examples with real numbers in every report.
5. Try/except wrapper on every file write, DB write, network call.
6. Never write to `master_log_*` (Bucket 2 pristine — only `flex_sync.py` does that).
7. Never commit secrets. `.env`, `*.db`, `audit_bundles/` are gitignored.
8. Status format: `<Phase> done | tests: X/Y | <key metric> | STOP | <report path>`

---

## Working Directory

`C:\AGT_Telegram_Bridge\`

**Key files you touch most:**
- `telegram_bot.py` — main bot entry, ~9500 lines (pruning target 3000 in Phase 3D)
- `agt_equities/walker.py` — pure function, source of truth for cycles
- `agt_equities/rule_engine.py` — pure rule evaluators (4 real, 7 PENDING stubs)
- `agt_equities/mode_engine.py` — 3-mode state machine + LeverageHysteresisTracker
- `agt_equities/seed_baselines.py` — glide path seed data
- `agt_equities/flex_sync.py` — EOD master log writer + git auto-push hook
- `agt_deck/main.py` — FastAPI Command Deck
- `agt_deck/risk.py` — leverage, EL, concentration helpers
- `agt_deck/queries.py` — DB read layer
- `agt_deck/desk_state_writer.py` — generates desk_state.md every 5 min
- `agt_deck/templates/cure_console.html` + `cure_partial.html`
- `schema.py` — all SQLite migrations, idempotent DDL
- `tests/` — pytest suite, 156 tests
- `scripts/archive_handoffs.py` — Friday handoff archiver

---

## Architecture Reminders

**3-Bucket data model (LOCKED):**
1. Real-time API — TWS via ib_async, no persistence
2. `master_log_*` tables — immutable, only `flex_sync.py` writes
3. Operational state — everything else (pending_orders, glide_paths, mode_history, el_snapshots, sector_overrides, walker_warnings_log, etc.)

**3-mode state machine:**
- PEACETIME (all green, Lev <1.40x) -> normal ops
- AMBER (any yellow, Lev 1.40-1.49x) -> block new CSP entries, exits/rolls allowed
- WARTIME (any red, Lev >=1.50x) -> block all LLM, Cure Console only, mandatory post-action audit

**Walker is a PURE FUNCTION.** Never mutate it. Wrap impure helpers in pure interfaces (see `compute_leverage_pure()` pattern wrapping `gross_beta_leverage()`).

**Day 1 must compute GREEN.** If any rule computes AMBER or RED on baseline values, that's a math error or baseline error — STOP and report.

---

## Current State

- Tests: 156/156 (114 base + 42 Phase 3A Stage 1)
- Mode: PEACETIME
- Walker: fully closed through W3.8 + W3.6 (warnings UI) + W3.7 (Hypothesis property tests, 18 properties)
- Cure Console: live at `/cure`, mobile-responsive, Tailscale-exposed at `0.0.0.0:8787`
- Mode Badge: live on top strip, clickable
- GitLab: connected as `git@gitlab.com:agt-group2/agt-equities-desk.git`, daily auto-push from flex_sync EOD
- Litestream: continuous DB replication to Cloudflare R2

---

## In Flight

**Phase 3A Stage 3: Telegram Integration**
- New commands: `/declare_wartime`, `/declare_peacetime`, `/mode`, `/cure`
- Mode transition push alerts hooked into `mode_engine.py`
- `/scan` and `/cc` blockers: AMBER blocks `/scan` and new CSP entries; WARTIME blocks both
- Update existing Rule 11 blocker to use new mode engine instead of direct leverage check

After Stage 3 -> Stage 4 (validation, screenshots, report).

---

## Phase Backlog (do NOT start without Architect prompt)

- **Phase 3A.5:** Real evaluators for R4/R5/R6/R8/R9 (currently PENDING stubs)
- **Phase 3B:** desk_state.md full integration, workstream.md protocol
- **Phase 3C:** CIO decision packets (Telegram-triggered, Deck-reviewed, cached Opus/Sonnet, adaptive Smart Friction)
- **Phase 3D:** Telegram pruning to ~3000 lines, kill display commands, replace text approvals with inline buttons
- **Phase 3E:** Mobile responsive polish (partially shipped via Tailscale)

---

## Active Gotchas / Don't-Touch List

1. **R4/R5/R6/R8/R9 stubs** — return GREEN, do not implement until Phase 3A.5 prompt arrives
2. **EL data source** — IBKR live `ExcessLiquidity`, snapshots to `el_snapshots`, NOT master log
3. **`gross_beta_leverage()`** — impure, has module-level hysteresis dict. Use `compute_leverage_pure()` wrapper from rule engine.
4. **UBER sector** — `sector_overrides` table, manual override to Consumer Cyclical
5. **`/cc` AMBER behavior** — allowed (it's exits/rolls), only block in WARTIME
6. **`/exit_math`** — deprecated W3.8 stub, merging into Cure Console row expansion in Phase 3C
7. **CORP_ACTION handler** — synthetic-tested only, Flex shape verification needed on first real one
8. **`boot_deck.bat` token** — placeholder, rotate to `.env` read (followup, non-blocking)
9. **Hardcoded account numbers in `backfill_trade_ledger.py`, `agt_deck/queries.py`, `agt_deck/main.py`** — these are routing keys, not secrets, OK to commit
10. **Git auto-push hook** — wrapped in try/except, never blocks flex_sync. Don't break this.

---

## Backup System

- **Code -> GitLab** `git@gitlab.com:agt-group2/agt-equities-desk.git` (SSH `@yashpatil1`)
- **Auto-push** at end of every successful `flex_sync.py` run, message `auto: EOD YYYY-MM-DD`
- **DB -> Cloudflare R2** via Litestream, continuous, 30-day retention
- **Friday handoff archive** via `scripts/archive_handoffs.py` — copies `*_latest.md` to dated files
- **NEVER commit:** `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `audit_bundles/`, `data/inception_carryin.csv`, `Archive/`, `.venv/`, `.hypothesis/`, `.claude/`, Litestream WAL segments

---

## Status Format (use exactly this)

    <Phase/Task> done | tests: X/Y | <key metrics> | STOP | <report path>

Example:

    Phase 3A Stage 2 done | tests: 156/156 | templates render: 59K chars | Day 1: PEACETIME | STOP | reports/phase_3a_stage2_20260407.md

---

## Reports Directory Convention

- All reports -> `reports/` with date suffix `YYYYMMDD.md`
- Discovery reports -> `reports/<phase>_discovery_YYYYMMDD.md`
- Implementation reports -> `reports/<phase>_implementation_YYYYMMDD.md`
- Stage reports -> `reports/<phase>_stage<N>_YYYYMMDD.md`
- Setup/infra reports -> `reports/<task>_setup_YYYYMMDD.md`

---

## Hard Stops

Always stop and report (do NOT auto-fix) on:
- Any rule disagrees with Rulebook v9 spec
- Walker purity would be violated by a fix
- Bucket 2 would be written by anything other than `flex_sync.py`
- Day 1 baseline computes AMBER or RED
- A fix might introduce new bugs (say so explicitly)
- Audit narrowing — do not reduce scope to protect prior work
- Secret found in staged files
- SSH/git auth fails

---

## How to Pick Up (new session ritual)

1. Read this file end-to-end.
2. Read `desk_state.md` at `C:\AGT_Telegram_Bridge\desk_state.md`.
3. Read latest report in `reports/` to know what just finished.
4. Wait for Architect prompt. Do not start work autonomously.

End of handoff.
