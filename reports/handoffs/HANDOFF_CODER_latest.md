# AGT Equities — Coder (Claude Code) Handoff

**Last updated:** 2026-04-08
**Status:** Phase 3A.5c2-β IN PROGRESS. β Impls 1–5 shipped. Impl 6 (AMBER tests) survey complete — test-only pass.
**Tests:** 367/367 passing. Runtime: ~25s.
**Next:** β Impl 6 (AMBER test-only pass), then β Impl 3 Tier 1 fix sprint commit, then live verification.

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
- `telegram_bot.py` — main bot entry, ~10,800 lines (pruning target ~4500 in Phase 3D)
- `agt_equities/walker.py` — pure function, source of truth for cycles
- `agt_equities/rule_engine.py` — rule evaluators (R1/R2/R3/R4/R5/R6/R7/R9/R11 real, R8/R10 PENDING stubs) + Gate 1/2, sweeper, is_ticker_locked, stage_stock_sale_via_smart_friction
- `agt_equities/mode_engine.py` — 3-mode state machine + LeverageHysteresisTracker + glide path math
- `agt_equities/seed_baselines.py` — glide path + sector override + initial mode seed data
- `agt_equities/flex_sync.py` — EOD master log writer + walker warnings persist + desk_state regen + git auto-push
- `agt_equities/schema.py` — all SQLite migrations, idempotent DDL (incl. TRANSMITTING migration + exception_type ALTER)
- `agt_equities/data_provider.py` — DEPRECATED IBKRProvider + MarketDataProvider ABC (retained for state_builder)
- `agt_equities/market_data_interfaces.py` — 4-way ISP ABCs (IPriceAndVolatility, IOptionsChain, ICorporateIntelligence)
- `agt_equities/providers/` — ibkr_price_volatility.py, ibkr_options_chain.py, yfinance_corporate_intelligence.py
- `agt_equities/state_builder.py` — upstream populator for PortfolioState (correlation matrix, EL snapshots, NLV)
- `agt_deck/main.py` — FastAPI Command Deck + Cure Console routes + Smart Friction POST + R5 staging route
- `agt_deck/risk.py` — leverage, EL, concentration, sector helpers
- `agt_deck/queries.py` — DB read layer (get_staged_dynamic_exits includes CC + STK_SELL + exception_type)
- `agt_deck/desk_state_writer.py` — generates desk_state.md (atomic write)
- `agt_deck/templates/cure_console.html` + `cure_partial.html` — Cure Console UI
- `agt_deck/templates/cure_dynamic_exit_panel.html` — Dynamic Exit & R5 panel (CC + STK_SELL column branching)
- `agt_deck/templates/cure_smart_friction.html` — Smart Friction modal (R8 CC + R5 4-exception branching)
- `agt_deck/templates/command_deck.html` — main deck with Mode Badge + Lev links
- `tests/test_walker.py` — 91 walker unit + W3.6 tests
- `tests/property/test_walker_properties.py` — 23 Hypothesis property tests
- `tests/test_phase3a.py` — 65 Phase 3A tests
- `tests/test_phase3a5a.py` — 63 Phase 3A.5a tests
- `tests/test_rule_9.py` — 20 Phase 3A.5b tests
- `tests/test_phase3a5c2_alpha.py` — 36 Phase 3A.5c2-α tests (Gate 1/2, sweeper, STK_SELL + exception_type fixture)
- `tests/test_providers.py` — 18 provider tests
- `tests/test_market_data_dtos.py` — 10 DTO tests
- `tests/test_phase3a5c2_beta_impl3.py` — 23 β Impl 3 tests (JIT chain, TRANSMIT/CANCEL, drift, 3-strike, sweeper ATTESTED TTL, counter isolation, poller dedup, IB error, F1-F9 fixes)
- `tests/test_phase3a5c2_beta_impl5.py` — 17 β Impl 5 tests (exception_type migration/persistence, R5 gate flows, JIT reuse, poller STK_SELL rendering)
- `scripts/archive_handoffs.py` — Friday handoff archiver

