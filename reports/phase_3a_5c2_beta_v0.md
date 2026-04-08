# Phase 3A.5c2-β v0 — Smart Friction UI Consolidation

**Date:** 2026-04-07
**Status:** v0 — pre-survey consolidation. Survey-first dispatch follows.
**Supersedes:** Scattered β scope across HANDOFF_CODER_latest, phase_3a_5c2_discovery_20260407 §3-11, ADR-004.
**Predecessor:** Phase 3A.5c2-α COMPLETE at `ec0ea3a` (327/327 tests, live IBKR 8/8 PASS).
**Governing rulebook:** v10. Implementation specs governed by ADR-004.

---

## 1. Context

α shipped the backend chokepoint: STAGED-row infrastructure, R7 fail-closed evaluator, R9 condition D, watchdog migration from CIO payload to `_stage_dynamic_exit_candidate()`, IBKRProvider deprecation. The STAGED rows in `bucket3_dynamic_exit_log` are dead inventory until β ships the consumer surface.

β = the Smart Friction UI that turns STAGED rows into transmittable orders via Operator Attestation per ADR-004.

This is the most security-critical sprint in the codebase to date. β contains the first code path that writes to live IBKR after Yash approval. Survey-first is binding.

---

## 2. α-Shipped — DO NOT RE-IMPLEMENT

The following are complete and verified at `ec0ea3a`. β consumes them; β does not modify them without explicit Architect ADR amendment.

| Component | α Anchor | Notes |
|---|---|---|
| `bucket3_dynamic_exit_log` schema | α | Patch 1 applied: strike/expiry/contracts NULLABLE for STK_SELL, campaign_id, household_nlv, underlying_spot_at_render |
| `dynamic_exit_campaigns` schema | α | Conviction modifier + target_shares state lock per Gemini Q1 |
| `evaluate_gate_1()` | α | Hardcoded conviction modifiers (HIGH 0.20 / NEUTRAL 0.30 / LOW 0.40), tax_liability_override default $0.00, projected post-exit margin via whatIfOrder + haircut fallback |
| `evaluate_gate_2()` | α | 33% PEACETIME / 25% AMBER+WARTIME |
| `_stage_dynamic_exit_candidate(source=...)` | α (Task 6) | Single chokepoint, source-tagged, per-candidate transactions, 15-min TTL → ABANDONED sweeper |
| Watchdog STAGED-row write | α (Task 6) | Replaces lines 10168-10255 CIO payload generation in `_scheduled_watchdog` |
| R7 fail-closed evaluator + `/override_earnings` | α (Task 9) | Three branches: override → cache → RED. Per-ticker GLOBAL across accounts, 720h max TTL |
| R9 condition D evaluator (2-of-4 fire / all-4 clear) | α (Task 16) | `IOptionsChain.get_chain_slice()` consumer |
| `/exit` deletion | α (Task 11) | `/exit_math` preserved for Phase 3D scope |
| IBKRProvider deprecation | α (Task 10) | DeprecationWarning + 5 call sites migrated. MarketDataProvider ABC retained pending state_builder refactor |
| 3-mode state machine | α | PEACETIME/AMBER/WARTIME, AMBER blocks new CSP, allows exits/rolls |

**Known α gaps carried into β (filed, not blocking):**
- Gate 1 DB write path live-untested (Task 16 Step 5 hit reject before write). β surfaces it via Smart Friction end-to-end.
- yf_tkr fix (`85b24a6`) has no regression test. Followup #4 in v3 handoff.
- R7 earnings cache scheduled job (eliminates 14-cycle override ritual). Followup #2.

---

## 3. β Scope — Five Work Areas

### 3.1 Cure Console Dynamic Exit Panel

**New template:** `cure_dynamic_exit_panel.html` partial. Same card pattern as glide_paths section. Insertion point: after glide paths in `cure_partial.html`.

**Reads:** STAGED rows from `bucket3_dynamic_exit_log` where `final_status='STAGED'` AND `render_ts > now - 15min`.

**Renders per row:**
- Position metrics (shares, spot, value, household concentration %)
- Adjusted basis, gap to basis
- Conviction tier + modifier (with override path)
- Exit scope (target_shares, excess_shares, available contracts after CC encumbrance)
- Gate 1 candidates ranked by ratio across the option ladder
- Gate 2 sizing per candidate
- `[Begin Attestation]` button → loads Smart Friction modal via `hx-get`

