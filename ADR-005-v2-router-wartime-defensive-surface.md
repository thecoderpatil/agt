> **SUPERSEDED 2026-04-19 by ADR-014_RETIRE_MODE_STATE_MACHINE.md.**
> The V2 Router defensive doctrine was anchored to the WARTIME desk mode,
> which ADR-014 retires in full. This ADR is preserved for audit-trail
> continuity only. The compensating controls and invariants documented here
> remain operationally active; only the mode-state framing is retired.

# ADR-005: V2 5-State Router as the Wartime Defensive Surface

**Status:** ACCEPTED (out-of-band, ratified 2026-04-10)
**Supersedes:** Partial relaxation of Rulebook V10 §Telegram, ADR-004 §Smart Friction (limited to V2 router actions)
**Author:** Yash (sole auditor)
**Implementing handoff:** HANDOFF_ARCHITECT_v15

---

## Context

The desk is currently in WARTIME. Margin pressure across both Yash and Vikram households requires immediate, systematic, surgical defensive operations on open short calls. The Cure Console + Smart Friction + Gate 1/2/3 attestation pipeline (ADR-004) is the correct long-term architecture for peacetime scaling and multi-tenant onboarding, but in its current state it is too slow and too unrefined for tactical Wartime defense. The operator needs Telegram to function as a pager, initiator, and approval surface for defensive actions — not just a transmit remote for Console-staged trades.

A V2 5-State Master Router has been implemented in `telegram_bot.py:_scan_and_stage_defensive_rolls` and an Apex Survival watchdog has been wired into `_el_snapshot_writer_job`. Both shipped out-of-band of the standard ADR/Sprint process. This ADR ratifies them, documents which invariants are intentionally relaxed, enumerates the compensating controls, and defines sunset criteria.

---

## Decision

**The V2 5-State Router is the canonical engine for handling open short calls under the Wartime defensive doctrine. It stages BTC and BAG-debit-roll tickets directly to `pending_orders`. The operator approves via the existing `/approve` Telegram flow. Execution flows through `_place_single_order` → `_pre_trade_gates` (with `site="v2_router"`) → `placeOrder`.**

The Apex Survival watchdog within `_el_snapshot_writer_job` is the canonical Excess Liquidity death-spiral detector. State 0 currently fires alerts only; autonomous tied-unwind execution remains TODO and is explicitly out of scope for this ADR.

---

## Invariants Relaxed

This ADR is the **sole** location where these relaxations are documented. Anyone reviewing the codebase should be able to grep for "ADR-005" and find every site where the relaxation applies.

### R1. Rulebook V10 §Telegram — Initiation Surface

**Original invariant:** Telegram operates strictly as a stateless final `[TRANSMIT]` confirmation remote and proactive pager. Trade initiation occurs exclusively via the Cure Console.

**Relaxation:** V2 router actions (STATE_2 HARVEST BTC, STATE_3 DEFEND BAG-debit roll) initiate from the Telegram scheduler, write to `pending_orders`, and surface to the operator via the standard `/cc` and `/rollcheck` digest. The operator approves via `/approve`. No Cure Console interaction required.

**Scope:** STRICTLY limited to V2 router-staged tickets (`payload.origin == "v2_router"`). All other staging paths (Rule 8 Dynamic Exit, conviction overrides, exception authorizations) remain Cure-Console-only.

### R2. ADR-004 §Smart Friction — Gate 3 Attestation

**Original invariant:** Gate 3 is implemented as Operator Attestation via the Cure Console Smart Friction widget. Python enforces Gates 1 and 2 deterministically and refuses to render the widget if either fails.

**Relaxation:** V2 router actions skip the Smart Friction widget entirely. Gate 3 is replaced by the operator's `/approve` tap on the standard pending_orders Telegram inline keyboard. Gates 1 and 2 (deterministic math gates evaluating EV-accretion and yield band thresholds) are enforced inside `_scan_and_stage_defensive_rolls` itself rather than via the canonical `evaluate_gate_1()`. Integer Lock is NOT applied.

