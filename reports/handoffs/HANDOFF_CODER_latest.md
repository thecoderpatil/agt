# AGT Equities — Coder (Claude Code) Handoff

**Last updated:** 2026-04-11
**Status:** Sprint 1 (A-F) + Cleanup A + Sprints B/C/D + Cure Polish + Execution Kill-Switch + PTB 22.7 Fix + P3.2-alt Day 2 findings + V2 Smart Yield Walk-Down + Defensive Roll Engine + adaptive roll execution + **ADR-005 V2 Router WARTIME whitelist/BAG/BTC cash-paid notional** + **ADR-006 ACB pipeline hardening (per-account precision + same-day delta)** + **Followups #9/#10/#11 doc** + **Act 60 Fortress CSP Screener C1→C6.1 (6 of 6 phases complete, live paper-run hotfix applied)** — all COMPLETE.
**Tests:** 859/864 passing, 5 skipped, 0 failed. Runtime: ~41s. (baseline 634 → 859 = +225 tests across ADR-005 + ADR-006 + Screener C1-C6.1.)
**Next:** C6.2 — NaN-safe coercion in `agt_equities/ib_chains.py` to resolve the GD/HSY/MPC class of paper-run failure (IBKR error 10091 / subscription edge cases). Pre-flight: verify `tests/test_ib_chains.py` exists and report before Architect dispatches C6.2.

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
11. **COMMIT after every sprint sub-unit hits its test target. No uncommitted work across sprints.**
12. **Push to GitLab immediately after each commit. Mirror propagates to GitHub.**

---

## Working Directory

`C:\AGT_Telegram_Bridge\`

**Key files you touch most:**
- `telegram_bot.py` — main bot entry, ~9,500 lines (post-Cleanup A purge from 12,180), cold-start wartime pin + adaptive roll combo execution/staging
- `agt_equities/walker.py` — pure function, source of truth for cycles
- `agt_equities/rule_engine.py` — rule evaluators (R1-R7/R9/R11 real, R8/R10 evaluator stubs) + Gate 1/2, V2 Smart Yield Walk-Down candidate engine, Defensive Roll engine, sweeper, is_ticker_locked, stage_stock_sale_via_smart_friction. R9 compositor NOW WIRED (Sprint B).
- `agt_equities/archive_wartime_v1.py` — archived Emergency Kill-Switch / Capital Velocity reference logic
- `agt_equities/mode_engine.py` — 3-mode state machine + LeverageHysteresisTracker + cold-start WARTIME pin
- `agt_equities/seed_baselines.py` — glide path + sector override + initial mode seed data (NULL ticker dedupe fix)
- `agt_equities/flex_sync.py` — EOD master log writer + walker warnings persist + desk_state regen + git auto-push
- `agt_equities/schema.py` — all SQLite migrations, idempotent DDL (incl. operational tables migrated from init_db in Cleanup A)
- `agt_equities/beta_cache.py` — cached yfinance trailing betas (Sprint 1F), daily 04:00 refresh
- `agt_equities/risk.py` — leverage, EL, concentration, sector helpers (relocated from agt_deck/ in Sprint B)
- `agt_equities/config.py` — canonical HOUSEHOLD_MAP, ACCOUNT_TO_HOUSEHOLD, MARGIN_ELIGIBLE_ACCOUNTS, MARGIN_ACCOUNTS, PAPER_MODE (Sprint C pre-step + Sprint D)
- `agt_equities/state_builder.py` — correlation matrix + account EL snapshot builder + DeskSnapshot + build_state() SSOT (Sprint C1)
- `agt_equities/order_state.py` — R5 order state machine (with BEGIN IMMEDIATE TOCTOU fix)
- `agt_equities/providers/` — ibkr_price_volatility.py, ibkr_options_chain.py, yfinance_corporate_intelligence.py
- `agt_deck/main.py` — FastAPI Command Deck + Cure Console routes + Smart Friction POST + Lifecycle Queue + Health Strip
- `agt_deck/queries.py` — DB read layer (get_staged_dynamic_exits, attest_staged_exit, get_lifecycle_rows, get_health_strip_data)
- `agt_deck/formatters.py` — money, pct, pnl_color, format_age, el_pct_color, lifecycle_state_classes
- `agt_deck/desk_state_writer.py` — generates desk_state.md (atomic write)
- `agt_deck/templates/cure_console.html` — Cure Console UI + Health Strip + Lifecycle Queue HTMX wiring
- `agt_deck/templates/cure_lifecycle.html` — Action Queue HTMX fragment (10s self-poll)
- `agt_deck/templates/cure_health_strip.html` — Health Strip HTMX fragment (10s self-poll)
- `agt_deck/templates/cure_smart_friction.html` — Smart Friction modal (polymorphic: R8 CC + R5 STK_SELL)
- `agt_deck/templates/command_deck.html` — main deck with Underwater Positions panel (grouped by household, CC column, breathe animation)
- `agt_deck/templates/cure_partial.html` — Cure Console HTMX body (Underwater Positions, Glide Paths, Rule Evaluations)
- `agt_deck/templates/base.html` — base template with paper mode banner
- `agt_deck/static/app.css` — breathe animation keyframes + reduced-motion support
- `protocols/P3_2_paper_run_protocol.md` — end-to-end paper run protocol
- `launcher/` — one-click Windows desktop launcher (start_cure.bat, stop_cure.bat, AGT_Cure.vbs, install_shortcut.ps1)
- `agt_equities/screener/` — Act 60 Fortress CSP Screener package (C1→C6.1, read-only side project)
  - `__init__.py` — pipeline docstring reflecting Tech-First reorder
  - `config.py` — single source of truth for all Phase 1-6 thresholds, exclusion lists, RAY band constants, IBKR courtesy delays
  - `types.py` — frozen dataclasses: UniverseTicker, TechnicalCandidate, Phase2Output, FundamentalCandidate, CorrelationCandidate, VolArmorCandidate, StrikeCandidate, RAYCandidate (each with `from_upstream` classmethod for carry-forward)
  - `cache.py` — file-backed TTL cache with atomic writes
  - `finnhub_client.py` — async httpx wrapper with token-bucket rate limiter (50/min Free tier), exponential backoff retry, cache hit/miss counters
  - `sp500_nasdaq100.csv` — 517-ticker universe seed (504 S&P 500 from iShares IVV + 13 NDX-only)
  - `universe.py` — Phase 1 (Finnhub Free profile2 exclusions + structural REIT/MLP/BDC/trust/SPAC filter)
  - `technicals.py` — Phase 2 (yfinance batch download + SMA200/RSI14/BBand pullback gate, returns Phase2Output with raw df hoisted for Phase 3.5)
  - `fundamentals.py` — Phase 3 (yfinance per-ticker: Altman Z, FCF yield, net-cash bypass leverage gate, ROIC, short interest fail-open)
  - `correlation.py` — Phase 3.5 (global correlation fit against current holdings with SLS/GTLB/TRAW.CVR exclusions + supplemental holdings download)
  - `vol_event_armor.py` — Phase 4 (live IBKR reqHistoricalData IVR Option D + YFinanceCorporateIntelligenceProvider earnings/ex-div/corp-action gates)
  - `chain_walker.py` — Phase 5 (ib_chains-routed option chain walk with interval-based strike floor, C6.1 fix)
  - `ray_filter.py` — Phase 6 (sync terminal RAY band filter, NaN-safe)
- `agt_equities/ib_chains.py` — low-level IBKR chain API (get_expirations, get_chain_for_expiry) — ONLY Phase 4/5 consumers are allowed to import ib_async
- `scripts/probe_ibkr_historical_iv.py` — throwaway diagnostic that confirmed Option D IVR subscription viability on 2026-04-11 (AAPL 249 bars, MSFT 250 IVR 95.7%, SPY 250 IVR 23.8%)

---

## Architecture Reminders

**3-Bucket data model (LOCKED):**
1. Real-time API — TWS via ib_async 2.1.0, no persistence
2. `master_log_*` tables (12 tables) — immutable, only `flex_sync.py` writes
3. Operational state — everything else

**3-mode state machine:**
- PEACETIME → normal ops
- AMBER → block `/scan` and new CSP; exits/rolls allowed; Smart Friction uses PEACETIME flow; Gate 2 sizing 25%
- WARTIME → block `/scan` AND `/cc`; Cure Console only; Smart Friction uses Integer Lock; 3-strike bypass
- Cold-start pin: startup pins to WARTIME immediately when live leverage >= 1.50x; no-op if already WARTIME

**Canonical DB connection pattern (BINDING — Followup #9 ruling):**
```python
# WRITE sites: closing() for resource + with conn: for transaction
with closing(_get_db_connection()) as conn:
    with conn:
        conn.execute("UPDATE ...")