---

## Architecture Reminders

**3-Bucket data model (LOCKED):**
1. Real-time API — TWS via ib_async, no persistence
2. `master_log_*` tables (12 tables) — immutable, only `flex_sync.py` writes
3. Operational state — everything else (pending_orders, glide_paths, mode_history, el_snapshots, sector_overrides, walker_warnings_log, bucket3_dynamic_exit_log, bucket3_earnings_overrides, etc.)

**3-mode state machine:**
- PEACETIME (all glide paths GREEN) -> normal ops
- AMBER (any glide path AMBER) -> block `/scan` and new CSP entries; `/cc` exits/rolls allowed; Smart Friction uses PEACETIME checkboxes+thesis flow (NOT Integer Lock); Gate 2 sizing 25% (not 33%)
- WARTIME (any glide path RED) -> block `/scan` AND `/cc`; Cure Console only; mandatory audit memo to revert; Smart Friction uses Integer Lock; 3-strike and ticker lockout bypassed for TRANSMIT

**Mode gates in telegram_bot.py:**
- `/scan`: `_check_mode_gate("PEACETIME")` — blocked in AMBER + WARTIME
- `/cc`: `_check_mode_gate("AMBER")` — blocked in WARTIME only
- Existing Rule 11 leverage check preserved post-gate (defense in depth)

**Mode source:** `mode_history` TABLE via `_get_current_desk_mode()` → direct SQLite query. NOT from `desk_state.md`. Zero staleness.

**Walker is a PURE FUNCTION.** Never mutate it. Wrap impure helpers in pure interfaces (see `compute_leverage_pure()` pattern).

**Dynamic Exit Pipeline (α + β):**
```
_stage_dynamic_exit_candidate(ticker, hh_name, hh_data, position, source)
  → conviction lookup → escalation → overweight scope → Gate 1 chain walk
  → INSERT bucket3_dynamic_exit_log (final_status='STAGED', source=<caller>)
  → 15-min TTL → sweep_stale_dynamic_exit_stages() → ABANDONED
  → Cure Console Dynamic Exit panel renders STAGED rows
  → "Begin Attestation" button → Smart Friction modal GET (2a)
  → Operator completes attestation → Smart Friction POST (2b) → ATTESTED
  → 10s poller pushes Telegram [TRANSMIT] [CANCEL] keyboard
  → TRANSMIT: 9-step JIT chain → ATTESTED→TRANSMITTING→TRANSMITTED
  → CANCEL: ATTESTED→CANCELLED (terminal)
  → 60s sweeper: stale STAGED→ABANDONED (15min), stale ATTESTED→ABANDONED (10min)
```

**R5 Sell Gate Pipeline (β Impl 5):**
```
POST /api/cure/r5_sell/stage → stage_stock_sale_via_smart_friction()
  → R5 sell gate (evaluate_rule_5_sell_gate) per exception_type
  → INSERT bucket3_dynamic_exit_log (action_type='STK_SELL', exception_type=<enum>)
  → Same STAGED→ATTESTED→TRANSMITTED lifecycle as R8
  → JIT chain: STK_SELL skips Gate 1 at Step 5a, drift uses 0.5% relative threshold
```

**`_run_cc_logic()` return contract:** `{"main_text": str}` — single key only (Task 11 removed `cio_payload` and `exit_commands`).

---

## Current State