**Scope:** STRICTLY limited to V2 router-staged tickets. WARTIME Rule 8 Dynamic Exits via the DEX path retain the full Integer Lock attestation flow.

### R3. Walker Source-of-Truth

**Original invariant:** `agt_equities/walker.py` is the pure-function source of truth for wheel cycle state, derived from `master_log_*` tables. `trade_repo.get_active_cycles()` is the canonical accessor.

**Relaxation:** `_scan_and_stage_defensive_rolls` reads positions directly from `ib_conn.reqPositionsAsync()` and computes `pnl_pct` from `pos.avgCost` rather than from walker-derived `initial_credit`. The router does not consult `trade_repo` for cycle state.

**Scope:** STRICTLY limited to the V2 router scan path. All deck rendering, R5 sell gate evaluation, R9 compositor, and DeskSnapshot construction continue to read from walker.

**Known drift risk:** `pos.avgCost` from IB can differ from walker-derived `initial_credit` after stock dividends, special distributions, or contract adjustments. V2 router pnl_pct can therefore drift from canonical cycle pnl. This is logged tech debt.

### R4. Mode State Machine — Wartime Whitelist

**Original invariant:** `_pre_trade_gates` Gate 1 blocks all non-DEX sites in WARTIME.

**Relaxation:** `v2_router` is added to the WARTIME whitelist alongside `dex`. The whitelist is `("dex", "v2_router")`.

**Scope:** Only the named sites are whitelisted. `legacy_approve` and any future sites remain WARTIME-blocked unless explicitly added to the whitelist via a follow-up ADR.

### R4.1 Amendment (2026-04-13) — legacy_approve added to whitelist

**Original whitelist (R4):** `('dex', 'v2_router')`

**Amended whitelist:** `('dex', 'v2_router', 'legacy_approve')`

**Rationale:** Operationally, the WARTIME defensive doctrine assumed all writing of new short calls would be paused during margin pressure. In practice, Mode 1 CCs on existing covered positions are the income-generation path that funds digging out of WARTIME. Locking out legacy_approve creates a deadlock: the desk cannot generate the premium income needed to reduce leverage and exit WARTIME. legacy_approve is added to the whitelist so /cc transmissions can fire during WARTIME on existing share positions. New CSP entries remain governed by AMBER/WARTIME blocks elsewhere in the rule engine — those blocks are unchanged.

---

## Invariants Retained

The following invariants are NOT relaxed and continue to apply to V2 router actions:

1. **Halt killswitch (`_HALTED`).** `/halt` immediately stops the V2 router scan loop and blocks `_pre_trade_gates`. Restart required to resume.
2. **$25k notional ceiling.** Applied per-ticket via `_pre_trade_gates` Gate 2, with corrected cash-paid semantics for BTC and BAG (see "Compensating Controls" below).
3. **Manual operator approval.** No autonomous transmit. Every V2 router ticket requires an operator `/approve` tap. Apex Survival State 0 autonomous unwind remains explicitly TODO.
4. **F20 NULL guard.** Not directly applicable to V2 router (audit_id is None for `pending_orders` rows), but the gate still runs.
5. **Non-wheel filter.** OPT, STK, and BAG are the only permitted secTypes. Anything else is fail-closed.
6. **Account scoping.** V2 router only acts on positions in `ACTIVE_ACCOUNTS`. Apex Survival only acts on `MARGIN_ACCOUNTS`.

---

## Compensating Controls (New, This ADR)

### CC1. Notional Gate Cash-Paid Semantics

`_pre_trade_gates` Gate 2 previously computed `notional = qty * strike * 100` for all OPT regardless of action. This is correct for SELL (worst-case obligation) but wrong for BUY-to-close, which extinguishes obligation rather than creating it. A 5-contract BTC on a $200 strike at $0.05 premium previously registered as $100,000 notional and was blocked, despite actual cash exposure of $25.

