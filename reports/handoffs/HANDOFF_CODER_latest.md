# AGT Equities — Coder (Claude Code) Handoff

**Last updated:** 2026-04-08
**Status:** Phase 3A.5c2-β COMPLETE. Followup #17 (orderRef linking + orphan recovery) COMPLETE. Pre-paper cleanup sprint COMPLETE.
**Tests:** 441/441 passing. Runtime: ~30s.
**Next:** Paper run. Then Phase 3B.

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
9. **Invariant deviations require STOP-and-surface BEFORE shipping, not after.**
10. **Empirical verification required for any fix touching transaction or resource semantics.**

---

## Working Directory

`C:\AGT_Telegram_Bridge\`

**Key files you touch most:**
- `telegram_bot.py` — main bot entry, ~11,100 lines
- `agt_equities/walker.py` — pure function, source of truth for cycles
- `agt_equities/rule_engine.py` — rule evaluators (R1-R7/R9/R11 real, R8/R10 stubs) + Gate 1/2, sweeper, is_ticker_locked, stage_stock_sale_via_smart_friction
- `agt_equities/mode_engine.py` — 3-mode state machine + LeverageHysteresisTracker
- `agt_equities/seed_baselines.py` — glide path + sector override + initial mode seed data
- `agt_equities/flex_sync.py` — EOD master log writer + walker warnings persist + desk_state regen + git auto-push
- `agt_equities/schema.py` — all SQLite migrations, idempotent DDL (incl. TRANSMITTING migration + Followup #17 4-column ALTER + recovery_audit_log)
- `agt_equities/order_state.py` — R5 order state machine (with BEGIN IMMEDIATE TOCTOU fix)
- `agt_equities/data_provider.py` — DEPRECATED IBKRProvider
- `agt_equities/market_data_interfaces.py` — 4-way ISP ABCs
- `agt_equities/providers/` — ibkr_price_volatility.py, ibkr_options_chain.py, yfinance_corporate_intelligence.py
- `agt_equities/state_builder.py` — upstream populator for PortfolioState
- `agt_equities/ib_chains.py` — IBKR option chain fetcher (string-based error classification)
- `agt_deck/main.py` — FastAPI Command Deck + Cure Console routes + Smart Friction POST (10-branch validator)
- `agt_deck/risk.py` — leverage, EL, concentration, sector helpers
- `agt_deck/queries.py` — DB read layer (get_staged_dynamic_exits, attest_staged_exit)
- `agt_deck/desk_state_writer.py` — generates desk_state.md (atomic write)
- `agt_deck/templates/cure_console.html` + `cure_partial.html` — Cure Console UI
- `agt_deck/templates/cure_dynamic_exit_panel.html` — Dynamic Exit & R5 panel
- `agt_deck/templates/cure_smart_friction.html` — Smart Friction modal (polymorphic: R8 CC + R5 STK_SELL)
- `agt_deck/templates/command_deck.html` — main deck with Mode Badge
- `tests/test_followup_17.py` — 23 Followup #17 tests (orphan scan, recovery, CAS, column ownership, timezone)
- `tests/test_cleanup6_toctou.py` — 3 concurrency regression tests (BEGIN IMMEDIATE)
- `tests/test_cleanup5_datetime.py` — 7 timezone-aware expiry tests
- `tests/test_yf_tkr_regression.py` — 2 yf_tkr AST regression tests
- `tests/test_walker.py` — 92 walker unit + special dividend regression
- `tests/property/test_walker_properties.py` — 23 Hypothesis property tests
- `tests/test_phase3a.py` — 65 Phase 3A tests
- `tests/test_phase3a5a.py` — 63 Phase 3A.5a tests
- `tests/test_rule_9.py` — 20 Phase 3A.5b tests
- `tests/test_phase3a5c2_alpha.py` — 36 Phase 3A.5c2-α tests
- `tests/test_phase3a5c2_beta_impl3.py` — 23 β Impl 3 tests
- `tests/test_phase3a5c2_beta_impl5.py` — 17 β Impl 5 tests
- `tests/test_phase3a5c2_beta_impl6_amber.py` — 4 β Impl 6 tests
- `tests/test_phase3a5c2_beta_impl8_thesis_copy.py` — 4 β Impl 8 tests
- `tests/test_phase3a5c2_beta_impl9_e2e.py` — 8 β Impl 9 E2E tests
- `tests/test_followup9_pr1_f3_correctness.py` — 5 Followup #9 PR1 tests
- `tests/test_followup9_pr2_sweep.py` — 7 Followup #9 PR2 tests
- `tests/test_followup9_pr3_other_modules.py` — 5 Followup #9 PR3 tests
- `tests/test_followup13_phase1.py` — 5 Followup #13 tests
- `scripts/verify_followup_17_lock_persistence.py` — empirical: CAS lock survives hard kill
- `scripts/verify_followup_17_timezone_fix.py` — empirical: _normalize_ibkr_time correctness
- `scripts/verify_cleanup_6_concurrency.py` — empirical: BEGIN IMMEDIATE prevents lost updates
- `scripts/archive_handoffs.py` — Friday handoff archiver

---

## Architecture Reminders

**3-Bucket data model (LOCKED):**
1. Real-time API — TWS via ib_async 2.1.0, no persistence
2. `master_log_*` tables (12 tables) — immutable, only `flex_sync.py` writes
3. Operational state — everything else

**3-mode state machine:**
- PEACETIME → normal ops
- AMBER → block `/scan` and new CSP; `/cc` exits/rolls allowed; Smart Friction uses PEACETIME flow; Gate 2 sizing 25%
- WARTIME → block `/scan` AND `/cc`; Cure Console only; Smart Friction uses Integer Lock; 3-strike bypass

**Canonical DB connection pattern (BINDING — Followup #9 ruling):**
```python
# WRITE sites: closing() for resource + with conn: for transaction
with closing(_get_db_connection()) as conn:
    with conn:
        conn.execute("UPDATE ...")

