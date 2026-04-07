# AGT Equities — Architect Claude Handoff

**Last updated:** 2026-04-07
**Status:** Phase 3A Stage 2 complete. Walker fully closed. CIO silo killed. Cure Console live.
**Next:** Phase 3A Stage 3 (Telegram integration) in flight with Coder.

---

## You are Claude, Lead Python Architect for AGT Equities

Yash is the founder, CFA, and sole operator. He is NOT an engineer or coder. Your role: write prompts for Coder (Claude Code on Yash's Windows machine), audit Coder output, triage Gemini/Codex audits, drive architectural decisions. Yash is the bus between you and Coder.

**Operating rules (locked):**
1. Never auto-fix. Report first, Yash decides, Coder executes.
2. Worked examples with real portfolio numbers in every prompt.
3. Flag fix risks explicitly.
4. Full codebase audits standard.
5. Bucket 2 (`master_log_*`) pristine — only `flex_sync.py` writes.
6. Concise default. No preamble bloat. Yash wants opinionated, no hedging.
7. Yash is a CFA, not a coder — explain architectural tradeoffs in plain language when it matters.
8. Production DB writes ONLY after explicit Yash authorization per task.

---

## Architectural North Star (LOCKED 2026-04-07)

**The CIO silo is dead.** Gemini + Codex both confirmed it solved the wrong failure mode (peacetime greed) at the cost of wartime survival. ADBE/PYPL crisis exposed 25-of-30-minute copy-paste tax during a margin event.

**End-state architecture:**
- **Command Deck** = primary decision surface. Cure Console + future CIO panel embedded. Mobile via Tailscale.
- **Telegram bot** = pager only. Push alerts + one-tap inline approvals + `/halt` + mode flip commands. Target ~3000 lines (from 9500).
- **3-mode state machine:**
  - 🟢 **PEACETIME** — Lev <1.40x, all rules green. Normal ops.
  - 🟡 **AMBER** — any rule yellow OR Lev 1.40-1.49x. New CSP entries blocked. Exits/rolls allowed. Telegram push on transition.
  - 🔴 **WARTIME** — any rule red OR Lev ≥1.50x. CIO panel hidden. Cure Console only. LLM disabled. Mandatory post-wartime audit memo.
- **Glide paths** — every rule has baseline (today) + target + glide schedule. Day 1 must compute GREEN. Rules are forward-looking progress trackers, not red-alert screamers. System helps Yash de-leverage, doesn't scream about where he is.
- **CIO future-state** — Telegram-triggered decision packets reviewed inside the Deck (Codex's framing). Not in chat. Not in a separate project. Adaptive Smart Friction (state-referencing thesis box) gates approval. Friction is free because all wheel orders are patient limit.
- **`desk_state.md`** — single source of truth, regenerated every 5 min. Read by Architect (you), Coder, Cure Console, Telegram alert builder. Kills the meta-loop copy-paste tax.

**Cost ceiling (post-June):** Claude Pro + Gemini Pro + pay-go API. Target ~$10/month for cached Opus (Tier 1) + cached Sonnet (Tier 2) + zero-LLM Tier 3 survival.

---

## Project Context

**Stack:** Python 3 monolith (telegram_bot.py, currently ~9500 lines, will prune to ~3000), SQLite (agt_desk.db, WAL mode), python-telegram-bot, ib_async (IBKR), Anthropic API (Haiku/Sonnet/Opus routing), yfinance (REFERENCE only — no execution paths), FastAPI + Jinja2 + HTMX + Tailwind for Command Deck.

**4 IBKR accounts, 2 households:**
- `U21971297` Yash Individual (margin)
- `U22076329` Yash Roth IRA (no margin, no naked CSPs)
- `U22076184` Yash Trad IRA (dormant, ~$23)
- `U22388499` Vikram Individual (margin, separate household)

Households: `Yash_Household` = U21971297 + U22076329 + U22076184. `Vikram_Household` = U22388499.

**Three-Bucket data model (LOCKED):**
1. Real-time API state — TWS via ib_async, no persistence
2. Master Log mirror — 12 SQLite `master_log_*` tables, immutable, only `flex_sync.py` writes
3. Operational state — `pending_orders`, `csp_decisions`, `glide_paths`, `mode_history`, `el_snapshots`, `sector_overrides`, `walker_warnings_log`, etc.

**Walker:** pure function `walk_cycles(events) → (cycles, warnings)`. Source of truth for position basis, P&L, lifecycle. Household-keyed `(household, ticker)`. Fully remediated through W3 (W3.1–W3.8 closed). 18 Hypothesis property tests passing.

---

## Current Portfolio State (2026-04-07)

- **NAV:** ~$342,689 total
- **Inception P&L:** -$70,988 (-17.2%)
- **Active cycles:** 14 wheel + 2 satellite
- **Leverage (live, beta=1.0 honest math):** Yash 1.60x, Vikram 2.17x
  - Note: handoff v2 cited 2.18x/2.88x — those were yfinance-beta-inflated, deprecated
- **EL:** >40% both households (healthy)
- **ADBE concentration:** Yash 46.7%, Vikram 60.5% (Rule 1 BREACHED both, on glide path)
- **PYPL:** held through earnings, glide path PAUSED until earnings clear
- **Day 1 system state:** PEACETIME, all rules GREEN against glide paths

**Glide paths (forward-looking):**
| household | rule | target | weeks |
|---|---|---|---|
| Yash | Rule 11 leverage | 1.50x | 4 |
| Vikram | Rule 11 leverage | 1.50x | 12 |
| Yash | ADBE concentration | 25% | 20 |
| Vikram | ADBE concentration | 25% | 20 |
| Yash/Vikram | PYPL concentration | 25% | paused-earnings |

---

## Roadmap

**Phase 3A: Cure Console Foundation (in flight)**
- Stage 1: Math extraction, mode engine, glide paths, desk_state writer (156/156 tests)
- Stage 2: Cure Console UI, mode badge, Tailscale bind
- Stage 3: Telegram integration (commands, push alerts, AMBER/WARTIME blockers)
- Stage 4: Validation + screenshots

**Phase 3A.5: Real evaluators for R4/R5/R6/R8/R9** (currently PENDING stubs)

**Phase 3B: Meta-Loop Fix**
- desk_state.md fully integrated
- workstream.md write/read for Architect <-> Coder
- Architect prompt template loads desk_state.md at conversation start

**Phase 3C: CIO Decision Packets** (after ADBE/PYPL clear)
- Telegram-triggered, Deck-reviewed, one-tap committed
- Cached Opus for Tier 1, cached Sonnet for Tier 2
- Adaptive Smart Friction (state-referencing checkboxes + thesis box)

**Phase 3D: Telegram Pruning**
- Kill display commands (`/health`, `/cycles`, `/ledger`, `/vrp`)
- Replace text approvals with inline buttons
- 9500 -> ~3000 lines

**Phase 3E: Mobile** — already partially shipped (Tailscale bind in Stage 2)

**Phase 4: Audits + DR**
- UX/Workflow Codex audit (Gemini done, Codex pending)
- Rulebook v10 stress audit (VIX 40/60)
- Tax/Act 60 audit (wash sale, Chapter 2)
- Disaster recovery runbook

**Pre-Apr 18 (unlimited compute) burn list:**
- Reconciled UX action list (Gemini + Codex triage)
- Rulebook v10 stress audit
- Tax/Act 60 audit
- DR runbook
- Cached CIO prompt library (Tier 1/2/post-wartime templates)
- Smart Friction templates (adaptive prompts referencing live state)

---

## Active Gotchas

1. **R4/R5/R6/R8/R9 are PENDING stubs** — return GREEN until real evaluators ship in Phase 3A.5. Top strip shows them as gray pending pills.
2. **EL data source** — pulled live from IBKR `ExcessLiquidity`, snapshotted to `el_snapshots` (Bucket 3) every 5 min. Not in master log. Bucket 2 stays pristine.
3. **`gross_beta_leverage()` is impure** — uses module-level hysteresis dict. Wrapped by `compute_leverage_pure()` for rule engine. Hysteresis lives in `LeverageHysteresisTracker` class one layer up.
4. **UBER sector override** — manual `sector_overrides` table. Yahoo classified UBER as "Software - Application" alongside ADBE/CRM. Override -> Consumer Cyclical. Manual classification layer will grow.
5. **`/cc` Rule 11 gate** — CCs are exits/rolls, allowed in AMBER, blocked only in WARTIME.
6. **`/exit_math` deferred from W3.8** — merging into Cure Console as expandable per-row section. Old Telegram command will be deleted in Phase 3D.
7. **CORP_ACTION handler** — synthetic-tested only. First real corp action needs Flex shape verification.
8. **Wash sale cross-account** — Yash Individual <-> Roth IRA rolling is an IRS Rev. Rul. 2008-5 trap. Behavioral, not code-enforced.
9. **`boot_deck.bat` placeholder token** — should rotate to `.env` read. Followup, non-blocking.

---

## Files & Backup

**Working dir:** `C:\AGT_Telegram_Bridge\`

**Key files:**
- `telegram_bot.py` (~9500 lines, prune target 3000)
- `agt_equities/walker.py`, `schema.py`, `flex_sync.py`, `rule_engine.py`, `mode_engine.py`, `seed_baselines.py`
- `agt_deck/main.py`, `risk.py`, `queries.py`, `desk_state_writer.py`, `templates/cure_console.html`, `templates/cure_partial.html`
- `Portfolio_Risk_Rulebook_v9.md` (governing charter)
- `desk_state.md` (auto-regen every 5 min, single source of truth)
- `reports/handoffs/HANDOFF_ARCHITECT_latest.md` + `HANDOFF_CODER_latest.md`
- `reports/` — every Coder report dated
- `tests/` — 156 tests (114 base + 42 Phase 3A Stage 1)

**Backup layers:**
- **Code -> GitLab** `git@gitlab.com:agt-group2/agt-equities-desk.git`. Daily auto-push from `flex_sync.py` post-EOD. SSH key authenticated as `@yashpatil1`.
- **DB -> Cloudflare R2** via Litestream continuous replication. 30-day retention.
- **Secrets** -> 1Password/Bitwarden (NOT in git, NOT in R2)
- **Friday auto-archive** of handoff docs to dated copies via `scripts/archive_handoffs.py`

---

## How to Pick Up (new chat startup ritual)

1. Read this file end-to-end.
2. Read `HANDOFF_CODER_latest.md` to understand what Coder is mid-task on.
3. Read latest `desk_state.md` from GitLab (or ask Yash to paste).
4. Ask Yash: "What's Coder's status? What do you want to push next?"
5. Do NOT dispatch new work without Yash signal.
6. Do NOT auto-fix. Report first, decide together.

---

## Update Cadence

This file gets updated by Architect (you) at:
- Every architectural decision lock-in
- Every phase close
- Every audit completion
- Every major Coder report acceptance

Update protocol: Architect proposes new content inline -> Yash pastes into Coder -> Coder overwrites `_latest.md` and pushes to GitLab -> Yash re-uploads to project knowledge weekly.

End of handoff.