**HTMX refresh:** Existing `/api/cure` 60s poll. New STAGED rows surface within one cycle. Optional Telegram pager notification per ADR-004 §1 for immediate awareness.

### 3.2 Smart Friction Modal (PEACETIME variant)

**New template:** `cure_smart_friction.html` partial. **First modal pattern in the Deck.** Tailwind `fixed inset-0 z-50` overlay + centered card. Vanilla JS for state (no Alpine.js dependency unless Architect amends).

**Endpoint:** `GET /cure/dynamic_exit/{audit_id}/attest` returns HTMX fragment.

**Form fields per ADR-004 §2 (60-second render staleness) + §3 (whole-dollar precision):**
- Hidden: `render_ts`, `audit_id`, all frozen gate math values
- Checkbox 1: "I acknowledge this trade locks in a -${loss:,d} unrecoverable loss"
- Checkbox 2: "I confirm the cure target: reduce {ticker} from {current_pct}% to {target_pct}%"
- Textarea: Adaptive thesis (see §3.4)
- `[STAGE]` button: DOM-disabled until checkboxes + thesis non-empty

**Submission handler `POST /cure/dynamic_exit/{audit_id}/attest` per discovery §7 + ADR-004 §2:**
1. `render_ts` freshness: `(server_now - render_ts) <= 60_000ms`. Fail → HTTP 409, swap STAGE button for red `[MATH STALE — REFRESH]`.
2. Checkbox states all TRUE. Fail → HTTP 422.
3. Thesis non-empty (PEACETIME). Fail → HTTP 422.
4. State transition: `final_status: STAGED → ATTESTED`.
5. Telegram push: inline keyboard `[TRANSMIT] [CANCEL]` with `callback_data=f"dyn_exit:{audit_id}:transmit|cancel"`.

### 3.3 Smart Friction Modal (WARTIME Integer Lock variant)

**New template:** `cure_smart_friction_wartime.html` (or polymorphic content in same template — Coder choice, document in survey).

Per ADR-004 §5:
- Hidden: same frozen values
- Gate 1/2 option math fields HIDDEN for STK_SELL action
- Integer Lock input: "Type the exact integer {loss} to authorize"
- `[STAGE EMERGENCY EXIT]` button: DOM-disabled until input strict-integer-equals `gate1_realized_loss` (no whitespace, no commas, no cents)
- Thesis textarea REMOVED from DOM entirely
- No checkboxes

**Fallback:** If loss rounds to $0 or $1, replace integer match with ticker-symbol exact-match (e.g. `PYPL`).

**Audit:** Typed value captured to `attestation_value_typed`. Mismatch attempts logged but not separately auditable.

### 3.4 Adaptive Smart Friction Thesis Copy

**Architect deliverable, not Coder.** Drafted pre-β dispatch.

Generic prompts ("explain your reasoning") become muscle memory and die. Adaptive prompts reference current portfolio state at render time:

> "ADBE is currently 46.7% of household, you're proposing to add 5% more — explain the thesis given this concentration"

Templates needed (Architect drafts a separate doc; Coder consumes via template variables):
- R8 Dynamic Exit (concentration-cure framing)
- R5 thesis deterioration (bearish-rationale framing)
- R5 emergency risk event (catalyst-logging framing)
- R6 forced liquidation (WARTIME — Integer Lock only, no thesis)

Friction is free because all wheel orders are patient limit. Operator awakeness > latency.

### 3.5 AMBER Blocker Semantics at Button Level

Per v10 + 3-mode state machine:
- `[TRANSMIT]` button greyed out for **CSP entries** in AMBER
- `[TRANSMIT]` button **active** for exits/rolls in AMBER
- `[TRANSMIT]` button greyed out for ALL non-emergency in WARTIME

Worth a dedicated test pass — this is not just a render-time check, it's a state-aware button semantic that must hold across both Cure Console and Telegram surfaces.

---

## 4. Pre-β Architectural Concerns — SURVEY TARGETS

### 4.1 TRANSMIT Handler — Dedicated Audit Pass

The Telegram `[TRANSMIT]` callback is the first place this system writes to live IBKR after Yash approval. Most security-critical code in the codebase. Deserves its own audit pass separate from normal Coder review.