**New semantics:**

| sec_type | action | notional formula |
|---|---|---|
| OPT | SELL | `qty * strike * 100` (worst-case obligation, unchanged) |
| OPT | BUY | `qty * limit_price * 100` (cash paid to extinguish) |
| STK | any | `qty * limit_price` (unchanged) |
| BAG | any | `qty * abs(limit_price) * 100` (net debit/credit cash movement) |
| other | — | fail-closed reject |

This correction is cross-cutting and applies to all sites, not just V2.

### CC2. Payload Origin Tagging

V2 router-staged `pending_orders` payloads carry two new keys:

- `origin: "v2_router"` — routes through `site="v2_router"` in `_pre_trade_gates`
- `v2_state: "HARVEST" | "DEFEND"` — audit trail for which state-machine path triggered the stage
- `v2_rationale: <free-form>` — captured math snapshot at staging time (pnl_pct, ray, ev_ratio, etc.)

These fields are read by `_place_single_order` to select the correct gate site and are persisted in `pending_orders.payload` for forensic review.

### CC3. Mode Logging in V2 Scan Output

`_scan_and_stage_defensive_rolls` reads the current mode at scan entry and prefixes the alert digest with `━━ V2 Router [mode=WARTIME] ━━`. Mode is NOT used to gate staging — the execution gate handles that — but the operator sees mode context on every digest.

### CC4. BAG Combo Permitted in Non-Wheel Filter

Gate 3 previously rejected BAG outright. BAG is now permitted because the V2 STATE_3_DEFEND path stages BAG combos as IBKR's native primitive for atomic two-leg rolls. The notional formula in CC1 prevents oversized BAGs from slipping through.

---

## Risks and Tech Debt

### TD1. Walker Bypass Drift
V2 router pnl_pct can diverge from walker-derived cycle pnl after corporate actions. Mitigation: log every V2 staging decision with the rationale snapshot (CC2) so post-hoc reconciliation against walker is possible.

### TD2. No Audit Trail in `bucket3_dynamic_exit_log`
V2 actions never appear in `bucket3_dynamic_exit_log`. Forensic queries that join across DEX and V2 will need to also query `pending_orders` filtered by `payload.origin = 'v2_router'`. Documenting this as a known reporting gap.

### TD3. Smart Friction Bypass
V2 router actions are not subjected to Integer Lock or qualitative thesis attestation. The compensating control is operator manual review of the digest before tapping `/approve`. This is materially weaker than the ADR-004 attestation flow and is the primary reason this ADR has explicit sunset criteria.

### TD4. Apex Survival Autonomous Execution Stub
State 0 fires alerts but does not execute tied-unwinds. If margin actually crosses the 8% threshold, the operator is the only line of defense. This ADR does NOT authorize the autonomous execution path; that requires its own ADR.

### TD5. `cmd_rollcheck` Behavior Change
`/rollcheck` was previously read-only. It now actively stages tickets via `_scan_and_stage_defensive_rolls`. Operators who type `/rollcheck` expecting the old behavior will inadvertently stage trades. Mitigation: add a banner to the digest output noting that tickets have been staged.

### TD6. Notional Semantics Cross-Cutting Change
The Gate 2 correction (CC1) affects all sites, not just V2. Existing tests calibrated against the old strike-notional semantics may pass incorrectly or fail. Test suite needs a sweep.

---

## Sunset Criteria

This ADR is intentionally a Wartime expedient, not a permanent architecture. It will be superseded when:

