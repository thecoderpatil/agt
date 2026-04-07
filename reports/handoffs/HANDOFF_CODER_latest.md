# AGT Equities — Coder (Claude Code) Handoff

**Last updated:** 2026-04-07
**Status:** Phase 3A.5c2-alpha partial. Surgery tasks (6,9,10,11) deferred to next session.
**Tests:** 319/319 passing. Runtime: ~17s.

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
- `tests/test_phase3a5a.py` — 63 Phase 3A.5a tests (R4 correlation, R5 sell gate, R6 refinement, data provider, tolerance band)
- `tests/test_rule_9.py` — 20 Phase 3A.5b tests (R9 Red Alert compositor, hysteresis, composition logic)
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

- **Tests:** 319/319 (91 walker + 23 property + 58 phase3a + 63 phase3a5a + 21 rule_9 + 10 dto + 17 providers + 36 phase3a5c2_alpha)
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
| Phase 3A Stages 1-3 | 170 | `reports/phase_3a_stage3_20260407.md` |
| Phase 3A.5a: R4/R5/R6 + data provider | 235 (+65) | `reports/phase_3a_5a_final_20260407.md` |
| Phase 3A.5a triage: R2 denominator + glide tolerance | 235 | `reports/phase_3a_5a_triage_20260407.md` |
| Phase 3A.5b: R9 Red Alert compositor | 255 (+20) | `reports/phase_3a_5b_implementation_20260407.md` |
| Phase 3A.5c1: Data layer (DTOs, ABCs, providers) | 282 (+27) | `reports/phase_3a_5c1_implementation_20260407.md` |
| Phase 3A.5c2-alpha: R8 backend (partial) | 319 (+37) | `reports/phase_3a_5c2_alpha_implementation_20260407.md` |

---

## In Flight

**Phase 3A.5c2-alpha: PARTIAL — surgery tasks deferred**
Shipped: walker extraction, schemas (3), Gate 1/2, orchestrator, R9 condition D (2-of-4), R5 stage helper, sweeper, is_wartime helper, v10 changelog.
Deferred to next session (telegram_bot.py surgery with commit checkpoints):
- Task 6: Watchdog CIO payload refactor (~120 lines removed, STAGED row writes)
- Task 9: R7 earnings fail-closed + /override_earnings command
- Task 10: IBKRProvider migration to 4-way ABCs
- Task 11: /exit command removal (BLOCKED by Task 6 — hidden coupling at line 9402)

**Phase 3A.5c2-beta: Smart Friction UI** (after alpha surgery completes)
- Cure Console Dynamic Exit panel template
- Smart Friction widget (PEACETIME checkbox + WARTIME Integer Lock)
- Telegram [TRANSMIT] [CANCEL] inline keyboard handler
- JIT re-validation at TRANSMIT
- 3-strike retry budget + 5-minute ticker lock
- /sell_shares command

---

## Phase Backlog (do NOT start without Architect prompt)

- **Phase 3B:** desk_state.md full integration (5-min APScheduler, EL snapshots from IBKR live API, automated mode pipeline)
- **Phase 3C:** LLM CIO Oracle advisory injection above Smart Friction widget (additive, not destructive)
- **Phase 3D:** Telegram pruning to ~4500 lines, kill display commands, replace text approvals with inline buttons
- **Phase 3E:** Mobile responsive polish

---

## Active Gotchas / Don't-Touch List

