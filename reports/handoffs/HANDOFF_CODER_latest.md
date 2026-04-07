# AGT Equities — Coder (Claude Code) Handoff

**Last updated:** 2026-04-07
**Status:** Phase 3A.5c2-α COMPLETE. All 17 tasks shipped. Live IBKR verified.
**Tests:** 327/327 passing. Runtime: ~24s.
**Next:** Pre-β checklist, then Phase 3A.5c2-β (Smart Friction UI).

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
9. **Invariant deviations require STOP-and-surface BEFORE shipping, not after.** If during execution you encounter a locked invariant that you believe is incorrect, halt, surface with reasoning, wait for greenlight. This applies even when the deviation is obviously correct.

---

## Working Directory

`C:\AGT_Telegram_Bridge\`

**Key files you touch most:**
- `telegram_bot.py` — main bot entry, ~9700 lines (pruning target ~4500 in Phase 3D)
- `agt_equities/walker.py` — pure function, source of truth for cycles
- `agt_equities/rule_engine.py` — rule evaluators (R1/R2/R3/R4/R5/R6/R7/R9/R11 real, R8/R10 PENDING stubs)
- `agt_equities/mode_engine.py` — 3-mode state machine + LeverageHysteresisTracker + glide path math
- `agt_equities/seed_baselines.py` — glide path + sector override + initial mode seed data
- `agt_equities/flex_sync.py` — EOD master log writer + walker warnings persist + desk_state regen + git auto-push
- `agt_equities/schema.py` — all SQLite migrations, idempotent DDL
- `agt_equities/data_provider.py` — DEPRECATED IBKRProvider + MarketDataProvider ABC (retained for state_builder)
- `agt_equities/market_data_interfaces.py` — 4-way ISP ABCs (IPriceAndVolatility, IOptionsChain, ICorporateIntelligence)
- `agt_equities/providers/` — ibkr_price_volatility.py, ibkr_options_chain.py, yfinance_corporate_intelligence.py
- `agt_equities/state_builder.py` — upstream populator for PortfolioState (correlation matrix, EL snapshots, NLV)
- `agt_deck/main.py` — FastAPI Command Deck + Cure Console routes
- `agt_deck/risk.py` — leverage, EL, concentration, sector helpers
- `agt_deck/queries.py` — DB read layer
- `agt_deck/desk_state_writer.py` — generates desk_state.md (atomic write)
- `agt_deck/templates/cure_console.html` + `cure_partial.html` — Cure Console UI
- `agt_deck/templates/command_deck.html` — main deck with Mode Badge + Lev links
- `tests/test_walker.py` — 91 walker unit + W3.6 tests
- `tests/property/test_walker_properties.py` — 23 Hypothesis property tests
- `tests/test_phase3a.py` — 65 Phase 3A tests (rule engine + mode engine + glide paths + gates + R7)
- `tests/test_phase3a5a.py` — 63 Phase 3A.5a tests (R4 correlation, R5 sell gate, R6 refinement, data provider, tolerance band)
- `tests/test_rule_9.py` — 20 Phase 3A.5b tests (R9 Red Alert compositor, hysteresis, composition logic)
- `tests/test_phase3a5c2_alpha.py` — 36 Phase 3A.5c2-α tests (Gate 1/2, orchestrator, sweeper, STK_SELL)
- `tests/test_providers.py` — 18 provider tests (IBKRPriceVol, OptionsChain, YFinance, NLV, deprecation)
- `tests/test_market_data_dtos.py` — 10 DTO tests
- `scripts/archive_handoffs.py` — Friday handoff archiver

---

## Architecture Reminders

**3-Bucket data model (LOCKED):**
1. Real-time API — TWS via ib_async, no persistence
2. `master_log_*` tables (12 tables) — immutable, only `flex_sync.py` writes
3. Operational state — everything else (pending_orders, glide_paths, mode_history, el_snapshots, sector_overrides, walker_warnings_log, bucket3_dynamic_exit_log, bucket3_earnings_overrides, etc.)

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

**Dynamic Exit Pipeline (Task 6, α):**
```
_stage_dynamic_exit_candidate(ticker, hh_name, hh_data, position, source)
  → conviction lookup → escalation → overweight scope → Gate 1 chain walk
  → INSERT bucket3_dynamic_exit_log (final_status='STAGED', source=<caller>)
  → 15-min TTL → sweep_stale_dynamic_exit_stages() → ABANDONED
