# ADR-011 — Live-Execution Promotion Framework

**Status:** Draft
**Date:** 2026-04-19
**Author:** Architect (Cowork, Opus)
**Inputs:** RULING_ADR_BACKLOG_20260419.md §3.1 (SHIP items 1–4, 11) + DR-1 + Sonnet live-execution research
**Supersedes:** Implicit ad-hoc promotion ("flip the flag when it feels stable") — never documented, now explicit.
**Related:** ADR-007 (Self-Healing Loop), ADR-008 (Shadow Scan), ADR-010 (CSP Approval Digest), ADR-013 (Self-Healing v2 — successor budget mechanics), ADR-015 (Tier Migration Roadmap)

---

## 1. Context

AGT is paper-autonomous as of 2026-04-17. The CSP scanner (`csp_scan_daily` 09:35 ET), CC pipeline (`cc_daily` 09:45 ET), harvest scanner, and roll engine all stage and execute against the paper Gateway (port 4002) without operator intervention. The live Gateway (port 4001) is configured Read-Only at the IB protocol layer — `placeOrder` calls are rejected before they leave the wire.

Today there is no documented criterion for flipping any one engine to live capital. The implicit rule has been "Yash decides when it feels safe." That is not a Level-4 autonomy posture and it does not survive the first incident review by an external party.

This ADR defines the single canonical path from paper-autonomous to live-autonomous, engine by engine, gate by gate. It does *not* commit a date for the first live flip. It commits the conditions under which a flip becomes admissible.

The motivating prior art is short and well-documented. Knight Capital lost $440M in 45 minutes on 2012-08-01 because a stale flag re-routed a deprecated code path into production order flow — no canary, no kill switch, no rollback. Robinhood's March 2020 outages compounded for 17 hours because position-state divergence between systems was not bounded by an automated trip. Both are referenced once, here. They are not re-litigated in subsequent ADRs.

## 2. Promotion Gate Matrix

An engine is *eligible* to flip from paper to live when **all five** gates are simultaneously green for the trailing 14 calendar days.

| # | Gate | Threshold | Source of truth | Notes |
|---|------|-----------|-----------------|-------|
| G1 | Shadow-vs-live decision divergence | < 3 bps mean, < 5 bps p99 on staged-order notional vs simulated live-order notional | `shadow_scan` JSON output (ADR-008) cross-joined to a hypothetical live-priced fill | Divergence is computed per ticket, weighted by notional. The 3 bps mean is the steady-state target; the 5 bps p99 is the tail guard. |
| G2 | Zero-trip dry run | 14 consecutive trading days with zero Tier-0 or Tier-1 invariant trips | `incidents` table (ADR-013 schema) filtered by `severity_tier <= 1` | Tier-2 (observability) trips do not block. A single Tier-0 resets the clock. |
| G3 | Sample size | ≥ 60 staged decisions of the candidate engine type within the 14-day window; ≥ 120 cumulative if engine type is novel (no prior live history) | `pending_orders` filtered by `engine = ?` and `created_at >= now() - 14d` | Below 60 the divergence statistic is not statistically meaningful. The 120 floor for novel engines is the cold-start cushion. |
| G4 | Broker-rejection rate | < 0.1% of staged orders rejected by IB during paper canary phase (any reason: pacing, margin, insufficient buying power, contract not found) | `pending_orders.status = 'rejected'` count / total staged | A paper rejection rate above 0.1% is a defect signal — paper Gateway accepts almost everything; rejections indicate engine-level bugs the live Gateway will magnify. |
| G5 | Operator override variance | Counterfactual P&L of operator-overridden decisions does not statistically beat the engine's own decisions over the same window (one-sided t-test, α=0.05) | `decisions` table (ADR-012 schema) | If the operator's overrides are demonstrably better than the engine, the engine is not ready. This gate exists specifically for CSP entry; it is N/A for engines with no operator-gate (CC exit / harvest / roll). |

Thresholds live in `config/promotion_gates.yaml`. Threshold changes are an ADR amendment, not a config push.

## 3. Engine Promotion Sequence

Engines flip in the following fixed order. No engine may flip while a prior engine in the sequence is still in canary or rollback.