# READ sites (Sprint B cursor hygiene): explicit cursor.close()
with closing(_get_db_connection()) as conn:
    rows = _fetchall(conn, "SELECT ...")  # agt_deck/queries.py helpers

# CONCURRENT RMW: BEGIN IMMEDIATE before SELECT (CLEANUP-6)
```

**Dynamic Exit Pipeline (α + β + #17 + Sprint 1A/1D):**
```
Staging → Cure Console attestation → [10s trust-tier cooldown] → JIT 9-step chain → placeOrder
  Pre-trade gates: halt check → mode gate → $25k notional → non-wheel filter → F20 NULL guard
  Cooldown: T0=10s, T1=5s, T2=0s (AGT_TRUST_TIER env)
  Gate 1 staging now calls canonical evaluate_gate_1 (Sprint B dedup)
  _discover_positions includes DEX encumbrance overlay (Sprint B)
```

**Paper Mode (Sprint 1C):**
- `AGT_PAPER_MODE=1` → port 4002/7497, paper account IDs from `AGT_PAPER_ACCOUNTS` env
- `[PAPER]` prefix on all outbound Telegram messages
- `[WARTIME]`/`[AMBER]` mode prefix on all pushes
- Blue banner on Cure Console when paper active
- `_round_to_nickel()` for OPT prices (nickel ≤$3, dime >$3)

---

## Current State

- **Tests:** 859/864 passing, 5 skipped, 0 failed on `python -m pytest -q tests` (~41s runtime)
  - Full screener slice: 198/198 passing across 9 test files (isolation 3 + finnhub 17 + phase1 24 + phase2 18 + phase3 33 + correlation 22 + phase4 32 + phase5 29 + phase6 20)
  - AST guard at `tests/test_screener_isolation.py` enforces: no screener file imports telegram_bot, V2 router, walker, trade_repo, rule_engine, mode_engine, or `_pre_trade_gates`. `ib_async` is whitelisted ONLY for `chain_walker.py` and `vol_event_armor.py`.
- **Mode:** PEACETIME
- **Production DB:** CLEAN, backed up as `agt_desk.db.p3.2alt.bak`. mode_transitions seeded with 3 OVERWEIGHT rows (2026-04-01 backdate) for watchdog live test.
- **Walker:** fully closed through W3.8 + special dividend fix (.net_cash). 14 active cycles (8 Yash + 6 Vikram).
- **telegram_bot.py:** ~9,500 lines (down from 12,180 after Cleanup A purge)
- **Cure Console:** live at `/cure`, Health Strip (10s EL refresh), Lifecycle Queue (10s), Underwater Positions (grouped by household, CC column), linear-breathing top strip
- **Smart Friction:** Polymorphic. TOCTOU desk_mode guard.
- **Telegram commands (pruned Sprint 1D):** /start, /status, /orders, /budget, /clear, /reconnect, /vrp, /think, /deep, /approve, /reject, /declare_peacetime, /mode, /cure, /recover_transmitting, /halt, /resume
- **Killed commands:** /health, /cycles, /ledger, /fills, /dashboard, /cc, /mode1, /scan, /rollcheck, /declare_wartime, /sync_universe, /cleanup_blotter, /status_orders, /stop, /dynamic_exit, /override, /override_earnings, /reconcile, /clear_quarantine
- **R9 compositor:** WIRED (Sprint B Unit 1). evaluate_all(ps, hh, conn=conn) passes conn for real R9. Reporting string now correctly shows 4-condition denominator.
- **Gate 1:** DEDUPED (Sprint B Unit 3). Staging calls canonical evaluate_gate_1.
- **DEX overlay:** FIXED (Sprint B Unit 2). _discover_positions reads STAGED/ATTESTED/TRANSMITTING encumbrance.
- **Beta cache:** Daily 04:00 refresh job. Both top strip and rule engine use same cached betas.
- **EL snapshots:** 30s writer job. Top strip and PortfolioState read from el_snapshots table.
- **R7 corporate intel:** Daily 05:00 refresh job. evaluate_rule_7 reads cache. Bug fixed: evaluate_all now forwards conn= to R7 (enables operator overrides). Provider handles yfinance 1.2.0 datetime.date return type.
- **PRAGMA tuning:** WAL + synchronous=FULL + wal_autocheckpoint=4000 + busy_timeout=5000.
- **DeskSnapshot SSOT (Sprint C + #43):** `build_state()` returns frozen DeskSnapshot (NAV, cycles, betas, DEX encumbrance). 3-tier NAV: live_nlv param > el_snapshots (<120s) > Flex EOD. `nav_source_by_account` tracks provenance. `build_top_strip` now injects `live_nlv_dict` and consumes it.
- **Cold-start wartime pin (Priority 4):** startup checks live `accountSummaryAsync()` NLV + live spots before watchdog/polling loops. If any household leverage is >= 1.50x, it logs WARTIME with reason `Cold-start pin: leverage >= 1.50x`.
- **Dynamic Exit engine:** V1 Emergency Kill-Switch logic archived to `agt_equities/archive_wartime_v1.py`. Active candidate selection in `rule_engine.py` is V2 Smart Yield Walk-Down. `evaluate_defensive_rolls()` appended for 0.40 delta / Friday Trap defense.
- **Adaptive execution path:** Adaptive Mid combos automated; current HEAD routes adaptive roll combos through human-in-the-loop staging.
- **Config centralized (Sprint C + D):** HOUSEHOLD_MAP, ACCOUNT_TO_HOUSEHOLD, MARGIN_ELIGIBLE_ACCOUNTS, MARGIN_ACCOUNTS, PAPER_MODE all in `agt_equities/config.py`. Paper-aware. Rule engine imports from config (no hardcoded account IDs).
- **Execution kill-switch:** `AGT_EXECUTION_ENABLED` env var (default OFF) + `_HALTED` in-process + `execution_state` DB row. Triple-gate OR logic. All 3 placeOrder sites wrapped. AST guard test enforces. `/halt` persists to DB, `/resume CONFIRM` clears. DEX TRANSMIT handler reverts TRANSMITTING→CANCELLED on gate/kill-switch failure (Finding #10).
- **AGTFormattedBot:** ExtBot subclass replaces monkey-patch for PTB 22.7 compat. Applies `_format_outbound` (paper + mode prefix) to send_message + edit_message_text.
- **P3.2-alt protocol:** `protocols/P3_2alt_read_only_live_protocol.md`. P3.2 paper superseded.
- **dump_rules.py:** `scripts/dump_rules.py` — one-shot rule evaluator for Day 1.4 smoke test. Verified against live DB: 14 cycles, R11 Yash 2.16x / Vikram 2.85x, R9 Red Alert active both households.
- **P3.2-alt pre-flight:** Day 0 complete. Kill-switch verified, DB backed up, git tag `p3.2alt-start` at `04c1d20`.
- **GitLab:** `git@gitlab.com:agt-group2/agt-equities-desk.git` (SSH `@yashpatil1`)
- **GitHub mirror:** push mirror from GitLab (verified)
- **Litestream:** continuous DB replication to Cloudflare R2
- **IBKR accounts:** U21971297 (Yash Individual), U22076329 (Yash Roth IRA), U22388499 (Vikram). U22076184 (Trad IRA) dormant but included in NAV.

---

## Completed Work — Sprint 1 (A-F)

| Sprint | Tests | Key Deliverables |
|--------|-------|-----------------|
| 1A | 485 | Unified `_pre_trade_gates()` (mode, $25k, non-wheel, F20), cold-start WARTIME→PEACETIME pin |
| 1B | 513 | Lifecycle Queue panel, Health Strip, `el_snapshots` writer, `cure_dynamic_exit_panel.html` deleted |
| 1C | 531 | PAPER_MODE env flag, port switch, paper banner, [PAPER] prefix, nickel-round (dime >$3) |
| 1D | 561 | Kill 20 commands + 2 callbacks, /halt killswitch, 10s DEX cooldown, STAGED coalescing, mode prefix |
| 1E | 565 | `client_id` on 7 tables, template hardcode cleanup |
| 1F | 578 | NAV per-account fix, beta_cache module, EL read path, glide-path softening, R9 compositor wired in deck, R7 cache job, seed_baselines dedupe, mode transition idempotency |

## Completed Work — Cleanup Sprint A

| Purge | Lines | What |
|-------|-------|------|
| 1+2 | 2,128 | 18 dead command handlers + 3 dead callbacks + alias |
| 3+4 | 123 | 2 legacy builders + 1 dead wrapper |
| 5 | 457 | DDL migrated to schema.py `register_operational_tables()` |

## Completed Work — Sprint B (Architectural Fixes)

| Unit | What | Commit |
|------|------|--------|
| 1 | R9 compositor wired via evaluate_all(conn=conn) | `5a3b6f2` |
| 2 | DEX overlay in _discover_positions (STAGED/ATTESTED/TRANSMITTING encumbrance) | `4286248` |
| 3 | Gate 1 dedup — staging calls canonical evaluate_gate_1 | `7ccd0cf` |
| 4 | risk.py relocated to agt_equities/ | `b688c35` |
| 5 | run_polling audit — DEFERRED (stable, revisit post-paper) | — |
| 6 | PRAGMA tuning (synchronous=FULL, autocheckpoint=4000, busy_timeout=5000) | `2630b19` |
| 7 | Cursor hygiene helpers (_fetchall/_fetchone) + busy_timeout on deck | `e4669ac` |
| 8 | Account summary verify on reconnect | `6a3518d` |
| 9 | Partial fill audit — REPORT ONLY (fill_qty is last-write-wins, not cumulative) | — |

---

## Completed Work — Sprint C (state_builder SSOT)

| Unit | What | Commit |
|------|------|--------|
| Pre-step | HOUSEHOLD_MAP → `agt_equities/config.py` (7 definition sites collapsed) | `017e9b1` |
| C1 | Additive `build_state()` + `DeskSnapshot` dataclass in state_builder.py | `5c58083` |
| C2 | `build_top_strip` consumes DeskSnapshot (NAV, cycles, betas deduped) | `6772d18` |

**C2 pivot:** Original plan targeted `_build_cure_data` then `_discover_positions`. Survey found both are orchestrators/IB-aggregators with minimal DeskSnapshot overlap. `build_top_strip` was the actual DB-read dedup target. `_discover_positions` deferred to Followup #35. `_build_cure_data` deferred to Followup #33.

## Completed Work — Cure Console Polish

| Group | What | Commit |
|-------|------|--------|
| G6 | `agt_deck/main.py` PAPER_MODE import from config (pre-step residual) | `25faa5e` |
| G1+G2 | Underwater Positions ported to cure_console, both decks relayouted (household grouping, CC column, sort indicator) | `816a5cd` |
| G7 | Linear-breathing animation on top strip .num values (4s linear, hover-pause, reduced-motion) | `402d187` |

## Completed Work — Sprint D (Rule Engine Hardcode Purge)

| Unit | What | Commit |
|------|------|--------|
| D1 | MARGIN_ELIGIBLE_ACCOUNTS + MARGIN_ACCOUNTS → config.py (paper-aware) | `ad8dc6f` |
| D2 | Rule 6 derives Vikram account from config (was hardcoded U22388499) | `ad8dc6f` |
| D3 | telegram_bot.py imports MARGIN_ACCOUNTS from config | `ad8dc6f` |
| D4 | AST guard test: no U-prefixed 8-digit strings in rule_engine.py | `ad8dc6f` |

**Resolved DEX pre-flight Blockers 1-3.** Paper run unblocked.

## Completed Work — Hotfixes + Safety

| Item | What | Commit |
|------|------|--------|
| Hotfix | Restore `get_betas` import in `_build_cure_data` (Sprint C2 fallout) | `13d1db8` |
| Hotfix | `AGT_DECK_TOKEN` read after `load_dotenv` (config import order) | `617f590` |
| PTB 22.7 | `AGTFormattedBot(ExtBot)` subclass replaces monkey-patch | `d2ab6d6` |
| Kill-switch | `execution_gate.py` + `execution_state` DB table + 3 placeOrder wraps + `/halt` persist + `/resume CONFIRM` | `7c821ad` |
| P3.2-alt | Protocol doc + P3.2 paper marked superseded | `04c1d20` |
| Doc hygiene | Stale test counts + anchors refreshed | `7c45a6f` |
| dump_rules | `scripts/dump_rules.py` one-shot rule evaluator for Day 1.4 | `177ad25` |

## Completed Work — P3.2-alt Day 2 Findings

| Finding | What | Commit |
|---------|------|--------|
| #10 | DEX TRANSMIT: revert TRANSMITTING→CANCELLED on gate/kill-switch failure. `_revert_transmitting_to_cancelled` helper. Deleted redundant NULL-account block (wrong WHERE clause). Preserved TRANSMIT_IB_ERROR sticky path. | `40bfdae` |
| #12 | `/cure` auto-detect LAN host via UDP socket trick + `AGT_DECK_HOST` env override + `AGT_DECK_PORT`. Removed manual Tailscale hint. | `41e3e02` |
| #4 | R7 dual fix: (A) yfinance 1.2.0 returns `datetime.date`, not `datetime.datetime` — isinstance dispatch. (B) `evaluate_all` was not forwarding `conn=` to `evaluate_rule_7` — operator overrides were dead code. | `ad275b3` |
| #43 | NAV freshness: `build_state()` overlays live NLV from el_snapshots (<120s) over Flex EOD. `nav_source_by_account` field on DeskSnapshot for observability. | `bdeb4af` |
| #43 v2 | `live_nlv` injection param on `build_state()` for 0-second freshness. 3-tier priority: injected > db_live > flex_eod. `agt_deck/main.py` top-strip caller now wires `live_nlv_dict`. | `019d118` |

## Completed Work — Post-Handoff HEAD

| Commit | What |
|--------|------|
| `49aa7c0` | R9 reporting string fixed from 3-condition to 4-condition denominator. Deck render path updated. |
| `532cb7c` | Cold-start wartime pin now checks live leverage before startup loops and pins into WARTIME when leverage >= 1.50x. |
| `d65a536` | Archived Wartime V1 capital-velocity logic. Active Rule 8 candidate engine replaced with V2 Smart Yield Walk-Down. Defensive Roll Engine appended. |
| `c5f7665` | Adaptive Mid combo execution path upgraded to full automation. |
| `4528c38` | Adaptive roll combos routed through human-in-the-loop staging on top of adaptive combo plumbing. |

## Completed Work — ADR-005 (V2 Router WARTIME whitelist + BAG + BTC cash-paid notional)

| Commit | What |
|--------|------|
| `729c5ba` | DIFF 1: `_pre_trade_gates` full body replacement. WARTIME whitelist = `("dex", "v2_router")`. OPT BUY uses cash-paid notional (premium × 100), OPT SELL keeps strike-notional. BAG combos get `qty * abs(lmtPrice) * 100` cash exposure. Gate 2 has explicit guards for zero qty, non-numeric lmtPrice, missing strike on SELL, negative lmtPrice on BUY, and unsupported secType. DIFF 2: `_place_single_order` routes by `payload["origin"]` → `v2_router` vs `legacy_approve` site. DIFF 3: `_scan_and_stage_defensive_rolls` docstring + mode banner in alerts, STATE 2 HARVEST and STATE 3 DEFEND ticket dicts tagged with `origin="v2_router"`, `v2_state`, and `v2_rationale`. New test file `tests/test_pre_trade_gates_v2.py` with 12 tests (WARTIME whitelist, BAG bands, BTC cash-paid, fail-closed regressions). Test fallout fixed in-commit: `test_bag_blocked` deleted (encoded pre-ADR invariant; BAG now allowed per CC4, covered by new test file), 4 V2 router alert-list assertions updated to include the mode banner per CC3. |

## Completed Work — ADR-006 (ACB Pipeline Hardening)

| Commit | What |
|--------|------|
| `b874d81` | Walker `Cycle._premium_by_account` for IRS-correct per-account attribution. All 9 `cycle.premium_total += ev.net_cash` sites paired with `_credit_premium_to_account(cycle, ev.account_id, ev.net_cash)` per ruling R1 (universalized — no EXPIRE_WORTHLESS exception). Walker `Cycle.premium_for_account()` / `adjusted_basis_for_account()` accessors. `_load_premium_ledger_snapshot` extended with `account_id` parameter (Act 60 compliance). Legacy fallback fail-closed when `account_id` provided. V2 router STATE_1/STATE_3 propagate `pos.account` through ACB lookup. `trade_repo.get_active_cycles_with_intraday_delta` same-day `fill_log` overlay using `MAX(last_synced_at)` from `master_log_trades` as watermark (Architect ruling after `flex_sync_log` table absence verified). New test file `tests/test_acb_per_account.py` with 6 unit tests + 5 skipped integration stubs. `cc_decision_log` audit wiring deferred to Followup #10. |

## Completed Work — Followups doc

| Commit | What |
|--------|------|
| `3bb8c81` | Appended Followups #9, #10, #11 to `FOLLOWUPS.md`. #9: intraday delta watermark reconciliation gap (pre-row reconciliation flag long-term fix). #10: `cc_decision_log` V2 router audit wiring (forensic infra, deferred from ADR-006). #11: empirical verification of `EXPIRE_WORTHLESS` `ev.net_cash` values (hygiene audit to determine whether the line at walker.py:410 is H1 dead code or H2 load-bearing). |

## Completed Work — Act 60 Fortress CSP Screener (C1 → C6.1)

Read-only side project. Six-phase pipeline that surfaces wheel-eligible cash-secured put candidates from an S&P 500 + NASDAQ 100 universe. Completely isolated from execution paths by AST guard (`tests/test_screener_isolation.py`). End-to-end runnable from a Python REPL — orchestrator wiring to `/scan` is a separate sprint (C7).

| Commit | What | Tests added |
|--------|------|-------------|
| `9e223f2` | C1: package scaffolding. `agt_equities/screener/` with `__init__`, `cache.py` (file TTL cache, atomic writes), `finnhub_client.py` (async httpx + 50/min token-bucket rate limiter, backoff retry), `sp500_nasdaq100.csv` (517 deduplicated tickers from iShares IVV + Wikipedia NDX), `tests/test_screener_isolation.py` AST guard, `tests/test_screener_finnhub.py`. | +20 |
| `81af518` | C2: Phase 1 (Finnhub Free profile2 exclusions) + Phase 2 (yfinance batch technicals). Adds `universe.py`, `technicals.py`, `config.py`, `types.py` (UniverseTicker, TechnicalCandidate). Bugfix in-commit: `_passes_sector` / `_passes_country` rejected whitespace-only strings correctly. | +34 |
| `0092b35` | C2.1: cache hit rate instrumentation. `FinnhubClient.get_stats()` + Phase 1 final log line surfaces `cache_hits`/`cache_misses`/`hit_rate_pct`/`elapsed`. | +1 |
| `56487aa` | C3: Phase 3 (yfinance per-ticker fundamentals). Altman Z-Score, FCF Yield, Net Debt/EBITDA, ROIC, Short Interest. `FundamentalCandidate` dataclass with carry-forward + field-rename (`current_price`→`spot`, `bband_middle`→`bband_mid`). Thresholds per Architect dispatch: Z>3.0, FCF≥4%, ND/EBITDA≤3.0, ROIC≥10%, SI≤10%. | +27 |
| `570ce02` | C3.5: correlation-fit portfolio gate. Global fit (no per-household routing per Yash ruling). Pairwise |corr| ≤ 0.60 against effective holdings. `CORRELATION_HOLDINGS_EXCLUSIONS = {SLS, GTLB, TRAW.CVR}` applied before already-held check. Supplemental holdings download path for holdings not in Phase 2 df. **BREAKING CHANGE**: Phase 2 return type `list[TechnicalCandidate]` → `Phase2Output(survivors, price_history)`. 17 phase2 tests updated mechanically + 1 new for dataframe hoisting. | +23 |
| `b1404df` | C3.6: structural exclusions. `EXCLUDED_SECTORS` expanded from 3 quality exclusions to 22 entries (REITs, MLPs, BDCs, trusts, SPACs, closed-end funds, shell companies). Per-ticker sector-drop log added to `universe.py` (tight-scoped to sector path only per Architect ruling after initial over-scope). Yash ruling 2026-04-11: wheel candidate universe is US-domiciled common-stock C-corporations only. | +6 |
| `6f9744a` | C3.7: Phase 3 semantic fixes discovered during TIKR calibration. Fix 1: short interest fail-OPEN when both `shortPercentOfFloat` and `sharesShort/floatShares` unavailable (yfinance has SI=None for ~20-30% of tickers). Fix 2: net-cash bypass on leverage gate — net_debt ≤ 0 short-circuits to 0.0 sentinel, positive net_debt with negative EBITDA rejected with specific `positive_net_debt_negative_ebitda` reason. Fixes the NVDA/GOOGL/AAPL/META/BRK.A class of false rejection. Downstream grep audit confirmed no consumer misinterprets the 0.0 sentinel. | +6 (net; 2 existing tests updated in-place) |
| `329d16f` | C4: Phase 4 vol/event armor. **First screener phase with live IBKR calls.** IVR gate via `ib.reqHistoricalDataAsync(whatToShow="OPTION_IMPLIED_VOLATILITY")` — Option D per probe verification 2026-04-11 (AAPL 249 bars, MSFT 250 IVR 95.7%, SPY 250 IVR 23.8%, zero subscription errors). Corporate calendar gates via `YFinanceCorporateIntelligenceProvider` (earnings blackout 10d, ex-div blackout 5d, pending corp action). Two separate try/except blocks for IBKR vs calendar attribution. AST guard amended: `IBKR_WHITELIST += {"vol_event_armor.py"}`. `chain_walker.py` was pre-reserved in C1. | +32 |
| `9425b29` | C5: Phase 5 IBKR option chain walker. Routes through `agt_equities.ib_chains.get_expirations` + `get_chain_for_expiry` — does NOT call ib_async directly. Walks two nearest Friday expiries per ticker (MIN_DTE 2, MAX_DTE 21). Puts only (right='P'). Original strike floor was `candidate.lowest_low_21d`. `StrikeCandidate` dataclass with all 40 upstream fields + expiry/dte/strike/bid/ask/mid/last/volume/OI/IV/annualized_yield/otm_pct. AST guard required zero amendment — `chain_walker.py` already in IBKR_WHITELIST from C1. | +23 |
| `040c939` | C6: Phase 6 RAY band filter. SYNC function (first screener phase without I/O). Pure list-to-list: StrikeCandidate → RAYCandidate. 30%/130% inclusive band. NaN-safe guard (`math.isnan`) to prevent silent NaN pass-through. Filter-only — does NOT sort, rank, or select winners. Return order matches input order. Screener 6 of 6 phases complete. | +20 |
| `b5885e4` | **C6.1: Phase 5 strike band fix** (paper-run hotfix). CHRW at $163.49 with 21-day low $160.45 failed in paper run — band was only $3.04 wide and IBKR returned zero strikes for that range. New interval-based floor: `spot - (expected_interval × 5)` where `expected_interval` is a CONSERVATIVE estimate ($2.50 under $25, $5.00 otherwise) intentionally overestimating OCC theoretical grids. `lowest_low_21d` remains in the dataclass chain as carried-forward metadata but is no longer used as a strike filter. **PUSHED to origin/main** (first screener commit pushed per dispatch instruction). | +6 |
| `2211e28` | **Infra** (not a screener phase commit). `scripts/paper_run_screener.py` — end-to-end verification harness that runs the 6-phase pipeline against live Finnhub + yfinance + IB Gateway. Produced the Phase 1-4 verified paper run on 2026-04-11 documented in `HANDOFF_ARCHITECT_v17`. Surfaced the CHRW narrow-band failure that C6.1 fixed. Authored by Architect directly. | 0 |

**Screener pipeline at C6.1 (runnable from a Python REPL, orchestrator wiring C7+):**

```python
universe = await run_phase_1(finnhub_client)                        # Phase 1
tech_out = run_phase_2(universe)                                    # Phase 2 → Phase2Output
fund_cands = run_phase_3(tech_out.survivors)                        # Phase 3
corr_cands = run_phase_3_5(
    fund_cands, tech_out.price_history, current_holdings,
)                                                                    # Phase 3.5
vol_cands = await run_phase_4(corr_cands, ib, calendar_provider)    # Phase 4
strikes = await run_phase_5(vol_cands, ib)                          # Phase 5
rays = run_phase_6(strikes)                                          # Phase 6 (sync)
```

Expected output shape: 5-30 `RAYCandidate` entries drawn from 2 nearest Friday expiries across ~8-15 Phase 4 survivors. C6.2 (next sprint) fixes NaN-safe coercion in `ib_chains.py` to resolve the GD/HSY/MPC class of paper-run failure (IBKR error 10091, subscription edge cases now paid).

---

## In Flight

**Screener C6.2 (next sprint)** — NaN-safe coercion in `agt_equities/ib_chains.py`.
- Target: resolve the GD/HSY/MPC class of paper-run failure from 2026-04-11. CHRW was fixed in C6.1 (strike band width). GD/HSY/MPC failed with IBKR error 10091 (OPRA subscription missing) which is now paid/live next week — but the defensive coercion in `ib_chains.py` still needs to handle edge cases where `reqMktData` returns `None`, `nan`, or invalid types for bid/ask/last/volume/OI/IV fields.
- Pre-flight for Coder: verify whether `tests/test_ib_chains.py` exists before Architect dispatches C6.2. Report presence/absence to Architect.
- Out of scope: live paper-run retry. That happens after C6.2 lands + OPRA subscription is live.

**P3.2-alt Read-Only Live** — Day 2 findings + V2 execution engine shipped. Kill-switch live test pending (watchdog seeded).
- Protocol: `protocols/P3_2alt_read_only_live_protocol.md`
- Git tag: `p3.2alt-start` at `04c1d20`
- DB backup: `agt_desk.db.p3.2alt.bak`
- Kill-switch: triple-gate verified (env OFF + DB disabled=1 + _HALTED=False)
- mode_transitions: seeded 3 OVERWEIGHT rows (ADBE×2, PYPL×1) backdated to 2026-04-01 for watchdog staging test
- R9 runtime harness (`scripts/debug_r9.py`, throwaway): confirmed R9 fires RED both households, red_alert_state ON with conditions A+B
- V2 execution: Yield walkers use mid price, rolls staged as BAG combos in `pending_orders`, operator executes via `/approve`

**Screener orchestrator (C7, separate sprint after C6.2)** — wiring the 6-phase pipeline to `/scan` in `telegram_bot.py`. Output is Markdown table via `reply_text(parse_mode="Markdown")`. Scheduled daily refresh at 06:00 ET alongside existing R7 corporate intel refresh. Single-ticker mode `/screener TICKER` for interactive checks. C8 optional: PXO cross-check layer.

---

## Followups (logged, NOT in scope)

| # | Item | Filed by | Priority |
|---|------|----------|----------|
| 7 | Mobile keyboard ticker strip leniency | β Impl 2b | Post-paper |
| 10 | R5 auto-discovery pipeline | β Impl 5 survey | Post-paper |
| 11 | `/sell_shares` Telegram command | β Impl 5 | Post-paper |
| 14 | ~125 reply_text sites need paper/mode prefix middleware | Sprint 1D | Post-paper |
| 19 | Flex statement reconciliation for cross-midnight orphans | #17 | Post-paper |
| 20b | STK_SELL form/handler for originating_account_id | F20 | Post-paper |
| 25 | TOCTOU deferred sites (beyond share ledger) | CLEANUP-6 survey | Post-paper |
| 26 | fill_qty should be cumulative not last-write-wins | Sprint B Unit 9 | Post-paper |
| 33 | `_build_cure_data` SSOT consolidation (gated on PortfolioState reconciliation) | Sprint C2 survey | Post-paper |
| 34 | DeskSnapshot extensions (MarketSnapshot/ModeSnapshot decision) | Sprint C2 survey | Post-paper |
| 35 | `_discover_positions` swap (gated on #34) | Sprint C2 survey | Post-paper |
| 36 | Dead helper sweep post-Sprint C (get_portfolio_nav, load_active_cycles, get_betas) | Sprint C2 | Post-paper |
| 39 | Dedupe Underwater Positions rendering into shared Jinja macro | G2 | Post-paper |
| 9 | Intraday delta watermark reconciliation gap (per-row reconciliation flag on fill_log long-term fix) | ADR-006 STEP 0 | Low now, Medium-High before multi-tenant |
| 10 | `cc_decision_log` V2 router audit wiring (structured per-decision audit trail) | ADR-006 Blocker C | Low (`v2_rationale` text field is fallback) |
| 11 | Empirical verification of `EXPIRE_WORTHLESS` `ev.net_cash` values (H1 dead code vs H2 load-bearing) | ADR-006 DIFF 1 triage | Low (hygiene) |
| — | R4 pair mapping (glide_paths needs ticker_a/ticker_b ALTER) | Sprint 1F | Post-paper |
| — | run_polling() → manual PTB init (if event loop starvation observed) | Sprint B Unit 5 | Post-paper |
| — | Remaining query cursor hygiene migration | Sprint B Unit 7 | Post-paper |
| — | Dead handler function BODIES still in telegram_bot.py (~3000 lines) | Cleanup A | Post-paper |
| — | Screener log-level normalization (Phase 3 filter_fail warning→info, Phase 3.5 ALREADY_HELD warning→info, Phase 1 drop-path audit for remaining 4 paths) | C3.6 Delta 2 | Low, post-C6.2 |
| — | Screener `cc_decision_log` integration for `/scan` decision audit trail (if structured queries become needed) | C3.6 planning | Low |
| — | Screener Phase 1 heartbeat cache instrumentation parity review — ensure Phase 2/3/3.5/4/5/6 all surface cache hit rate in their final log lines | C2.1 pattern | Low, polish |

**Closed followups:** ~~#2~~ R7 cache (Sprint 1F), ~~#4~~ yf_tkr regression, ~~#8~~ Gate 1 dedup (Sprint B), ~~#9~~ Connection leak, ~~#13~~ Cross-await refactor, ~~#17~~ orderRef linking, ~~#20~~ Sub-account routing, ~~#23~~ Graceful shutdown

---

## Active Gotchas / Don't-Touch List

1. **R8 stub** — returns PENDING. Real R8 infrastructure (Gate 1/2, orchestrator, campaigns) lives alongside.
2. **R9 compositor WIRED** (Sprint B) — reads softened evals, fires RED on 2+ simultaneous conditions. REPORTING ONLY — does NOT trigger mode transitions.
3. **R7 (Earnings Window)** — REAL evaluator. FAIL-CLOSED. Override via `/override_earnings`. Daily 05:00 cache refresh.
4. **R5 (sell gate)** — REAL evaluator + staging function. Gate via `evaluate_rule_5_sell_gate()`.
5. **IBKRProvider DEPRECATED** — New code must use 4-way ISP providers or state_builder.
6. **AMBER Smart Friction = PEACETIME flow** — Intentional. No Integer Lock in AMBER.
7. **TRANSMITTING state** — intermediate lock between ATTESTED and TRANSMITTED. Orphan scan resolves on restart.
8. **`_pre_trade_gates()`** (Sprint 1A) — 5 gates: halt, mode, notional ($25k), non-wheel, F20 NULL. Wired at all 3 placeOrder sites.
9. **`_HALTED` flag** (Sprint 1D) — `/halt` sets flag, cancels all jq jobs, blocks all gates. Restart required to resume.
10. **Trust-tier cooldown** (Sprint 1D) — 10s (T0), 5s (T1), 0s (T2) via `AGT_TRUST_TIER` env. CancelledError → row stays ATTESTED.
11. **STAGED coalescing** (Sprint 1D) — 60s buffer, 15s flush job. Critical alerts bypass buffer.
12. **Paper mode** (Sprint 1C) — `AGT_PAPER_MODE=1`, ports 4002/7497, `[PAPER]` prefix, nickel/dime rounding, blue banner.
13. **Cold-start pin** (Priority 4) — startup checks live `accountSummaryAsync()` + live spots and pins into WARTIME if any household leverage is >= 1.50x. No reset-to-PEACETIME behavior remains.
14. **Beta cache** (Sprint 1F) — daily 04:00 refresh, startup run if empty. Both deck and rule engine read from beta_cache table.
15. **EL snapshots** (Sprint 1B) — 30s writer job in bot, deck reads from table. Health Strip polls every 10s.
16. **Glide-path softening** (Sprint 1F) — paused evals softened to GREEN before template render and R9 compositor.
17. **Mode transition idempotency** (Sprint 1F) — `log_mode_transition()` no-ops when old==new.
18. **seed_baselines NULL dedupe** (Sprint 1F) — DELETE before INSERT for NULL-ticker glide path rows.
19. **Operational DDL in schema.py** (Cleanup A) — `register_operational_tables(conn)` called from init_db. 20 tables migrated.
20. **risk.py relocated** (Sprint B) — canonical at `agt_equities/risk.py`, re-export stub at `agt_deck/risk.py`.
21. **Gate 1 canonical** (Sprint B) — `_stage_dynamic_exit_candidate` calls `evaluate_gate_1()`, no inline math.
22. **DEX encumbrance** (Sprint B) — `_discover_positions` reads STAGED/ATTESTED/TRANSMITTING from bucket3_dynamic_exit_log.
23. **Cursor hygiene** (Sprint B) — `_fetchall()`/`_fetchone()` helpers in queries.py for explicit cursor.close().
24. **Reconnect verify** (Sprint B) — accountSummaryAsync() called after auto-reconnect, logged.
25. **DeskSnapshot** (Sprint C1) — `build_state()` returns frozen `DeskSnapshot` (NAV, cycles, betas, DEX encumbrance, optional live_positions). IB-free, pure DB read path. `build_top_strip` is the first consumer (Sprint C2).
26. **config.py centralized** (Sprint C pre-step + D) — HOUSEHOLD_MAP, ACCOUNT_TO_HOUSEHOLD, MARGIN_ELIGIBLE_ACCOUNTS, MARGIN_ACCOUNTS, PAPER_MODE all canonical in `agt_equities/config.py`. Paper-aware. All consumers import from config.
27. **Rule 6 dynamic** (Sprint D) — Vikram account derived from `MARGIN_ELIGIBLE_ACCOUNTS["Vikram_Household"][0]`, not hardcoded. Returns GREEN if config empty.
28. **Underwater Positions** (G2) — present on BOTH command_deck and cure_console. Grouped by household, dedicated CC column, ▼ sort indicator. Shared `_build_underwater_rows()` helper.
29. **Breathe animation** (G7) — `.breathe` class on `<header>`, cascades to `.num` children. `:not(.animate-pulse)` excludes WARTIME badges. Hover pauses, reduced-motion disables.
30. **Execution kill-switch** — triple-gate OR logic: env `AGT_EXECUTION_ENABLED` (default OFF) + `_HALTED` in-process + `execution_state` DB row. All 3 `placeOrder` sites wrapped with `assert_execution_enabled()`. AST guard test enforces. `/halt` persists to DB (survives restart). `/resume CONFIRM` clears.
31. **AGTFormattedBot** (PTB 22.7) — `ExtBot` subclass overrides `send_message` + `edit_message_text` to apply `_format_outbound`. Replaces monkey-patch broken by `TelegramObject._frozen` lockdown. Wired via `ApplicationBuilder().bot(AGTFormattedBot(...))`.
32. **`_build_cure_data` get_betas** (hotfix) — deferred import restored inside `_build_cure_data` after Sprint C2 removed it from `build_top_strip`. Followup #33 will plumb DeskSnapshot betas properly.
33. **dump_rules.py** — `scripts/dump_rules.py` standalone rule evaluator. Read-only, no IB, no telegram_bot. Consumes rule_engine + Walker + yfinance + DB. For P3.2-alt Day 1.4 smoke test.
34. **DEX revert helper** (Finding #10) — `_revert_transmitting_to_cancelled(audit_id, reason)` reverts TRANSMITTING→CANCELLED after Step 7 early-exit (gate failure or kill-switch). Idempotent (WHERE final_status='TRANSMITTING'). NOT used for TRANSMIT_IB_ERROR (intentionally sticky). `_dispatched_audits.discard` cleanup wired in both branches.
35. **`/cure` auto-detect** (Finding #12) — `_detect_deck_host()` priority: AGT_DECK_HOST env > UDP socket LAN auto-detect > 127.0.0.1 fallback. `AGT_DECK_PORT` env (default 8787).
36. **R7 conn forwarding** (Finding #4) — `evaluate_all` now passes `conn=conn` to `evaluate_rule_7`. Operator overrides in `bucket3_earnings_overrides` are now reachable. 10 overrides currently active (expires 2026-04-14).
37. **yfinance 1.2.0 compat** (Finding #4) — Provider extraction uses `isinstance(raw, datetime)` / `isinstance(raw, date)` dispatch. Silent `except Exception: pass` replaced with `logger.warning`.
38. **NAV 3-tier priority** (#43 + #43v2) — `build_state()` NAV: (1) `live_nlv` param → "live_injected", (2) el_snapshots <120s → "live_db", (3) master_log_nav → "flex_eod". `nav_source_by_account` on DeskSnapshot tracks provenance. `agt_deck/main.py` top-strip caller now wires `live_nlv_dict`.
39. **mode_transitions seed** (Day 2 live test) — 3 OVERWEIGHT rows (ADBE Yash, ADBE Vikram, PYPL Vikram) backdated to 2026-04-01 for watchdog calendar gate bypass. `days_overweight=8 >= 7` (EVERY_CYCLE tier).
40. **R9 runtime confirmed** (#5b survey) — R9 fires RED both households (Condition A: 2+ simultaneous R1 RED). `red_alert_state` table shows ON with conditions A+B. Cure Console banner rendering is separate display-layer issue, not rule engine bug.
41. **V2 Smart Yield Walk-Down** — R8 candidate engine respects Adjusted Cost Basis and 10% Anti-Rip floor. V1 emergency kill-switch logic archived to `agt_equities/archive_wartime_v1.py`.
42. **Defensive Roll Engine** — `evaluate_defensive_rolls()` in rule_engine.py. Triggers at 0.40 Delta inflection, 98% proximity, or Friday trap. Level 1: Roll Up-and-Out. Level 2: Same-strike time buy. Level 3: CRITICAL_ALERT. All credit calculations use mid-to-mid pricing.
43. **`_scan_and_stage_defensive_rolls`** — Injected into 3:30 PM watchdog (after all DB housekeeping). Scans short calls via `reqPositionsAsync`, fetches Greeks/Ask/Bid, computes short_mid, stages BAG combo tickets to `pending_orders`. Human-in-the-loop: operator executes via `/approve`.
44. **`_build_adaptive_roll_combo`** — BAG combo order builder. Action=BUY, negative limit price ensures net credit. Adaptive Urgent priority. Used by `_place_single_order` when `sec_type=BAG`.
45. **`_place_single_order` BAG routing** — Dispatches on `sec_type`: OPT → standard CC flow (duplicate + capacity checks), BAG → combo roll flow (capacity checks bypassed — rolls are net-zero). Both paths share `_pre_trade_gates` + `assert_execution_enabled`.
46. **Yield walker mid pricing** — `_walk_mode1_chain` and `_walk_harvest_chain` compute `mid = (bid+ask)/2`, use mid for annualized yield calc, output mid as `"bid"` key (preserves downstream schema). Adaptive Patient priority.
47. **Cold-start wartime pin** (`532cb7c`) — Bot checks live leverage on startup. Pins to WARTIME when leverage >= 1.50x. Prevents false PEACETIME on restart during margin stress.
48. **ADR-005 V2 router site** (`729c5ba`) — `_place_single_order` reads `payload["origin"]` to pick gate site: `v2_router` (WARTIME-whitelisted) vs `legacy_approve` (WARTIME-blocked). STATE_2 HARVEST and STATE_3 DEFEND ticket dicts built in `_scan_and_stage_defensive_rolls` include `origin="v2_router"`, `v2_state`, and `v2_rationale` fields for audit. Notional gate semantics: OPT BUY = `qty * lmtPrice * 100` (cash-paid), OPT SELL = `qty * strike * 100` (strike-notional), BAG = `qty * abs(lmtPrice) * 100`, STK = `qty * lmtPrice`. Zero qty and unsupported secType fail-closed.
49. **ADR-006 per-account ACB** (`b874d81`) — `Cycle._premium_by_account` dict mirrors `_paper_basis_by_account`. All 9 `cycle.premium_total +=` sites in `walker.py:_apply_event` paired with `_credit_premium_to_account(cycle, ev.account_id, ev.net_cash)` per ruling R1 (no EXPIRE_WORTHLESS exception — universal). `Cycle.premium_for_account()` and `Cycle.adjusted_basis_for_account()` accessors produce per-account IRS-correct basis. `_load_premium_ledger_snapshot(household, ticker, account_id=None)` returns per-account dict when `account_id` provided + `READ_FROM_MASTER_LOG=True`; fails closed to None on legacy path when per-account is requested (Act 60 compliance). V2 router `_scan_and_stage_defensive_rolls` passes `pos.account` to the lookup.
50. **Intraday delta watermark** (`b874d81`) — `trade_repo.get_active_cycles_with_intraday_delta()` overlays `fill_log` entries with `created_at > MAX(last_synced_at)` from `master_log_trades` as the watermark source (`flex_sync_log` table does not exist — verified during ADR-006 pre-flight). Known reconciliation gap logged as Followup #9: if Flex reporting lags a same-day fill, the fill becomes invisible to the walker intraday view for T+1→T+2 window. Acceptable for single-machine ops; needs per-row reconciliation flag before RIA multi-tenant.
51. **Screener is read-only and isolated** — `agt_equities/screener/` is a side project that surfaces wheel-eligible CSP candidates. Zero coupling to execution paths. AST guard at `tests/test_screener_isolation.py` enforces that no screener file imports `telegram_bot`, `_pre_trade_gates`, `placeOrder`, `execution_gate`, `_HALTED`, V2 router symbols, `walker`, `trade_repo`, `rule_engine`, or `mode_engine`. `ib_async` is whitelisted ONLY for `vol_event_armor.py` (Phase 4 IVR) and `chain_walker.py` (Phase 5 chain walk, routed through `agt_equities.ib_chains`). Phase 5 uses `ib_chains.get_expirations` / `ib_chains.get_chain_for_expiry` — does NOT call `reqMktData` / `reqSecDefOptParams` / `qualifyContractsAsync` directly.
52. **Screener Phase 5 strike floor is interval-based, not price-history** (`b5885e4`, C6.1) — Strike band lower bound is computed as `spot - (_expected_strike_interval(spot) × CHAIN_WALKER_MIN_STRIKES_IN_BAND)` where the interval is a CONSERVATIVE estimate ($2.50 under $25, $5 otherwise, intentionally overestimating OCC theoretical grids). Guarantees at least 5 walkable strikes regardless of chain density. `lowest_low_21d` remains a carried-forward dataclass field on `VolArmorCandidate` → `StrikeCandidate` → `RAYCandidate` but is NOT used as a filter. `_compute_strike_band_floor()` takes ONLY `spot` as input — verified structurally via `inspect.signature` in tests.
53. **Screener Phase 3 net-cash sentinel** (`6f9744a`, C3.7) — `_compute_net_debt_to_ebitda` returns 0.0 sentinel when `net_debt <= 0` (total_debt ≤ cash) regardless of EBITDA sign. Fixes the mega-cap tech class of failure (NVDA, GOOGL, AAPL, META, BRK.A). Positive net_debt with negative EBITDA rejected with specific reason `degenerate_denominator:positive_net_debt_negative_ebitda`. Only arithmetic consumer of `net_debt_to_ebitda` is Phase 3's `_passes_fortress_filters` gate — the 0.0 sentinel passes the `<= 3.0` check correctly. Downstream phases (3.5, 4, 5, 6) only carry the field forward, they don't read it.
54. **Screener Phase 3 SI fail-open** (`6f9744a`, C3.7) — When both `info.shortPercentOfFloat` and the `sharesShort/floatShares` fallback are None, `_extract_fundamentals` logs `SHORT_INTEREST_UNAVAILABLE` warning and sets `short_interest_pct = 0.0`. Yfinance has SI=None for ~20-30% of tickers; fail-closed would silently starve the candidate pool.
55. **Screener universe exclusions** (`b1404df`, C3.6) — `config.EXCLUDED_SECTORS` contains 22 entries: 3 quality exclusions (Airlines, Biotechnology, Pharmaceuticals) + 19 structural exclusions (REITs in 6 variants, MLPs + Oil & Gas Storage bucket, BDCs + Asset Management & Custody Banks bucket, Closed-End Funds, Trust/Trusts/Royalty Trusts, SPACs/Blank Checks/Shell Companies). Bucket-level exclusions (Oil & Gas Storage, Asset Management) are intentionally aggressive — they strip MLPs/BDCs plus some legitimate C-corps (BLK, TROW). Trade-off: cost of false negative (missing BLK) < cost of false positive (admitting ARCC whose fundamentals break Phase 3 math).
56. **Screener Phase 6 NaN guard** (`040c939`, C6) — `ray_filter.run_phase_6` uses `math.isnan()` explicitly in the malformed-data branch. Without it, a NaN `annualized_yield` would fall through all three comparison branches (NaN comparisons all return False) and silently pass as in-band. Test 12 locks the guard semantic.
57. **IBKR historical IV subscription** (probe verified 2026-04-11) — `reqHistoricalDataAsync` with `whatToShow="OPTION_IMPLIED_VOLATILITY"` returns ~250 daily bars for US large-caps on the paid subscription. Probe at `scripts/probe_ibkr_historical_iv.py` ran against live Gateway at port 4001 with `clientId=99` (no collision with production bot's clientId=1). Verdict `SUBSCRIBED` unlocked Option D for C4. Throwaway script, uncommitted.

---

## Backup System

- **Code -> GitLab** `git@gitlab.com:agt-group2/agt-equities-desk.git` (SSH `@yashpatil1`)
- **GitHub mirror** — push mirror from GitLab (verified 2026-04-08)
- **Auto-push** at end of every successful `flex_sync.py` run
- **DB -> Cloudflare R2** via Litestream, continuous, 30-day retention
- **Friday handoff archive** via `scripts/archive_handoffs.py`
- **NEVER commit:** `.env`, `.deck_token`, `*.db`, `*.db-wal`, `*.db-shm`, `audit_bundles/`, `data/inception_carryin.csv`, `Archive/`, `.venv/`, `.hypothesis/`, `.claude/`, Litestream WAL segments

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
- **Uncommitted work detected at sprint boundary** — commit + push before proceeding

---

## How to Pick Up (new session ritual)

1. Read this file end-to-end.
2. Read `desk_state.md` at `C:\AGT_Telegram_Bridge\desk_state.md`.
3. Run `git log --oneline -12` to confirm HEAD matches expected state. Expected top-of-tree (2026-04-11):
   ```
   b5885e4  screener C6.1: widen Phase 5 strike band ...        (origin/main)
   040c939  screener C6:   Phase 6 (RAY filter - terminal)
   9425b29  screener C5:   Phase 5 (IBKR option chain walker)
   329d16f  screener C4:   Phase 4 (IVR + corporate calendar)
   6f9744a  screener C3.7: Phase 3 semantic fixes
   b1404df  screener C3.6: structural exclusions
   570ce02  screener C3.5: correlation-fit portfolio gate
   56487aa  screener C3:   fundamentals
   0092b35  screener C2.1: cache instrumentation
   81af518  screener C2:   Phase 1 + Phase 2
   9e223f2  screener C1:   scaffolding
   3bb8c81  followups #9 #10 #11
   b874d81  ADR-006 ACB pipeline hardening
   729c5ba  ADR-005 V2 router WARTIME whitelist + BAG + BTC cash-paid
   ```
4. Wait for Architect prompt. Do not start work autonomously.

End of handoff.