- **Tests:** 367/367 (91 walker + 23 property + 65 phase3a + 63 phase3a5a + 20 rule_9 + 10 dto + 18 providers + 36 phase3a5c2_alpha + 23 beta_impl3 + 17 beta_impl5 + 1 rule_9_day1)
- **Mode:** PEACETIME (verified on live IBKR, Task 16)
- **Walker:** fully closed through W3.8 + W3.6 (WalkerWarning dataclass + UI) + W3.7 (18 Hypothesis properties)
- **Cure Console:** live at `/cure`, mobile-responsive, HTMX 60s refresh, Tailscale-exposed at `0.0.0.0:8787`. Dynamic Exit & R5 Sell panel displays both CC and STK_SELL STAGED rows with action_type + exception_type badges.
- **Smart Friction:** Polymorphic across R8 CC (PEACETIME checkboxes+thesis, WARTIME Integer Lock) and R5 STK_SELL (4 sub-flows: Thesis Deterioration thesis, Forced Liquidation Integer Lock, Emergency Risk catalyst, Rule 8 Dynamic Exit).
- **Telegram TRANSMIT/CANCEL:** 9-step JIT re-validation chain. 10s poller delivers keyboards. 3-strike budget with WARTIME bypass. 0.5% relative drift for STK_SELL, $0.10 absolute for CC. TRANSMITTING intermediate state with no auto-revert on IB error.
- **Sweeper:** Runs continuously (60s) for both STAGED (15min TTL) and ATTESTED (10min R7 TTL).
- **Mode Badge:** live on Command Deck + Cure Console top strip, clickable to `/cure`
- **Telegram commands:** `/declare_wartime <reason>`, `/declare_peacetime <memo>`, `/mode`, `/cure`, `/cc`, `/scan`, `/dynamic_exit`, `/override`, `/override_earnings`
- **Push alerts:** mode transitions → emoji-coded Telegram message to AUTHORIZED_USER_ID
- **GitLab:** `git@gitlab.com:agt-group2/agt-equities-desk.git` (SSH `@yashpatil1`), daily auto-push
- **Litestream:** continuous DB replication to Cloudflare R2
- **IBKR accounts:** U21971297 (Yash Individual), U22076329 (Yash Roth IRA), U22388499 (Vikram). U22076184 (Yash Trad IRA) dormant.

**Household state (2026-04-06 Flex):**

| Household | NAV | Leverage | Top concentration |
|-----------|-----|----------|-------------------|
| Yash | $261,902 | 1.59x (BREACHED) | ADBE 46.7% |
| Vikram | $80,787 | 2.15x (BREACHED) | ADBE 60.5% |

**Glide paths seeded:** 10 rows (2 leverage, 8 concentration). PYPL paused (earnings-gated).
**Sector override:** UBER → Consumer Cyclical (fixes SW-App 3→2 violation).
**Earnings overrides:** 10 active (all held tickers, 7-day TTL).
**Rulebook:** v10 in force. `rulebook_llm_condensed.md` regenerated from v10 at `bb29103`.

---

## Completed Work — β Session

| Task | SHA | Tests | Notes |
|------|-----|-------|-------|
| β Impl 1: Dynamic Exit panel | `5f36e00` | 327 | Read-only STAGED row display |
| β Impl 2a: Smart Friction GET | `c9bc9d5` | 327 | Modal render |
| β Impl 2b: Smart Friction POST | `529ccbb` | 327 | STAGED→ATTESTED with mode+thesis validation |
| β Impl 2b fix: TOCTOU race | `c966060` | 327 | desk_mode race gate in attest |
| β Impl 2b fix: validation logging | `c4cb6fd` | 327 | 6 silent branches + rollback hygiene |
| β Impl 3: TRANSMIT/CANCEL + JIT | unstaged | 350 | 9-step JIT, poller, sweeper ATTESTED TTL |
| β Impl 3 Tier 1 fix sprint (F1-F9) | unstaged | 350 | Limit price spoofing, migration PRAGMA, conn leak, WARTIME lockout bypass, keyboard preservation, poller poison pill, sweeper job, STK_SELL drift |
| β Impl 5: R5 Sell Gate Surface | unstaged | 367 | exception_type column, 4-subflow widget, staging route |
| β Impl 6: AMBER survey | (no code) | 367 | Test-only pass — AMBER already correct, needs validation tests |