1. **Exit (CC sell-to-close on dynamic-exit threshold).** Defensive. Reduces existing exposure. Lowest blast radius — at worst we exit a profitable position too early. First to flip.
2. **Roll (CC roll-on-below-basis defensive sequence).** Defensive. Adjusts existing exposure without opening new directional risk. Second.
3. **Harvest (CC sell-to-close on profit-take threshold).** Defensive. Closes profitable positions. Third — slightly higher P&L sensitivity than dynamic-exit because harvest decisions are contested between engines (dynamic-exit may also trigger on the same position).
4. **Entry (CSP sell-to-open).** Opens new exposure. Highest blast radius. Last to flip. **Selection of which CSP to enter remains permanent Level-2** (CCO attestation per Yash ruling 2026-04-18). Entry-engine promotion to L4 covers *execution* of an approved candidate — order routing, time-in-force, limit-price computation — not the candidate yes/no.

The ordering is final. An engine cannot leapfrog. If gate G3 fails for Roll because we have not generated 60 roll decisions in 14 days, Roll waits — Harvest does not flip in front of it.

Each engine carries its own gate state. Exit can be live while Roll is still paper. Engines do not share canary phase.

## 4. Kill-Switch Triggers + Pre-Gateway Risk Layer

A pre-gateway risk layer (`agt_equities/risk/pregateway.py` — to be built) sits between any engine's `order_sink.stage(...)` call and the IB Gateway's `placeOrder` invocation. It evaluates four trip conditions on every order. Any single trip:

1. Halts the offending engine immediately (sets `engine_state = 'halted'` in a new `engine_state` table).
2. Cancels all the engine's open working orders via `cancelOrder` (best-effort; failures logged but do not unblock the trip).
3. Writes a Tier-0 incident.
4. Sends a Telegram alert to Yash with the trip reason and the exact rejected ticket payload.
5. Does **not** roll back already-filled orders. The trip is a stop, not an undo. Already-filled positions become operator-managed until Yash explicitly resumes the engine.

| # | Trigger | Threshold | Window | Reset condition |
|---|---------|-----------|--------|-----------------|
| K1 | Session drawdown | > 5.0% of pre-open NAV | Current trading session (09:30–16:00 ET) | Manual operator resume after post-mortem. No auto-reset. |
| K2 | Consecutive broker rejections | ≥ 3 sequential rejections from IB on the *same engine* within any rolling 60-second window | Rolling 60s | Manual operator resume. Auto-reset would mask a malformed-order bug. |
| K3 | Signal-to-ack latency | > 500ms (95th percentile of last 20 orders) on the path engine-decision → IB-acknowledgment | Trailing 20 orders | Auto-reset when p95 returns below 500ms for 5 consecutive orders. Latency is transient by nature; persistent breach implies a different problem K1/K2 will catch. |
| K4 | Correlation drift vs paper baseline | Pearson correlation of (live-engine decision sequence) vs (paper-engine decision sequence on identical input snapshot) drops below 0.95 over a trailing 50-decision window | Trailing 50 decisions | Manual operator resume. Sub-0.95 correlation between live and paper engines on identical input means one of them has drifted from the documented decision tree. |

K1 is the catastrophic-loss guard (Knight Capital). K2 is the malformed-order guard. K3 is the infrastructure-degradation guard. K4 is the engine-behavior-divergence guard (this is the gate that would have caught the Robinhood 2020 state divergence had it existed).

Kill switches are independent across engines. K1 triggering on Entry does not halt Exit — Exit may still be the only thing keeping us out of further trouble. The pre-gateway layer is per-engine, evaluated per-order.

## 5. Canary Ramps + Time-Based Baking

When an engine satisfies the §2 gate matrix and reaches its position in the §3 sequence, it enters canary. Canary is **percentage-of-equity-eligible**, time-baked, and explicitly *not* statistical-volume-baked.

| Phase | Equity exposure cap | Bake time | Advance condition |
|-------|---------------------|-----------|-------------------|
| C1 | 5% of household NAV | 5 trading sessions (one full week) | Zero K1–K4 trips during phase. Manual operator advance to C2. |
| C2 | 15% | 5 trading sessions | Zero K1–K4 trips during phase. Manual operator advance to C3. |
| C3 | 50% | 10 trading sessions (two full weeks) | Zero K1–K4 trips during phase. Manual operator advance to C4. |
| C4 | 100% | Permanent | This is "live." No further advancement. |

Total minimum bake from G-pass to C4 is **20 trading sessions** (one calendar month). There is no mechanism to skip phases. There is no mechanism to compress bake time. A Yash override to compress requires an ADR amendment and is logged as a permanent deviation.

Time-based baking is correct for our regime. We are not Citadel running 10⁶ orders/day where a 50% statistical-power calculation makes sense in 30 minutes. We trade ~20 tickers on a weekly cadence. A full continuous market session is the smallest meaningful unit of evidence; a calendar week is the smallest meaningful unit of confidence. ADR-013 §5 covers the per-commit time-bake; this ADR covers the per-engine multi-week bake.

