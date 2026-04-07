# AGT Equities — Coder (Claude Code) Handoff

**Last updated:** 2026-04-07
**Status:** Phase 3A Stage 3 complete. Stage 4 (validation) next.
**Tests:** 170/170 passing. Runtime: 14.29s.

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
- `telegram_bot.py` — main bot entry, ~10300 lines (pruning target ~4500 in Phase 3D)
- `agt_equities/walker.py` — pure function, source of truth for cycles
- `agt_equities/rule_engine.py` — pure rule evaluators (R1/R2/R3/R11 real, R4-R10 PENDING stubs)
- `agt_equities/mode_engine.py` — 3-mode state machine + LeverageHysteresisTracker + glide path math
- `agt_equities/seed_baselines.py` — glide path + sector override + initial mode seed data
- `agt_equities/flex_sync.py` — EOD master log writer + walker warnings persist + desk_state regen + git auto-push
- `agt_equities/schema.py` — all SQLite migrations, idempotent DDL
- `agt_deck/main.py` — FastAPI Command Deck + Cure Console routes
- `agt_deck/risk.py` — leverage, EL, concentration, sector helpers
- `agt_deck/queries.py` — DB read layer
- `agt_deck/desk_state_writer.py` — generates desk_state.md (atomic write)
- `agt_deck/templates/cure_console.html` + `cure_partial.html` — Cure Console UI
- `agt_deck/templates/command_deck.html` — main deck with Mode Badge + Lev links
- `tests/test_walker.py` — 91 walker unit + W3.6 tests
- `tests/property/test_walker_properties.py` — 23 Hypothesis property tests
- `tests/test_phase3a.py` — 56 Phase 3A tests (rule engine + mode engine + glide paths + gates)
- `scripts/archive_handoffs.py` — Friday handoff archiver

---

## Architecture Reminders

**3-Bucket data model (LOCKED):**
1. Real-time API — TWS via ib_async, no persistence
2. `master_log_*` tables (12 tables) — immutable, only `flex_sync.py` writes
3. Operational state — everything else (pending_orders, glide_paths, mode_history, el_snapshots, sector_overrides, walker_warnings_log, etc.)

**3-mode state machine:**
- PEACETIME (all glide paths GREEN) -> normal ops
- AMBER (any glide path AMBER) -> block `/scan` and new CSP entries; `/cc` exits/rolls allowed
- WARTIME (any glide path RED) -> block `/scan` AND `/cc`; Cure Console only; mandatory audit memo to revert

**Mode gates in telegram_bot.py:**
- `/scan`: `_check_mode_gate("PEACETIME")` — blocked in AMBER + WARTIME
- `/cc`: `_check_mode_gate("AMBER")` — blocked in WARTIME only
- Existing Rule 11 leverage check preserved post-gate (defense in depth)

**Mode source:** `mode_history` TABLE via `_get_current_desk_mode()` → direct SQLite query. NOT from `desk_state.md`. Zero staleness.

**Walker is a PURE FUNCTION.** Never mutate it. Wrap impure helpers in pure interfaces (see `compute_leverage_pure()` pattern).

**Day 1 must compute GREEN.** Baselines == current values → delta == 0 → GREEN. If any rule computes AMBER or RED on baseline, that's a math/baseline error — STOP.

---

## Current State

- **Tests:** 170/170 (91 walker + 23 property + 56 phase3a)
- **Mode:** PEACETIME (verified on live DB — all glide paths GREEN at Day 0)
- **Walker:** fully closed through W3.8 + W3.6 (WalkerWarning dataclass + UI) + W3.7 (18 Hypothesis properties)
- **Cure Console:** live at `/cure`, mobile-responsive, HTMX 60s refresh, Tailscale-exposed at `0.0.0.0:8787`
- **Mode Badge:** live on Command Deck + Cure Console top strip, clickable to `/cure`
- **Lev cells:** linkified to `/cure` on Command Deck
- **Telegram commands:** `/declare_wartime <reason>` (required), `/declare_peacetime <memo>` (required from WARTIME), `/mode`, `/cure`
- **Push alerts:** mode transitions → emoji-coded Telegram message to AUTHORIZED_USER_ID
- **GitLab:** `git@gitlab.com:agt-group2/agt-equities-desk.git` (SSH `@yashpatil1`), daily auto-push
- **Litestream:** continuous DB replication to Cloudflare R2