# READ sites: closing() alone
with closing(_get_db_connection()) as conn:
    rows = conn.execute("SELECT ...").fetchall()

# CROSS-AWAIT: split into read/await/write phases (Followup #13)
# CAS guard on every write (WHERE status='expected')

# CONCURRENT RMW: BEGIN IMMEDIATE before SELECT (CLEANUP-6)
with closing(_get_db_connection()) as conn:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT ...").fetchone()
        new_val = compute(current)
        conn.execute("UPDATE ... SET val = ?", (new_val,))
```

**Dynamic Exit Pipeline (α + β + #17):**
```
_stage_dynamic_exit_candidate(ticker, hh_name, hh_data, position, source)
  → conviction lookup → escalation → overweight scope → Gate 1 chain walk
  → INSERT bucket3_dynamic_exit_log (final_status='STAGED', source=<caller>)
  → 15-min TTL → sweep_stale_dynamic_exit_stages() → ABANDONED
  → Cure Console Dynamic Exit panel renders STAGED rows
  → "Begin Attestation" → Smart Friction modal GET (2a)
  → Operator completes attestation → Smart Friction POST (2b) → ATTESTED
  → 10s poller pushes Telegram [TRANSMIT] [CANCEL] keyboard
  → Stale-attestation guard (10min check) fires before CAS lock
  → TRANSMIT: order.orderRef = audit_id → JIT chain → placeOrder()
    → ATTESTED→TRANSMITTING→TRANSMITTED (ib_order_id written at Step 8)
  → CANCEL: ATTESTED→CANCELLED (terminal)
  → TRANSMIT_IB_ERROR: row stays TRANSMITTING, operator alerted
  → Step 8 failure: recovery wrapper alerts operator, row stays TRANSMITTING
  → Sweeper: stale STAGED→ABANDONED (15min, CAS-guarded), stale ATTESTED→ABANDONED (10min)
  → R5 fill handlers: orderRef fallback writes fill_price/fill_qty/fill_ts/ib_perm_id/commission
  → Startup orphan scan: resolves stuck TRANSMITTING rows via openTrades()/executions()
  → /recover_transmitting: manual operator recovery (filled|abandoned)