```
Sources: `scheduled_watchdog`, `manual_inspection`, `cc_overweight`.
Consumer surface (Smart Friction UI) ships in Phase β.

**`_run_cc_logic()` return contract:** `{"main_text": str}` — single key only (Task 11 removed `cio_payload` and `exit_commands`).

---

## Current State

- **Tests:** 327/327 (91 walker + 23 property + 65 phase3a + 63 phase3a5a + 21 rule_9 + 10 dto + 18 providers + 36 phase3a5c2_alpha)
- **Mode:** PEACETIME (verified on live IBKR, Task 16)
- **Walker:** fully closed through W3.8 + W3.6 (WalkerWarning dataclass + UI) + W3.7 (18 Hypothesis properties)
- **Cure Console:** live at `/cure`, mobile-responsive, HTMX 60s refresh, Tailscale-exposed at `0.0.0.0:8787`
- **Mode Badge:** live on Command Deck + Cure Console top strip, clickable to `/cure`
- **Lev cells:** linkified to `/cure` on Command Deck
- **Telegram commands:** `/declare_wartime <reason>`, `/declare_peacetime <memo>`, `/mode`, `/cure`, `/cc`, `/scan`, `/dynamic_exit`, `/override`, `/override_earnings`
- **Push alerts:** mode transitions → emoji-coded Telegram message to AUTHORIZED_USER_ID
- **GitLab:** `git@gitlab.com:agt-group2/agt-equities-desk.git` (SSH `@yashpatil1`), daily auto-push
- **Litestream:** continuous DB replication to Cloudflare R2
- **IBKR accounts:** U21971297 (Yash Individual), U22076329 (Yash Roth IRA), U22388499 (Vikram). U22076184 (Yash Trad IRA) dormant/closed — cleanup grep pending pre-β.

**Household state (2026-04-06 Flex):**

| Household | NAV | Leverage | Top concentration |
|-----------|-----|----------|-------------------|
| Yash | $261,902 | 1.59x (BREACHED) | ADBE 46.7% |
| Vikram | $80,787 | 2.15x (BREACHED) | ADBE 60.5% |

**Glide paths seeded:** 10 rows (2 leverage, 8 concentration). PYPL paused (earnings-gated).
**Sector override:** UBER → Consumer Cyclical (fixes SW-App 3→2 violation).
**Earnings overrides:** 10 active (all held tickers, set during Task 16 verification, 7-day TTL).

---

## Completed Work (α surgery sprint, this session)

| Task | SHA | Tests | Report |
|------|-----|-------|--------|
| Task 10: IBKRProvider DeprecationWarning | `46b43d4` | 320 (+1) | `reports/task_10_closeout_20260407.md` |
| Task 6: Watchdog → STAGED rows | `b7ea360` | 320 (+0) | `reports/task_6_implementation_20260407.md` |
| Task 11+9: /exit removal + R7 fail-closed | `9c60d3f` | 327 (+7) | `reports/task_11_implementation_20260407.md`, `reports/task_9_implementation_20260407.md` |
| Fix: yf_tkr initialization | `85b24a6` | 327 (+0) | In Task 16 verification report |
| Task 15: Test audit | (no commit) | 327 | `reports/task_15_test_audit_20260407.md` |
| Task 16: Live IBKR verification | (no commit) | 327 | `reports/task_16_verification_20260407.md` |
| Task 17: Final report | `ec0ea3a` | 327 | `reports/phase_3a_5c2_alpha_complete_20260407.md` |

**Prior session (pre-compaction, 13 tasks):**

| Task | Tests | Report |
|------|-------|--------|
| W3.6: Walker Warnings UI | 91 (+14) | `reports/w3_6_implementation_20260407.md` |
| W3.7: Hypothesis Property Tests | 114 (+23) | `reports/w3_7_implementation_20260407.md` |
| Phase 3A Stages 1-3 | 170 | `reports/phase_3a_stage3_20260407.md` |
| Phase 3A.5a: R4/R5/R6 + data provider | 235 (+65) | `reports/phase_3a_5a_final_20260407.md` |
| Phase 3A.5a triage: R2 denominator + glide tolerance | 235 | `reports/phase_3a_5a_triage_20260407.md` |
| Phase 3A.5b: R9 Red Alert compositor | 255 (+20) | `reports/phase_3a_5b_implementation_20260407.md` |
| Phase 3A.5c1: Data layer (DTOs, ABCs, providers) | 282 (+27) | `reports/phase_3a_5c1_implementation_20260407.md` |
| Phase 3A.5c2-alpha: R8 backend (partial) | 319 (+37) | `reports/phase_3a_5c2_alpha_implementation_20260407.md` |

---

## In Flight

**Phase 3A.5c2-α: COMPLETE.**

**Pre-β checklist (before any β code):**
1. v10 upload
2. `rulebook_llm_condensed.md` refresh
3. Bot restart with committed code on production machine
4. 3-account cleanup grep (remove dormant U22076184 references)
5. `HANDOFF_ARCHITECT_v3.md` + this file refresh

**Phase 3A.5c2-β: Smart Friction UI** (next phase)
- Cure Console Dynamic Exit panel template
- Smart Friction widget (PEACETIME checkbox + WARTIME Integer Lock)
- Telegram [TRANSMIT] [CANCEL] inline keyboard handler
- JIT re-validation at TRANSMIT
- 3-strike retry budget + 5-minute ticker lock
- `/sell_shares` command
- Adaptive thesis prompt (CIO Oracle replacement)

β v0 draft does NOT exist as a standalone file. Scope is across: `HANDOFF_CODER_latest.md` (this section), `reports/phase_3a_5c2_discovery_20260407.md` Sections 3-11, `ADR-004`.

---

## Phase Backlog (do NOT start without Architect prompt)

- **Phase 3B:** desk_state.md full integration (5-min APScheduler, EL snapshots from IBKR live API, automated mode pipeline, R7 earnings cache scheduled job)
- **Phase 3C:** LLM CIO Oracle advisory injection above Smart Friction widget (additive, not destructive)
- **Phase 3D:** Telegram pruning to ~4500 lines, kill display commands, replace text approvals with inline buttons
- **Phase 3E:** Mobile responsive polish

---

## Active Gotchas / Don't-Touch List

1. **R8 stub** — returns PENDING (gray pill). Real R8 infrastructure (Gate 1/2, orchestrator, campaigns) lives alongside but is reporting-only via STAGED rows. No execution path until β ships Smart Friction UI.
2. **R9 (Red Alert compositor)** — REAL evaluator (Phase 3A.5b). Post-softening compositor. 4-condition (A/B/C/D), 2-of-4 fire, all-4 clear (asymmetric hysteresis). REPORTING ONLY — does NOT trigger mode transitions. See ADR-003.
3. **R7 (Earnings Window)** — REAL evaluator (Task 9). FAIL-CLOSED: missing/stale data → RED. Override via `/override_earnings TICKER YYYY-MM-DD [TTL_HOURS] [reason]`. Override is per-ticker GLOBAL, max 720h TTL. No scheduled cache yet — all R7 GREEN requires manual override. Phase 3B scope: wire YFinance cache into daily job.
4. **R4 (correlation)** — REAL evaluator (Phase 3A.5a). Reads `ps.correlations`. ADBE-CRM 0.69 raw RED, glide-pathed to GREEN.
5. **R5 (sell gate)** — REAL evaluator (Phase 3A.5a). Status grid = GREEN always. Real gate via `evaluate_rule_5_sell_gate()`.
6. **R6 (Vikram EL)** — REAL evaluator (Phase 3A.5a). 4-tier: GREEN >=25%, AMBER 20-25%, RED 10-20%, RED+CRITICAL <10%.
7. **IBKRProvider DEPRECATED** — DeprecationWarning fires on `__init__()`. New code must use 4-way ISP providers. `MarketDataProvider` ABC retained for `state_builder.py`. Deletion scheduled Phase 3B.
8. **EL data source** — IBKR live `ExcessLiquidity` via IBKRProvider `get_account_summary()`. `el_snapshots` table (Bucket 3) writer not yet wired (Phase 3B).
9. **`gross_beta_leverage()`** — impure, has module-level hysteresis dict. Use `compute_leverage_pure()` from rule_engine.
10. **UBER sector** — `sector_overrides` table, manual override to Consumer Cyclical.
11. **`/cc` AMBER behavior** — allowed (exits/rolls), only block in WARTIME.
12. **`/declare_wartime` and `/declare_peacetime`** — both require reason/memo (mandatory audit trail).
13. **`/exit_math`** — never implemented (W3.8 deferred), merging into Cure Console in Phase 3C. `/exit` command DELETED in Task 11.
14. **CORP_ACTION handler** — synthetic-tested only, Flex shape verification needed on first real one.
15. **ADBE/PYPL Dynamic Exits** — Yash handles personally, don't touch.
16. **Git auto-push hook** — wrapped in try/except, never blocks flex_sync. Don't break this.
17. **Hardcoded account numbers** in queries.py, main.py — routing keys, not secrets, OK to commit. U22076184 dormant — cleanup grep pending pre-β.
18. **Production DB is `agt_desk.db`** — the `agt_equities/` package name does NOT match the DB filename. Never `sqlite3.connect("agt_equities.db")`.
19. **Prompt worked-examples are illustrative** — Ground-truth inputs ALWAYS come from `master_log_*` tables or live IBKR calls. If a prompt number disagrees with the authoritative data source, STOP and report.
20. **R2 denominator = margin-eligible NLV only (Reading 2)** — per v9. Excludes Roth IRA. Yash margin = [U21971297]. Vikram = [U22388499]. See ADR-001.
21. **R11 denominator = all-account NLV** — intentional, different from R2.
22. **R2 glide paths DECOUPLED from R11** — R11 cures fast, R2 cures slow. Yash R2 42.1%, Vikram R2 54.2% are expected.
23. **Glide path tolerance band** — per-rule flat absolute tolerance. 1pp ratio, 2bp leverage/correlation. See ADR-002.
24. **Rule 10 sector exclusions** — R3 excludes legacy picks (SLS, GTLB), SPX box spreads, negligible holdings.
25. **R9 reads SOFTENED statuses** — after glide path softening, not raw. See ADR-003.
26. **R9 is REPORTING-ONLY** — does NOT trigger automatic mode transitions.
27. **4-way ABC split (3A.5c1)** — IPriceAndVolatility, IOptionsChain, ICorporateIntelligence, IAccountState (state_builder). New ABCs at `agt_equities/market_data_interfaces.py`, implementations at `agt_equities/providers/`.
28. **Stock spot via modelGreeks.undPrice (10089 workaround)** — fallback to reqHistoricalData last close.
29. **IV rank ETA April 2027** — 252 trading days to bootstrap. iv_rank=None until then.
30. **yfinance is COLD PATH only** — NEVER in hot paths. 24h TTL cache.
31. **jobs/eod_macro_sync.py is standalone** — NOT inside flex_sync.py. Windows Task Scheduler 5:00 PM.
32. **walker.compute_walk_away_pnl() is single source of truth** — extracted from inline implementations in α.
33. **Gate 1 conviction modifier is HARDCODED** — HIGH=0.20, NEUTRAL=0.30, LOW=0.40.
34. **bucket3_dynamic_exit_log is BOTH staging queue AND audit log** — lifecycle via final_status column. Permanent retention for Act 60 compliance. `source` column tracks origin (`scheduled_watchdog`, `manual_inspection`, `cc_overweight`, `manual_stage`).
35. **`_run_cc_logic()` returns `{"main_text": str}` only** — `cio_payload` and `exit_commands` keys removed in Task 11. Do NOT add them back.
36. **`_stage_dynamic_exit_candidate()` requires `yf.Ticker(ticker)` before chain walk** — Bug fix `85b24a6`. Do not remove the `yf_tkr = yf.Ticker(ticker)` line.

---

## Backup System

- **Code -> GitLab** `git@gitlab.com:agt-group2/agt-equities-desk.git` (SSH `@yashpatil1`)
- **Auto-push** at end of every successful `flex_sync.py` run
- **DB -> Cloudflare R2** via Litestream, continuous, 30-day retention
- **Friday handoff archive** via `scripts/archive_handoffs.py`
- **Rollback tag:** `task16-pre-verification` at `9c60d3f` (pre-yf_tkr-fix point)
- **NEVER commit:** `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `audit_bundles/`, `data/inception_carryin.csv`, `Archive/`, `.venv/`, `.hypothesis/`, `.claude/`, Litestream WAL segments

---

## Hard Stops

Always stop and report (do NOT auto-fix) on:
- Any rule disagrees with Rulebook v9/v10 spec (including denominator semantics)
- Walker purity would be violated
- Bucket 2 would be written by anything other than `flex_sync.py`
- Day 1 baseline computes AMBER or RED
- A fix might introduce new bugs (say so explicitly)
- Audit narrowing — do not reduce scope to protect prior work
- Secret found in staged files
- SSH/git auth fails
- **Invariant deviation discovered during execution** — STOP and surface BEFORE shipping

---

## How to Pick Up (new session ritual)

1. Read this file end-to-end.
2. Read `desk_state.md` at `C:\AGT_Telegram_Bridge\desk_state.md`.
3. Read `reports/phase_3a_5c2_alpha_complete_20260407.md` for α close-out state.
4. Wait for Architect prompt. Do not start work autonomously.

End of handoff.