Per-trade-count canary ramps are explicitly rejected. They distort engine cadence and create perverse incentives to over-trade for promotion velocity.

## 6. Rollback Playbook

Three rollback modalities, not interchangeable.

### 6.1 Immediate

Triggered by: K1–K4 trip, or manual `/halt <engine>` Telegram command.

Sequence:
1. Engine state flips to `halted` (atomic SQLite update).
2. Pre-gateway layer rejects all subsequent stages from the engine.
3. All engine-owned working orders cancelled via `cancelOrder` (best-effort; log non-cancels but do not block).
4. Telegram alert to Yash with trip cause, last 10 orders, current open positions owned by the engine.
5. Filled positions remain — they are now operator-managed.

No code revert. Immediate rollback halts the runtime, not the codebase.

### 6.2 Planned

Triggered by: Yash decides an engine in canary should regress to paper (e.g., post-mortem of a near-miss).

Sequence:
1. `/regress <engine>` Telegram command.
2. Engine `live` flag cleared in `config/engine_state.yaml`.
3. Next scheduler tick reads paper-only configuration for the engine.
4. Working orders not cancelled — they continue to natural close on the live Gateway. No new live orders.
5. Engine begins fresh in C1 paper after a minimum 14-day cool-off. The §2 gate clock resets.

### 6.3 Post-mortem

Triggered by: any Tier-0 incident on a live engine.

Sequence:
1. Engine immediately to halted (per §6.1).
2. Architect drafts a post-mortem within 72 hours: timeline, root cause, contributing factors, code fix shipped, gate or kill-switch threshold change proposed.
3. Re-promotion requires the gate clock to restart at G2 = 0 days, and the new C1 begins only after the proposed threshold change is shipped (or explicitly declined with rationale).
4. Permanent record in `docs/adr/post_mortems/PM-<YYYYMMDD>-<engine>.md`. This file is read by every future Architect session at the start of any promotion-related dispatch.

## 7. Compliance Overlay