```

**Orphan scan resolution policy (D3 — BINDING):**
- Filled (openTrades or executions) → auto-flip TRANSMITTED
- Dead at IBKR (Cancelled/ApiCancelled/Inactive) → auto-flip ABANDONED
- Live unfilled or partial fill → leave row, alert operator
- NOT FOUND → leave row TRANSMITTING, alert operator, NEVER auto-abandon
- Gateway limitation: executions() = since-midnight only. Cross-midnight = manual recovery.

**Column ownership partition (D4 — BINDING, test-enforced):**
- Orphan scan writes ONLY: final_status, last_updated
- R5 handlers write ONLY: fill_price, fill_qty, fill_ts, ib_perm_id, commission
- TRANSMIT handler: final_status + ib_order_id
- /recover_transmitting: final_status + recovery_audit_log + optional ib_order_id

**`_run_cc_logic()` return contract:** `{"main_text": str}` — single key only.

---

## Current State

- **Tests:** 441/441 (92 walker + 23 property + 65 phase3a + 63 phase3a5a + 20 rule_9 + 10 dto + 18 providers + 36 alpha + 23 impl3 + 17 impl5 + 4 impl6 + 4 impl8 + 8 impl9 + 5 f9pr1 + 7 f9pr2 + 5 f9pr3 + 5 f13 + 1 r9_day1 + 23 followup17 + 3 cleanup6_toctou + 7 cleanup5_datetime + 2 yf_tkr_regression)
- **Mode:** PEACETIME
- **Production DB:** CLEAN — zero ATTESTED, zero TRANSMITTING, zero STAGED. Smoke test row cleaned up. 49 pre-live pending_orders bulk-cancelled.
- **Walker:** fully closed through W3.8 + special dividend fix (.net_cash)
- **Cure Console:** live at `/cure`, HTMX 60s refresh, Tailscale-exposed
- **Smart Friction:** Polymorphic (R8 CC + R5 STK_SELL). 10-branch validator. TOCTOU desk_mode guard.
- **Telegram TRANSMIT/CANCEL:** 9-step JIT chain + stale-attestation guard + Step 8 recovery wrapper. order.orderRef = audit_id. ib_order_id written at Step 8.
- **Sweeper:** STAGED 15min (CAS-guarded with `AND final_status='STAGED'`) + ATTESTED 10min.
- **Orphan scan:** Runs at startup (post_init) AND on autoreconnect. Resolves TRANSMITTING rows against openTrades()/executions(). Non-fatal on failure.
- **Connection management:** All write sites use canonical `closing() + with conn:`. Fill handlers use `BEGIN IMMEDIATE` (CLEANUP-6). CAS guards on all state transitions.
- **/recover_transmitting:** Operator command for manual TRANSMITTING recovery. BEGIN IMMEDIATE + CAS + recovery_audit_log in one transaction.
- **R5 fill handlers:** Fallback to bucket3_dynamic_exit_log by orderRef. Column ownership enforced (fill columns only, never final_status).
- **Timezone:** `_normalize_ibkr_time()` for Issue #287 workaround. `_parse_sqlite_utc()` for SQLite timestamps. `_parse_override_expiry()` for legacy naive override rows. `_TWS_TZ = America/New_York`.
- **GitLab:** `git@gitlab.com:agt-group2/agt-equities-desk.git` (SSH `@yashpatil1`)
- **Litestream:** continuous DB replication to Cloudflare R2
- **IBKR accounts:** U21971297 (Yash Individual), U22076329 (Yash Roth IRA), U22388499 (Vikram). U22076184 (Trad IRA) dormant.

---

## Completed Work — Followup #17 + Cleanup Sprint

| Task | Tests | Notes |
|------|-------|-------|
| CLEANUP-1: Smoke test row removal | 418 | smoke-pt-5b9e8816 → ABANDONED |
| CLEANUP-2: 49 pending_orders bulk cancel | 418 | Pre-live test artifacts → cancelled |
| CLEANUP-3: Walker .amount → .net_cash | 419 | Special dividend basis fix + regression test |
| CLEANUP-4: R4 set/list rename | 419 | rule_engine.py ticker_set cosmetic fix |
| CLEANUP-5: Naive datetime → UTC-aware | 426 | 3 override expiry sites + 7 tests |
| CLEANUP-6: TOCTOU BEGIN IMMEDIATE | 429 | 3 RMW sites fixed + 3 concurrency tests + empirical proof |
| #17 Part A: Schema migration | 430 | 4 ALTER TABLE + recovery_audit_log |
| #17 Part B: TRANSMIT path | 430 | orderRef linking, stale guard, Step 8 recovery wrapper |
| #17 Part C: Orphan scan | 430 | post_init + autoreconnect, D3 resolution policy |
| #17 Part D: /recover_transmitting | 430 | Operator command, BEGIN IMMEDIATE, audit log |
| #17 Part E: R5 fill handler patches | 430 | 3 fallback paths by orderRef |
| #17 Part F: Sweep 1 CAS guard | 430 | AND final_status='STAGED' + race log |
| #17 Part H: Timezone workaround | 430 | _normalize_ibkr_time, _parse_sqlite_utc |
| #17 Part G: Tests | 441 | 23 tests covering all parts |

## Completed Work — Sprint W + Sprint X (audit/documentation)

- Sprint W: 13/13 tasks, 12 reports + DR_RUNBOOK.md + PRE_PAPER_CHECKLIST.md
- Sprint X: 39/39 tasks (32 base + 7 Phase 15), 39 reports totaling ~8,200 lines
- Key outputs: schema_reference.md (1555 lines), first_live_trade_walkthrough.md (577 lines), architecture_diagram.md, project_glossary.md, 31 open questions compiled

---

## In Flight

**Nothing in flight.** All followups through #17 are complete. System is paper-run ready.

---

## Followups (logged, NOT in scope)

| # | Item | Filed by | Priority |
|---|------|----------|----------|
| 2 | R7 earnings cache scheduled job | α | Post-paper |
| 7 | Mobile keyboard ticker strip leniency | β Impl 2b | Post-paper |
| 8 | Inline Gate 1 consolidation (staging path) | β Impl 3 / Sprint W | Post-paper |
| 10 | R5 auto-discovery pipeline | β Impl 5 survey | Post-paper |
| 11 | `/sell_shares` Telegram command | β Impl 5 | Post-paper |
| 14 | ~6 low-risk reply_text cross-await sites | Followup #13 / Sprint W | Post-paper |
| 19 | Flex statement reconciliation for cross-midnight orphans | #17 | Post-paper |
| 25 | TOCTOU deferred sites (beyond share ledger) | CLEANUP-6 survey | Post-paper |

**Closed followups:**
- ~~#4~~ yf_tkr regression test — COMPLETE (Sprint W)
- ~~#9~~ Connection leak sweep — COMPLETE
- ~~#13 Phase 1~~ Cross-await refactor — COMPLETE
- ~~#17~~ orderRef linking + orphan recovery + R5 fill patch — COMPLETE

---

## Phase Backlog (do NOT start without Architect prompt)

- **Phase 3B:** desk_state.md full integration (5-min APScheduler, EL snapshots from IBKR live API, automated mode pipeline, R7 earnings cache scheduled job)
- **Phase 3C:** LLM CIO Oracle advisory injection above Smart Friction widget
- **Phase 3D:** Telegram pruning to ~4500 lines, kill display commands, replace text approvals with inline buttons
- **Phase 3E:** Mobile responsive polish

---

## Active Gotchas / Don't-Touch List

1. **R8 stub** — returns PENDING. Real R8 infrastructure (Gate 1/2, orchestrator, campaigns) lives alongside. Execution path live via TRANSMIT handler.
2. **R9 (Red Alert compositor)** — REAL evaluator. REPORTING ONLY — does NOT trigger mode transitions.
3. **R7 (Earnings Window)** — REAL evaluator. FAIL-CLOSED. Override via `/override_earnings`.
4. **R5 (sell gate)** — REAL evaluator + staging function. Gate via `evaluate_rule_5_sell_gate()`.
5. **IBKRProvider DEPRECATED** — New code must use 4-way ISP providers.
6. **AMBER Smart Friction = PEACETIME flow** — Intentional. No Integer Lock in AMBER. Gate 2 uses 25%.
7. **TRANSMITTING state** — intermediate lock between ATTESTED and TRANSMITTED. On IB error, row stays TRANSMITTING. Orphan scan resolves on restart. Manual via /recover_transmitting.
8. **Counter isolation (R8)** — `_increment_revalidation_count()` uses separate connection.
9. **Canonical DB pattern (BINDING)** — `closing()` for resource, inner `with conn:` for write transactions, `BEGIN IMMEDIATE` for concurrent RMW sites, CAS guards for cross-await.
10. **`_ibkr_get_option_bid()`** — Guards against IBKR sentinel values (-1, NaN, inf). No caching.
11. **Drift thresholds** — CC: $0.10 absolute. STK_SELL: 0.5% relative.
12. **F1: Order routes at attested limit** — `row['limit_price']`, NOT `live_bid`.
13. **F2: Migration uses dynamic PRAGMA** — `PRAGMA table_info()` for column list.
14. **Keyboard preservation (F6)** — retryable branches pass `reply_markup=_original_markup`.
15. **Poller per-row isolation (F7)** — inner `try/except` per row. Failed row retried next tick.
16. **Poller dedup** — `_dispatched_audits: set[str]` purges stale IDs each tick.
17. **Sweeper runs continuously (F8)** — 60s interval. Sweeps STAGED (15min, CAS-guarded) + ATTESTED (10min).
18. **`exception_type` column** — nullable TEXT. NULL for R8 CC. Values: rule_8_dynamic_exit, thesis_deterioration, rule_6_forced_liquidation, emergency_risk_event.
19. **R5 staging route** — `POST /api/cure/r5_sell/stage`. Forced Liquidation requires WARTIME.
20. **Production DB is `agt_desk.db`** — NOT matching `agt_equities/` package name.
21. **walker.compute_walk_away_pnl() is single source of truth** — at walker.py:759.
22. **R2 denominator = margin-eligible NLV only** — per ADR-001.
23. **handle_approve_callback** — read/await/write phase separation with CAS guards.
24. **~6 reply_text cross-await sites** — known-tolerated (Followup #14). Low risk.
25. **order.orderRef = audit_id** — set BEFORE placeOrder(). IBKR round-trips it through openTrades() and executions(). Orphan scan matches by this field. NEVER touch order.orderId pre-placeOrder.
26. **Stale-attestation guard** — fires BEFORE Step 6 CAS lock. Rejects attestations older than 10 minutes.
27. **Step 8 recovery wrapper** — on DB write failure after placeOrder, logs + alerts + returns. Row stays TRANSMITTING for /recover_transmitting.
28. **_normalize_ibkr_time()** — Issue #287 workaround. Naive datetimes assumed to be _TWS_TZ (America/New_York).
29. **Gateway since-midnight limitation** — executions() returns current calendar day only. Cross-midnight orphans require manual /recover_transmitting.
30. **BEGIN IMMEDIATE in RMW handlers** — _on_shares_sold, _on_shares_bought, append_status. Prevents concurrent partial fill lost updates.
31. **Sweep 1 CAS guard** — `AND final_status='STAGED'` prevents overwriting concurrent attestations.

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
- Any rule disagrees with Rulebook v10 spec
- Walker purity would be violated
- Bucket 2 would be written by anything other than `flex_sync.py`
- Day 1 baseline computes AMBER or RED
- A fix might introduce new bugs
- Audit narrowing
- Secret found in staged files
- SSH/git auth fails
- **Invariant deviation discovered during execution**
- **Transaction/resource semantics change** — empirical verification required

---

## How to Pick Up (new session ritual)

1. Read this file end-to-end.
2. Read `desk_state.md` at `C:\AGT_Telegram_Bridge\desk_state.md`.
3. Wait for Architect prompt. Do not start work autonomously.

End of handoff.