## Completed Work — α + pre-β (prior sessions)

| Task | SHA | Tests |
|------|-----|-------|
| Task 10: IBKRProvider DeprecationWarning | `46b43d4` | 320 |
| Task 6: Watchdog → STAGED rows | `b7ea360` | 320 |
| Task 11+9: /exit removal + R7 fail-closed | `9c60d3f` | 327 |
| Fix: yf_tkr initialization | `85b24a6` | 327 |
| α COMPLETE report | `ec0ea3a` | 327 |

---

## In Flight

**Phase 3A.5c2-α: COMPLETE** at `ec0ea3a`.

**Phase 3A.5c2-β: Smart Friction UI — 7/9 impl items shipped**

Governing doc: `reports/phase_3a_5c2_beta_v0.md`. ADR-004 binding.

| # | Item | Status |
|---|------|--------|
| 1 | Walk-away P&L delegation | ✅ `16cd244` |
| 2 | Cure Console Dynamic Exit panel | ✅ `5f36e00` |
| 3 | Smart Friction GET+POST (2a+2b) | ✅ `c9bc9d5`+`529ccbb`+`c966060`+`c4cb6fd` |
| 4 | TRANSMIT/CANCEL + JIT + poller + sweeper | ✅ unstaged (Impl 3 + F1-F9 fix sprint) |
| 5 | R5 Sell Gate operator surface | ✅ unstaged (Impl 5) |
| 6 | AMBER button semantics | ⏳ test-only pass — survey complete, no code changes needed |
| 7 | TRANSMIT handler dedicated audit | ⏳ gated on Gemini Tier 1 budget |
| 8 | End-to-end Day 1 verification | ⏳ blocked on commit + push |
| 9 | Tests target | ⏳ 367 shipped, target ~380 after Impl 6 tests |

**Unstaged work (3 impl items) must be committed before live verification.**

---

## Followups (logged, NOT in scope)

| # | Item | Filed by | Priority |
|---|------|----------|----------|
| 2 | R7 earnings cache scheduled job | α | Post-β |
| 4 | yf_tkr fix regression test | α | Post-β |
| 7 | Mobile keyboard ticker strip leniency | β Impl 2b | Post-β |
| 8 | Inline Gate 1 consolidation (telegram_bot.py:7515) | β Impl 3 | Post-β |
| 9 | F3 legacy connection leak — 63 `_get_db_connection()` sites | β Impl 3 fix sprint | Pre-live-trading |
| 10 | R5 auto-discovery pipeline | β Impl 5 survey | Post-β |
| 11 | `/sell_shares` Telegram command | β Impl 5 | Post-β |

---

## Phase Backlog (do NOT start without Architect prompt)

- **Phase 3B:** desk_state.md full integration (5-min APScheduler, EL snapshots from IBKR live API, automated mode pipeline, R7 earnings cache scheduled job)
- **Phase 3C:** LLM CIO Oracle advisory injection above Smart Friction widget (additive, not destructive)
- **Phase 3D:** Telegram pruning to ~4500 lines, kill display commands, replace text approvals with inline buttons
- **Phase 3E:** Mobile responsive polish

---

## Active Gotchas / Don't-Touch List