1. **R8 stub** — returns PENDING (gray pill), do not implement until Phase 3A.5c
1z. **R9 (Red Alert compositor)** — REAL evaluator (Phase 3A.5b). Post-softening compositor reading R1/R2/R6 softened statuses. Reads SOFTENED statuses, not raw — a rule on an on-track glide path is NOT in violation. 3-condition (A/B/C), condition D deferred to 3A.5c. 2-of-3 fire, all-3 clear (asymmetric hysteresis). Persists to `red_alert_state` table. REPORTING ONLY — does NOT trigger mode transitions. See ADR-003.
1a. **R4 (correlation)** — REAL evaluator (Phase 3A.5a). Reads `ps.correlations`. ADBE-CRM 0.69 raw RED, glide-pathed to GREEN (20w linked to ADBE concentration glide).
1b. **R5 (sell gate)** — REAL evaluator (Phase 3A.5a). Status grid = GREEN always. Real gate via `evaluate_rule_5_sell_gate()` — NOT wired to commands yet (Phase 3A.5b/c).
1c. **R6 (Vikram EL)** — REAL evaluator (Phase 3A.5a). 4-tier: GREEN >=25%, AMBER 20-25%, RED 10-20%, RED+CRITICAL <10%. R6 <10% returns `detail["severity"]="CRITICAL"` — consumers checking Rule 5 override read this field, not the status enum.
1d. **Data provider** — `agt_equities/data_provider.py`. IBKRProvider (2 real methods: `get_historical_daily_bars`, `get_account_summary`; 3 stubs: option_chain, fundamentals, earnings_date -> NotImplementedError, Phase 3A.5c). State builder at `agt_equities/state_builder.py`.
2. **EL data source** — IBKR live `ExcessLiquidity` via IBKRProvider `get_account_summary()`. `el_snapshots` table (Bucket 3) writer not yet wired (Phase 3B).
3. **`gross_beta_leverage()`** — impure, has module-level hysteresis dict. Use `compute_leverage_pure()` from rule_engine
4. **UBER sector** — `sector_overrides` table, manual override to Consumer Cyclical
5. **`/cc` AMBER behavior** — allowed (exits/rolls), only block in WARTIME
6. **`/declare_wartime` and `/declare_peacetime`** — both require reason/memo (mandatory audit trail)
7. **`/exit_math`** — never implemented (W3.8 deferred), merging into Cure Console in Phase 3C
8. **CORP_ACTION handler** — synthetic-tested only, Flex shape verification needed on first real one
9. **ADBE/PYPL Dynamic Exits** — Yash handles personally, don't touch
10. **Git auto-push hook** — wrapped in try/except, never blocks flex_sync. Don't break this.
11. **Hardcoded account numbers** in queries.py, main.py — routing keys, not secrets, OK to commit
12. **Production DB is `agt_desk.db`** — the `agt_equities/` package name does NOT match the DB filename. Never `sqlite3.connect("agt_equities.db")` — that creates an empty file via SQLite's auto-create behavior.
13. **Prompt worked-examples are illustrative** — Ground-truth quantitative inputs ALWAYS come from `master_log_*` tables or live IBKR calls, never from the Architect prompt body. If a prompt number disagrees with the authoritative data source, STOP and report the divergence. Phase 3A.5a Day 1 baseline failed because a 4400-share illustrative example was treated as ground truth instead of pulling 500 shares from `master_log_open_positions`.
14. **R2 denominator = margin-eligible NLV only (Reading 2)** — per v9 lines 709-712. Excludes Roth IRA NLV from denominator (not just from EL numerator). Yash margin accounts = [U21971297] (Individual only). Vikram = [U22388499] (single account). Do NOT use `household.total_nlv` for R2. See ADR-001.
15. **R11 denominator = all-account NLV** — per v9 Definitions. This is intentional, not a bug. R2 (deployment governor) and R11 (portfolio leverage cap) use different denominators on purpose. Logged for v10 stress audit review. Do NOT change without rulebook amendment.
16. **R2 glide paths DECOUPLED from R11** — R11 cures fast (assignment-driven exposure reduction). R2 cures slow (cash accumulation requiring reduced redeployment velocity). Yash R2 baseline 42.1% and Vikram R2 baseline 54.2% are both EXPECTED conditions, not alarms. Both cure to 70% by end of Q4 2026 (38 weeks). Accelerator clause: fundamentals deterioration triggers Rule 5 thesis-deterioration compressed cure path.
17. **Glide path tolerance band** — `evaluate_glide_path()` applies per-rule flat absolute tolerance to BOTH worsened (RED) and behind (AMBER) checks. 1pp on ratio rules (R1/R2/R6), 2bp on leverage (R11), 2bp on correlation (R4). Sub-tolerance drift is NOT a mode transition. Do NOT widen to mask real movement. See ADR-002.
18. **Rule 10 sector exclusions** — R3 excludes legacy picks (SLS, GTLB), SPX box spreads, and negligible holdings (IBKR fractional, TRAW.CVR) from sector counts per v9 lines 502-514. R4 similarly excludes via `CORRELATION_EXCLUDED_TICKERS`.
19. **R9 reads SOFTENED statuses** — R9 (Red Alert compositor) reads RuleResult.status AFTER glide path softening, NOT raw evaluator output. A rule on an on-track glide path is by design NOT in violation. R9 fires only on real deviations from intended posture. See ADR-003.
20. **R9 is REPORTING-ONLY in 3A.5b** — R9 computes Red Alert status and persists to `red_alert_state` table, but does NOT trigger automatic mode transitions. Manual `/declare_wartime` remains the only WARTIME path until Phase 3B automated pipeline lands. See ADR-003.
21. **R9 condition D deferred** — R9 condition D (all-positions-Mode-1) is DEFERRED to Phase 3A.5c pending `IBKRProvider.get_option_chain()`. R9 currently fires on 2-of-3 conditions (A/B/C). When condition D lands, threshold becomes 2-of-4 per v9 spec.
22. **4-way ABC split for market data (3A.5c1)** — IPriceAndVolatility, IOptionsChain, ICorporateIntelligence, IAccountState. Account state owned by state_builder.py (NOT a separate provider class). Market data providers MUST NOT bleed PortfolioState into their interfaces. New ABCs at `agt_equities/market_data_interfaces.py`, implementations at `agt_equities/providers/`.
23. **Stock spot via modelGreeks.undPrice (10089 workaround)** — Default IBKR plan returns error 10089 on streaming stock reqMktData. Workaround: extract spot from option modelGreeks.undPrice (~80% hit rate) or fallback to reqHistoricalData last close (always marked is_extrinsic_stale). Monitor extrinsic_fallback_rate.
24. **IV rank operational ETA April 2027** — bucket3_macro_iv_history starts populating on first run of `jobs/eod_macro_sync.py`. 252 trading days to bootstrap. iv_rank=None until then. /scan must handle gracefully.
25. **yfinance is COLD PATH only** — NEVER in /cc, /health, /scan hot paths. ICorporateIntelligence wraps yfinance with 24h TTL cache. All calls marked `# DEPLOYMENT: replace with paid feed`.
26. **jobs/eod_macro_sync.py is standalone** — NOT inside flex_sync.py. Windows Task Scheduler at 5:00 PM AST daily. Failure must NOT block flex_sync.
27. **walker.compute_walk_away_pnl() is the single source of truth** — Extracted from 2 inline telegram_bot.py implementations in 3A.5c2-alpha. Line 2967 wrapper delegates to walker. Lines 7590 and 9019 will migrate when Task 6 (watchdog refactor) ships. Do NOT add new inline walk-away computations.
28. **Gate 1 conviction modifier is HARDCODED** — HIGH=0.20, NEUTRAL=0.30, LOW=0.40. Do NOT query live VRP or /scan output. The modifier IS the yield proxy per v10 + Gemini Q1. See `CONVICTION_MODIFIERS` in rule_engine.py.
29. **/exit command has hidden coupling** — Line 9402 in `_run_cc_logic()` parses `/exit` commands from dynamic exit payloads. /exit removal MUST follow watchdog refactor (Task 6). Do not remove independently.
30. **R8 stub remains PENDING in evaluate_all()** — The R8 evaluator stub returns PENDING. The real R8 infrastructure (Gate 1/2, orchestrator, campaigns) lives alongside but is reporting-only via STAGED rows in bucket3_dynamic_exit_log. No execution path exists until beta ships the Smart Friction UI + Telegram JIT handler.
31. **bucket3_dynamic_exit_log is BOTH staging queue AND audit log** — Per Patch 1, there is NO separate staging table. Lifecycle managed via final_status column: PENDING -> STAGED -> ATTESTED -> TRANSMITTED -> FILLED (or ABANDONED/DRIFT_BLOCKED/CANCELLED). Permanent retention for Act 60 compliance.

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
- Any rule disagrees with Rulebook v9 spec (including denominator semantics, not just tier values — R2 Reading 1 vs Reading 2 was a denominator bug not caught by tier verification alone)
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