1. **Mode normalization.** Desk returns to PEACETIME and stays there for ≥10 trading days. Trigger: re-evaluate whether V2 router should be downgraded to advisory-only or routed through Cure Console.
2. **Cure Console maturity.** Smart Friction widget supports a "Defensive Roll" attestation flow with Wartime-grade speed (sub-30-second operator path from alert to transmit). Trigger: migrate STATE_2 HARVEST and STATE_3 DEFEND to the DEX path with Smart Friction.
3. **Walker integration.** V2 router is refactored to consume `trade_repo.get_active_cycles()` and `inception_carryin` events instead of raw `reqPositionsAsync()`. Trigger: TD1 closed.
4. **Audit trail unification.** V2 actions write to `bucket3_dynamic_exit_log` (or a parallel `bucket3_v2_router_log`) with `final_status` lifecycle parity. Trigger: TD2 closed.

When at least three of the four criteria are met, this ADR is superseded by ADR-006 (Wartime Defensive Surface Sunset).

---

## Implementation Anchor

The diff package implementing this ADR is captured in HANDOFF_ARCHITECT_v15 §"V2 Router Wiring Diff". Test coverage requirements are in HANDOFF_CODER_v15 §"Test Suite Update".

Anchor commit (post-diff): TBD, Coder fills in.

---

## [REVISION: 2026-04-10] — TD1 framing correction per Phase 2 intel

Phase 2 intel gathering on 2026-04-10 surfaced three corrections to TD1 as originally logged. The original TD1 framing implied V2 router bypasses walker entirely for all states. That is incorrect.

**Corrected framing:**

V2 router's ACB pipeline is NOT a blanket walker bypass. STATE_1 ASSIGN and STATE_3 DEFEND read walker-backed cost basis via `_load_premium_ledger_snapshot(household, ticker)`, which routes to `trade_repo.get_active_cycles(household, ticker)` when `READ_FROM_MASTER_LOG = True`. That flag is hardcoded `True` in production. Walker supplies `Cycle.paper_basis` and `Cycle.adjusted_basis` for these two states, including partial corporate action handling (splits, SD, CM/TM — spinoffs SO/DW remain unimplemented, logged as Followup #3).

The walker bypass in V2 is limited to **STATE_2 HARVEST only**, which computes `pnl_pct` from `pos.avgCost` (IB position) rather than walker-derived `Cycle.premium_total / Cycle.initial_credit`. This is the narrow drift surface — not the global walker bypass TD1 originally described.

**Two additional issues surfaced by the same intel pass, now addressed by ADR-006:**

1. **Per-account precision loss.** Walker tracks basis per-account internally via `_paper_basis_by_account`, but `_load_premium_ledger_snapshot` consumes only the household-aggregated `Cycle.paper_basis` property. V2 router had no access to per-account basis. Under Puerto Rico Act 60 Chapter 2, household-aggregated basis is a tax compliance defect, not just imprecision. ADR-006 mandates per-account ACB for V2 STATE_1 and STATE_3.

2. **Same-day walker drift window.** Walker reads from `master_log_trades`, populated by `flex_sync_eod` at 5pm ET. During market hours walker sees yesterday-at-5pm state. V2 router classifications on positions with same-day activity can use stale basis. ADR-006 mandates a same-day delta reconciliation layer over walker output.

**TD1 status after ADR-006 lands:** The narrow drift surface (STATE_2 HARVEST using `pos.avgCost`) remains by deliberate design, with rationale captured in ADR-006 §TD-A4. HARVEST classification is about capital efficiency, not tax lot realization; `pos.avgCost` is account-scoped and canonical for the margin-freeing decision the HARVEST action triggers. TD1 is therefore NOT closed by ADR-006 — it is scoped down from "walker bypass" to "STATE_2 HARVEST uses IB avgCost by design", and the drift surface is explicitly bounded.

The broader ACB pipeline hardening is governed by ADR-006. TD1's original sunset criterion ("V2 router refactored to consume `trade_repo.get_active_cycles()`") is retained but reinterpreted: after ADR-006, STATE_1 and STATE_3 already meet this criterion via the per-account walker path. STATE_2's use of `pos.avgCost` is now documented permanent design, not debt.