1. **R8 stub** — returns PENDING (gray pill). Real R8 infrastructure (Gate 1/2, orchestrator, campaigns) lives alongside but is reporting-only via STAGED rows. Execution path now live via TRANSMIT handler.
2. **R9 (Red Alert compositor)** — REAL evaluator (Phase 3A.5b). REPORTING ONLY — does NOT trigger mode transitions. See ADR-003.
3. **R7 (Earnings Window)** — REAL evaluator (Task 9). FAIL-CLOSED. Override via `/override_earnings`. No scheduled cache yet.
4. **R5 (sell gate)** — REAL evaluator + staging function. Status grid GREEN always. Gate via `evaluate_rule_5_sell_gate()`. Staging via `stage_stock_sale_via_smart_friction()` persists `exception_type`.
5. **IBKRProvider DEPRECATED** — New code must use 4-way ISP providers. Deletion scheduled Phase 3B.
6. **AMBER Smart Friction = PEACETIME flow** — Intentional per v10 ("allows exits"). No Integer Lock in AMBER. Gate 2 uses 25% sizing. Gemini OBSERVATION confirmed as design, not bug.
7. **TRANSMITTING state** — intermediate lock between ATTESTED and TRANSMITTED. On IB error, row stays TRANSMITTING for manual recovery. No auto-revert.
8. **Counter isolation (R8)** — `_increment_revalidation_count()` uses separate SQLite connection. Counter persists even if main JIT flow fails.
9. **F3 connection leak** — β Impl 3 sites use `contextlib.closing()`. 63 legacy sites DO NOT. Followup #9 for pre-live-trading sprint.
10. **`_ibkr_get_option_bid()`** — new in β Impl 3. Guards against IBKR sentinel values (-1, NaN, inf). No caching.
11. **Drift thresholds** — CC: $0.10 absolute. STK_SELL: 0.5% relative (`attested_limit * 0.005`). Applied uniformly across all modes.
12. **F1: Order routes at attested limit** — `_build_adaptive_sell_order(qty, row['limit_price'], account_id)`, NOT `live_bid`. live_bid is for JIT gates only.
13. **F2: Migration uses dynamic PRAGMA** — `PRAGMA table_info()` for column list. Never `SELECT *` in migration.
14. **Keyboard preservation (F6)** — retryable branches pass `reply_markup=_original_markup`. Terminal branches destroy keyboard.
15. **Poller per-row isolation (F7)** — inner `try/except` per row. Failed row retried next tick, not added to `_dispatched_audits`.
16. **`_poll_attested_rows` dedup** — `_dispatched_audits: set[str]` purges stale IDs each tick (IDs not in current ATTESTED set).
17. **Sweeper runs continuously (F8)** — `_sweep_attested_ttl_job` at 60s interval. Sweeps both STAGED (15min) and ATTESTED (10min). `/cc` preamble sweep is redundant safety net.
18. **`exception_type` column** — nullable TEXT on `bucket3_dynamic_exit_log`. NULL for R8 CC rows. Values: `rule_8_dynamic_exit`, `thesis_deterioration`, `rule_6_forced_liquidation`, `emergency_risk_event`.
19. **R5 staging route** — `POST /api/cure/r5_sell/stage`. Forced Liquidation requires WARTIME mode (route-level guard). `cio_token=True` automatically (attestation IS the CIO token per ADR-004).
20. **`get_staged_dynamic_exits()` includes both CC + STK_SELL** — panel branches on `action_type`. CC shows option columns. STK_SELL shows shares/limit/loss.
21. **Production DB is `agt_desk.db`** — the `agt_equities/` package name does NOT match the DB filename.
22. **walker.compute_walk_away_pnl() is single source of truth** — at `walker.py:759`. Do not re-inline.
23. **R2 denominator = margin-eligible NLV only** — per ADR-001. R11 = all-account NLV.
24. **v10 doc gaps logged for v10.1** — R7 fail-closed, `/override_earnings`, render_ts units, LOCKED→DRIFT_BLOCKED, re_validation_count fail-only, CANCEL terminal, ATTESTED 10min TTL, thesis 30-char minimum.

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
- Any rule disagrees with Rulebook v10 spec (including denominator semantics)
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
3. Read `reports/phase_3a_5c2_beta_v0.md` for β scope, resolved concerns, and impl order.
4. Read `HANDOFF_ARCHITECT_v5.md` (or latest version in repo root) for Architect rulings and session state.
5. Wait for Architect prompt. Do not start work autonomously.

End of handoff.