**Architect mandate:** After β implementation lands, before any live verification, the TRANSMIT handler gets a Gemini Deep Think audit. Tier 1 reserve is depleted per v3 handoff — Yash greenlight required to spend.

### 4.2 JIT Re-Validation Precedence (Trickiest β Problem)

Per discovery §8 + ADR-004 §4:

Three failure modes the operator must distinguish:
1. **Stale row** — render_ts > 15 min, sweeper missed it (shouldn't happen, but defensive)
2. **Rule changed since stage** — desk_mode transitioned PEACETIME → AMBER, R7 cache went stale, override expired
3. **Portfolio moved against you** — live spot drift invalidates Gate 1

**Question for survey:** Which check fires first? What's the failure message contract? How does the operator distinguish the three?

**Architect lean (subject to survey):**
1. Mode transition check (cheapest, deterministic) → reject with "DESK MODE CHANGED: was PEACETIME at stage, now AMBER. Re-stage."
2. Rule re-evaluation (R7 override freshness, etc.) → reject with specific rule + reason.
3. Gate 1 re-eval against live spot (most expensive, requires `IPriceAndVolatility.get_spot()` + `IOptionsChain.get_chain_slice()`) → reject with old/new ratio.
4. Unmarketable drift check: `abs(live_mid - attested_limit_price) > 0.10` → reject.

Per ADR-004 §4 + Patch 5:
- 3-strike retry budget (PEACETIME only) → 5-min ticker lock
- WARTIME 3-strike DISABLED — operator can re-stage indefinitely

**This is the most likely candidate for Tier 1 Gemini Deep Think reserve spend.**

### 4.3 Smart Friction Copy as Writing Task

Architect drafts thesis templates pre-β. Coder consumes via template variables. Templates should not be hardcoded strings in Python — pull from a config file or templates dir for revision without code changes.

### 4.4 Walk-Away P&L Single Source of Truth — RESOLVED

**Discovery §16 was stale.** `walker.compute_walk_away_pnl()` was shipped in α at `walker.py:759` with `WalkAwayResult` dataclass and 4 unit tests. ADR-004's reference to this function is correct.

**β prep 1 (`16cd244`):** 3 inline reimplementations refactored to delegate to canonical function. Math agreement verified across all sites (all use adjusted basis). Validation: walk-away 4/4, Gate 1 8/8, dry_run 88/88.

β consumers import `compute_walk_away_pnl` from `agt_equities.walker` directly. No new utility module needed. Implementation order item #1 is complete.

### 4.5 Process Pattern Watch Carryover

Two α invariant deviations without STOP-and-surface (Task 6 per-candidate transactions, Task 16 yf_tkr fix mid-verification). Both technically correct, both kept. **Pattern is forming. A third occurrence in β triggers a process incident and Coder discipline re-scope.**

---

## 5. Implementation Order

Per ADR-004 §"Implementation Order" (subset, β-scoped):

1. Walk-away P&L extraction (architectural pre-req — see §4.4)
2. Cure Console Dynamic Exit panel template (read-only display of STAGED rows)
3. Cure Console Smart Friction widget template (PEACETIME checkbox flow)
4. Cure Console Smart Friction widget template (WARTIME Integer Lock variant)
5. HTTP 409 staleness check on form submission
6. Telegram `[TRANSMIT] [CANCEL]` inline keyboard handler
7. JIT Gate 1 re-validation in Telegram transmit handler (per §4.2 precedence)
8. 3-strike retry budget enforcement (PEACETIME) + WARTIME bypass
9. Drift block (`abs(live_mid - attested_limit_price) > 0.10`)
10. R5 sell gate wiring to same attestation flow
11. AMBER button semantics across Deck + Telegram
12. Tests (unit + integration, projection in §7)
13. Day 1 verification: full pipeline still PEACETIME, synthetic ADBE Dynamic Exit candidate renders correctly, audit log captures all fields
14. **TRANSMIT handler dedicated audit pass** (gated on §4.1)

---

## 6. Architect Review Queue

1. Walk-away P&L location: `agt_equities/walk_away.py` shared utility?
2. JIT re-validation precedence model: confirm 4-step order in §4.2 or escalate to Gemini Deep Think?
3. Smart Friction template variant: single template with WARTIME branching, or two templates (`cure_smart_friction.html` + `cure_smart_friction_wartime.html`)?
4. Alpine.js dependency for modal state, or vanilla JS only?
5. Adaptive thesis copy: Architect drafts when?
6. TRANSMIT handler audit: schedule Gemini reserve spend pre- or post-implementation?
7. R5 sell gate variant template (carryover from ADR-004 Open Question §5): same Smart Friction widget with different render template?
8. AMBER button semantic test pass: dedicated test file or fold into existing mode_engine tests?

---

## 7. Test Projection

β-scoped subset of discovery §14 (~91 total for full 3A.5c2; α already covered Gate 1/2 unit tests, watchdog scanner, schema/lifecycle, R9 condition D, R5 stage helper partial, R7 earnings, /override_earnings, IBKRProvider migration, Day 1 baseline regression).

| Category | Estimated Tests |
|---|---|
| Smart Friction submission handler (PEACETIME) | 10 |
| Smart Friction submission handler (WARTIME Integer Lock) | 6 |
| JIT re-validation (precedence, drift, 3-strike, WARTIME bypass) | 12 |
| Cure Console Dynamic Exit panel render | 5 |
| AMBER button semantics (Deck + Telegram) | 6 |
| Walk-away P&L shared utility | 4 |
| R5 sell gate end-to-end | 5 |
| TRANSMIT handler integration | 8 |
| Day 1 baseline regression | 2 |
| **β total new** | **~58** |
| **Running total** | **327 + 58 = ~385** |

---

## 8. Followups Carried Forward (filed, not blocking β)

Per HANDOFF_ARCHITECT_v3:

1. Gate 1 DB write path live-untested → β Smart Friction end-to-end exercises this naturally.
2. yf_tkr regression test for `_stage_dynamic_exit_candidate()` (commit `85b24a6` has no test).
3. R7 earnings cache scheduled job (eliminates 14-cycle override ritual) — Phase 3A.5c3 or β-side opportunistic.
4. R8, R10 still stub — track in β scope discussion if R8 evaluator semantics affect Cure Console rendering.
5. Phase 3D Telegram pruning (~9500 → 3000 lines) — wait until β stabilizes.
6. Phase 4 audits: UX Codex, v10 stress audit, Tax/Act 60, DR runbook — pre-Apr 18 burn list, parallelizable with β.
7. R4/R5/R6/R8 full real evaluators (Tasks 7, 8 partial credit) — debt tracked, β does not depend.
8. **NEW:** v10 doc gap — R7 fail-closed semantics + `/override_earnings` not in v10 rulebook body. Logged for v10.1 or supplementary ops doc.

---

## 9. Stop Conditions for β Survey Phase

- Any survey response that surfaces undocumented coupling between α STAGED rows and a β consumer → STOP, file as architectural blocker.
- JIT precedence model survey produces ambiguous answer → escalate to Gemini Deep Think (Tier 1 reserve spend, requires Yash greenlight).
- Walk-away P&L extraction reveals downstream consumers beyond `telegram_bot.py:2967, 7590` → STOP, expand scope.
- TRANSMIT handler audit surfaces a security concern → STOP, do not proceed to live verification until resolved.
- Third invariant deviation without STOP-and-surface → process incident, full pause per v3 handoff.

---

## 10. References

- **Phase 3A.5c2 discovery:** `reports/phase_3a_5c2_discovery_20260407.md` §3-11
- **ADR-004:** `docs/adr/ADR-004-smart-friction-cure-console-deterministic-gate-enforcement.md`
- **Rulebook v10:** Rule 5, Rule 6, Rule 8, Rule 9, Definitions (Operator Attestation), Appendix D, Appendix E
- **HANDOFF_ARCHITECT_v3:** β scope bullets, process pattern watch, Tier 1 reserve status
- **Phase 3A.5c2-α COMPLETE:** `reports/phase_3a_5c2_alpha_complete_20260407.md`, anchor `ec0ea3a`
- **ADR-001/002/003:** R2 denominator, glide tolerance, R9 reporting-only

---

*This document is the v0 consolidation of β scope. Architect dispatches survey prompts per §4 before any β implementation. Coder may not begin β work without Architect dispatch.*