AGT is a California state-registered RIA (CRD# 338997). This ADR's promotion framework intersects compliance in three places.

**Fiduciary duty.** Live execution must remain consistent with the documented investment strategy on each affected client's IMA. The Heitkoetter Wheel strategy is the documented strategy for principal accounts; advisory clients with different IMAs are not in scope for autonomous live execution under this ADR. Multi-tenant promotion requires a separate per-IMA promotion record (deferred to ADR-015 component inventory).

**Best execution.** Limit-price computation in the entry engine must be auditable. The pre-gateway layer logs the computed limit price, the contemporaneous NBBO from `reqMktData`, and the basis for any deviation, on every order. This log is the best-execution evidence trail.

**Form ADV Part 2A disclosure.** Algorithmic execution against principal accounts is disclosed in Part 2A narrative. The CCO attestation of that narrative is a permanent Level-2 act (per Yash ruling 2026-04-18). This ADR does not change that. Material changes to the disclosed execution methodology — adding a new engine, changing the canary ramp, changing the kill-switch set — require an ADV amendment. The ADV amendment text itself is L1 (Architect-drafted, AI-assisted); the CCO signature on the filed amendment is L2.

**No client algorithm exposure today.** Advisory clients currently do not have algorithm-driven execution. If and when an external advisory client's IMA is amended to include algorithmic execution, a separate Client Account Agreement amendment is required (per Sonnet §7.4 framing). That work is **out of scope** for this ADR.

## 8. Non-Goals

Explicitly not in scope:

- **Multi-broker order aggregation.** AGT trades through Interactive Brokers exclusively. The pre-gateway risk layer is IB-specific. Multi-broker support is deferred indefinitely; if revisited, it requires a successor ADR.
- **FINRA Rule 15c3-5 (Market Access Rule) delegation.** 15c3-5 applies to broker-dealers providing market access to other broker-dealers. AGT is an RIA, not a BD, and does not provide market access. Reference architectures from market-access vendors (Wedbush, IBKR's institutional layer) inform our kill-switch design but the rule itself does not bind us.
- **High-frequency execution surface.** Sub-100ms latency is not a goal. The 500ms K3 threshold is a degradation guard, not a target. Optimizing for HFT-grade latency would require infrastructure investment (colo, FPGA NIC) inconsistent with our wheel-strategy cadence.
- **CSP candidate selection autonomy.** Permanent Level-2. Out of scope for any §3 engine promotion.
- **CCO attestation acts.** Permanent Level-2. Out of scope.
- **Quarterly re-certification ceremony.** AGT has one operator who is also the CCO; quarterly re-cert collapses to "Yash reviews KPIs" which does not warrant ADR-mandated ceremony. Calendar reminder only.
- **Live testing in non-canary mode.** There is no path from paper directly to 100% equity exposure. The §5 ramp is mandatory.

## 9. First Shippable Sub-Dispatch

The first artifact to land from this ADR is `config/promotion_gates.yaml` plus `tests/test_promotion_gates.py`. The test reads current paper baseline metrics from the production DB (read-only) and asserts each gate threshold against them. Whichever gates the current paper baseline does not satisfy are our concrete first live-promotion blockers, named explicitly in the test failure output.

This dispatch is in the §4 Coder queue of the ruling document as **Dispatch C** (Coder-tier, ~200 LOC, serializes behind the Codex-tier Dispatches A and B that lay down the ADR-012 / ADR-013 schema dependencies).

## 10. Open Questions

These are deferred to subsequent ADRs or Coder verification, not to this draft.

- Does the K4 correlation-drift threshold (0.95) survive contact with the actual decision-sequence variance once Dispatch B's incidents-with-tier-and-source schema is populated for two weeks? Re-evaluate at first ADR-011 amendment review.
- Does G5 (operator override variance) belong in this ADR or in ADR-012? Currently here because it gates promotion; argument for moving to ADR-012 is that the counterfactual-P&L mechanism is owned by the learning loop. **Decision pending Dispatch A's `decisions` schema landing.**
- Does the pre-gateway layer share state across engines for the K3 latency calculation, or does each engine carry its own latency window? Current draft says per-engine; revisit if engines share enough infrastructure that per-engine windows are statistically anemic.
- For re-promotion after a Tier-0 post-mortem (§6.3), should the gate clock restart fully (G3 sample resets to zero) or partially (G3 retains pre-incident sample, G2 resets only)? Currently full restart. Revisit after first actual post-mortem — there will not be one in the first 90 days if §5 holds.

---

## Appendix A — Verify-Pending Citations

Per RULING_ADR_BACKLOG_20260419.md §5, the following citations referenced in upstream DR research are **not yet verified** by Coder. They are **not** load-bearing for any decision in this ADR, but are noted here so subsequent amendments can cite them confidently once Coder returns the verification report (`reports/investigation_citation_verification_20260419.md`).

- ESMA February 2026 supervisory briefing on AI in algorithmic trading + pre-trade controls. **Verify-Pending.** If verified, becomes the EU regulatory cross-reference for the §4 kill-switch architecture. If not verified, no decision in this ADR changes.
- FINRA 2024 market access guidance update (DR-1 §3.1 reference). Cited by DR-1 as a converging-source for the K1–K4 trigger set. The triggers themselves stand on Knight/Robinhood case studies and first-principles risk analysis; the FINRA citation is supplementary.

Verified citations (already cleared in ruling §1 trust tier):

- Knight Capital Group 2012 incident — SEC Release No. 34-70694, 2013-10-16.
- Robinhood March 2020 outages — Colorado Securities Commissioner Consent Order, 2021.
- Quantopian / Alpaca / Freqtrade / IBKR-platform threshold convergence on §2 gate matrix — DR-1 + Sonnet cross-confirmation.

## Appendix B — Threshold Provenance

| Threshold | Source | Confidence |
|-----------|--------|------------|
| Shadow divergence < 3 bps mean / 5 bps p99 | DR-1 + Sonnet convergence | High |
| 14-day zero-trip dry run | DR-1 + first-principles | High |
| 60-trade minimum (120 cold-start) | DR-1 + statistical power for divergence test | High |
| 0.1% rejection rate | DR-1 + IBKR platform standard | High |
| 5% session DD kill switch | DR-1 + Knight Capital postmortem heuristic | High |
| 3 consecutive broker rejections | DR-1 + Sonnet | High |
| 500ms signal-to-ack latency | DR-1 + IB Gateway typical p95 baseline | Medium — re-evaluate after first 30 days of live latency telemetry |
| 0.95 correlation-drift floor | First-principles. No external citation. **Re-evaluate at first amendment.** | Medium |
| Canary ramp 5/15/50/100 + 5/5/10/permanent sessions | DR-1 + first-principles + Sonnet §5.1 | High |

Any threshold marked Medium is a candidate for a §10 open-question revisit. None of the Medium thresholds are load-bearing alone — each is one of multiple gates or guards in defense in depth.