**Household state (2026-04-06 Flex):**

| Household | NAV | Leverage | Top concentration |
|-----------|-----|----------|-------------------|
| Yash | $261,902 | 1.59x (BREACHED) | ADBE 46.7% |
| Vikram | $80,787 | 2.15x (BREACHED) | ADBE 60.5% |

**Glide paths seeded:** 10 rows (2 leverage, 8 concentration). PYPL paused (earnings-gated).
**Sector override:** UBER → Consumer Cyclical (fixes SW-App 3→2 violation).

---

## Completed Work (this session)

| Task | Tests | Report |
|------|-------|--------|
| W3.6: Walker Warnings UI | 91 (+14) | `reports/w3_6_implementation_20260407.md` |
| W3.7: Hypothesis Property Tests | 114 (+23) | `reports/w3_7_implementation_20260407.md` |
| Phase 3A Discovery | — | `reports/phase_3a_discovery_20260407.md` |
| Phase 3A Stage 1: Foundation | 156 (+42) | `reports/phase_3a_stage1_20260407.md` |
| Phase 3A Stage 2: Cure Console UI | 156 (no new) | `reports/phase_3a_stage2_20260407.md` |
| Phase 3A Stage 3: Telegram Integration | 170 (+14) | `reports/phase_3a_stage3_20260407.md` |

---

## In Flight

**Phase 3A Stage 4: Validation**
- Full test suite pass (target: maintain 170+)
- Run live against prod DB in read-only mode, confirm all rules compute GREEN on Day 1
- Generate sample `desk_state.md` and paste in report
- Render Cure Console screenshots
- Confirm mode engine computes PEACETIME
- Final implementation report at `reports/phase_3a_implementation_20260407.md`

---

## Phase Backlog (do NOT start without Architect prompt)

- **Phase 3A.5:** Real evaluators for R4/R5/R6/R8/R9 (currently PENDING stubs)
- **Phase 3B:** desk_state.md full integration (5-min APScheduler, EL snapshots from IBKR live API)
- **Phase 3C:** CIO decision packets (Telegram-triggered, Deck-reviewed, cached Opus/Sonnet, adaptive Smart Friction)
- **Phase 3D:** Telegram pruning to ~4500 lines, kill display commands, replace text approvals with inline buttons, rip CIO payload generators
- **Phase 3E:** Mobile responsive polish

---

## Active Gotchas / Don't-Touch List

1. **R4/R5/R6/R8/R9 stubs** — return PENDING (gray pills), do not implement until Phase 3A.5
2. **EL data source** — IBKR live `ExcessLiquidity` → `el_snapshots` table (Bucket 3). Writer not yet wired (Phase 3B)
3. **`gross_beta_leverage()`** — impure, has module-level hysteresis dict. Use `compute_leverage_pure()` from rule_engine
4. **UBER sector** — `sector_overrides` table, manual override to Consumer Cyclical
5. **`/cc` AMBER behavior** — allowed (exits/rolls), only block in WARTIME
6. **`/declare_wartime` and `/declare_peacetime`** — both require reason/memo (mandatory audit trail)
7. **`/exit_math`** — never implemented (W3.8 deferred), merging into Cure Console in Phase 3C
8. **CORP_ACTION handler** — synthetic-tested only, Flex shape verification needed on first real one
9. **ADBE/PYPL Dynamic Exits** — Yash handles personally, don't touch
10. **Git auto-push hook** — wrapped in try/except, never blocks flex_sync. Don't break this.
11. **Hardcoded account numbers** in queries.py, main.py — routing keys, not secrets, OK to commit

---

## Backup System

- **Code -> GitLab** `git@gitlab.com:agt-group2/agt-equities-desk.git` (SSH `@yashpatil1`)
- **Auto-push** at end of every successful `flex_sync.py` run
- **DB -> Cloudflare R2** via Litestream, continuous, 30-day retention
- **Friday handoff archive** via `scripts/archive_handoffs.py`
- **NEVER commit:** `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `audit_bundles/`, `data/inception_carryin.csv`, `Archive/`, `.venv/`, `.hypothesis/`, `.claude/`, Litestream WAL segments

---

## Hard Stops

Always stop and report (do NOT auto-fix) on:
- Any rule disagrees with Rulebook v9 spec
- Walker purity would be violated
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
