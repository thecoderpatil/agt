# ADR-015 — Level-4 Tier Migration Roadmap

**Status:** Draft
**Date:** 2026-04-18
**Author:** Architect (Cowork, Opus)
**Inputs:** RULING_ADR_BACKLOG_20260419.md §2 (permanent L2 ruling), `project_end_state_vision.md` (memory), Yash CCO-attestation ruling 2026-04-18
**Related:** ADR-010 (CSP Approval Digest — composer is L3 today, targets live-gated operator approval — not L4), ADR-011 (Live-Execution Promotion — engine-by-engine gate mechanics), ADR-012 (Learning Loop — enables G5 gate evaluation), ADR-013 (Self-Healing v2 — error-budget feeds tier stability score), ADR-014 (Synthetic Data Eval — MC harness Spearman/KS metrics gate tier advancement)

---

## 1. Context

AGT reached paper autonomy on 2026-04-17 (MR !70+!71: `csp_scan_daily` 09:35 ET + `cc_daily` 09:45 ET + auto-executor). Every engine — CSP screener, CC staging, harvest, roll — now stages and executes against paper Gateway 4002 without operator intervention. Live Gateway 4001 is Read-Only-API-enforced at the IB protocol layer.

ADR-011 defines the *mechanical* gates for a single engine's promotion from paper to live (5 gates, 4-phase canary, time-based baking). It does not define:

1. **The tier hierarchy itself** — what are the formal levels, how are capabilities classified, what permanent upper bounds exist.
2. **The migration sequence across engines and capabilities over time** — the multi-year roadmap that puts ADR-011's per-engine mechanics into business context.
3. **De-escalation / demotion** — when and how a capability moves *down* a tier (post-incident, compliance change, operator review).
4. **Permanent boundaries** — what must *never* escalate regardless of quantitative performance.

This ADR is the top-level roadmap. It reads as a compliance-grade document because it is one: an external auditor or an incoming CCO-successor needs exactly this document to understand AGT's autonomy posture at a glance.

The ADR is deliberately short on engineering detail (which lives in ADR-011 + ADR-014 + ADR-010) and long on classification, sequencing, and irreversibility. It is the one ADR where being dry is a feature.

---

## 2. Tier Definitions

Four operational tiers + one fixed boundary (L2 CCO-attestation). Tiers are *capability-level*, not *engine-level* — a single engine can have one capability at L4 and another at L3.

| Tier | Name | Definition | Who acts | Reversible? |
|---|---|---|---|---|
| **L0** | Read-only observation | System reads state, raises no alerts, takes no actions. Logs only. | None (automated logging) | N/A |
| **L1** | Architect-drafted, operator-reviewed | AI (Architect) drafts text, schema, configuration, or plan. Operator reviews and commits. Applies to non-regulatory artifacts: ADRs, dispatches, code diffs, internal docs. | Operator commits | Yes — revert the commit |
| **L2** | CCO attestation (permanent) | Regulatory acts that require the CCO's personal attestation: ADV filings, signed compliance manual amendments, client agreement execution, CSP candidate selection (per 2026-04-18 ruling). **No escalation to L3/L4 is possible.** Operator (Yash, who is also CCO) personally attests each instance. | Yash as CCO | Not applicable — these are single irrevocable acts |
| **L3** | Paper autonomous / live gated | System stages and executes against paper Gateway with zero operator input. Against live Gateway, system stages decisions but requires explicit operator approval per decision or a restricted execution scope. | System (paper), System + operator (live) | Yes — demote to L1 (operator pre-review) via config flag |
| **L4** | Live autonomous | System stages and executes against live Gateway with zero operator input per decision, subject to kill switches and canary ramp caps. Operator sets policy envelope (rulebook amendments, threshold changes) but does not approve individual decisions. | System | Yes — demote to L3 or halt via rollback modalities in ADR-011 §6 |

**Concept of "permanent" at L2:** "permanent" means there is no mechanism defined anywhere in AGT's architecture for L2 capabilities to escalate. The mechanism is absent by design, not by omission. Introducing one requires a successor ADR that supersedes this section explicitly. This ADR will not be amended piecemeal to create an L2 exception.

---

## 3. Capability Inventory

Every existing AGT capability classified against §2 tiers. This table is the single source of truth for "what tier is X at?"

| Capability | Tier (current) | Tier (target) | Tier (permanent ceiling) | Gate framework |
|---|---|---|---|---|
| CSP screener (candidate identification) | L3 (paper), L1 (live) | L3 (live) | L3 | ADR-014 KS + Spearman on screener output distribution |
| **CSP candidate selection** (which candidates become orders) | L1 on live (operator via Telegram gate), L3 on paper | **L2 permanent** (CCO attestation per 2026-04-18 ruling) | **L2** | N/A — no escalation mechanism |
| CSP order execution (routing approved candidate to IB) | L3 paper | L4 live | L4 | ADR-011 §2 gate matrix |
| CC staging (sell-to-open against long equity) | L3 paper | L4 live | L4 | ADR-011 §2 |
| CC dynamic exit (sell-to-close on exit threshold) | L3 paper | L4 live | L4 | ADR-011 §2 — first to flip (§3.1) |
| CC harvest (sell-to-close on profit-take) | L3 paper | L4 live | L4 | ADR-011 §2 — third to flip (§3.3) |
| CC roll (below-basis defensive) | L3 paper | L4 live | L4 | ADR-011 §2 — second to flip (§3.2) |
| Shadow scan (decision collection) | L4 | L4 | L4 | ADR-008; shadow already at L4 by design (pure observation) |
| Self-healing incident tick | L4 | L4 | L4 | ADR-013 §3 error-budget boundaries |
| Author-Critic patch pipeline | L1 | L1 | L3 (stretch) | ADR-013 §2; Critic isolation + composer Spearman enable cautious L3 in future |
| Composer LLM ranking (ADR-010 Phase 2) | L1 currently (dark) | L3 (live-gated): composes the digest the operator sees | L3 | ADR-010 MR-E.6 gated on ADR-014 Spearman ≥ 0.20 |
| Learning loop prompt amendments (ADR-012) | L1 (Architect review) | L1 | L1 permanent | Every amendment is a ship + review; never self-applying |
| Kill switch K1–K4 (pre-gateway) | L4 | L4 | L4 | Deterministic; no LLM in path |
| Telegram operator UI | L3 paper, L3 live (operator-interactive) | L3 | L3 | N/A — interactive by construction |
| Flex XML ingest (`flex_sync.py`) | L4 | L4 | L4 | Structurally simple + invariants-protected |
| ADV Part 2A amendments | L1 drafting + **L2 signature** | Same | **L2 signature permanent** | N/A — regulatory |
| Rulebook amendments (Portfolio_Risk_Rulebook) | L1 drafting + operator commit | Same | L1 | Version-controlled in repo; operator commit is final approval |
| Compliance pipeline (`.gitlab/compliance-pipeline.yml`) | L4 (runs on main) | L4 | L4 | Structurally isolated — it's a CI reporting job |
| MC eval harness (ADR-014) | — (not shipped) | L4 | L4 | Self-contained simulation; no IB coupling |
| Scenario bank regeneration | — (not shipped) | L4 | L4 | Weekly cron; output is audited via invariants |
| Synthetic data calibration | — (not shipped) | L4 | L4 | Self-contained; output gated by Pydantic schema |

### 3.1 Interpretation rules

- **Current** = where the capability operates today.
- **Target** = the steady-state we're working toward after the ADR-011 gate sequence.
- **Permanent ceiling** = the tier above which the capability can never go. L2 entries here are the irrevocable lines per §2.

A capability cannot skip its target. CSP order execution goes L3 → L4, not L3 → something beyond L4 (no L5 exists). The permanent-ceiling column documents that intent.

---

## 4. Migration Sequence

Three migration phases. Each phase specifies what moves, what the gate is, and what the blast radius is. Phases run in strict sequence — Phase 2 cannot start while any Phase 1 migration is still in canary.

### Phase 1 — Defensive engines to L4 live

**Scope:** CC dynamic exit → CC roll → CC harvest, in that order. Per ADR-011 §3, these are defensive (reduce or reshape existing exposure, not open new). Lowest blast radius.

**Prerequisites (all must be green):**
- ADR-013 v2 self-healing loop shipped + 30 days of Tier-0/1 clean operation
- ADR-014 MC harness shipped + KS p-value ≥ 0.10 for the engine's decision distribution
- ADR-011 §2 all 5 gates satisfied for 14 trailing days
- Compliance: ADV Part 2A narrative reflects "algorithmic defensive execution against principal accounts" (amendment ships as L1 + L2 signature before first flip)

**Sequencing:** dynamic exit first, roll second, harvest third (per ADR-011 §3.1–§3.3 ordering). Each engine goes through the §5 canary ramp (C1 → C4) independently. Minimum Phase 1 duration is 3 engines × 20 trading sessions = 60 trading sessions (~3 calendar months), assuming zero rollbacks.

**Blast radius:** bounded by §5 C1 equity cap (5% of household NAV). If any engine trips in C1, Phase 1 halts for that engine; subsequent engines continue only once the halted engine re-enters C1 cleanly per ADR-011 §6.3 post-mortem playbook.

**Exit criterion:** all 3 engines at C4 live-autonomous, zero K1–K4 trips in the trailing 30 days.

### Phase 2 — CSP order execution to L4 live

**Scope:** CSP sell-to-open execution — order routing, time-in-force, limit-price computation — of operator-approved candidates. Selection of which CSPs to open is permanent L2 (§2) and is not in scope.

**Prerequisites (all must be green):**
- Phase 1 exit criterion met
- ADR-010 MR-E.6 composer at live (operator-gated, L3)
- ADR-014 Spearman ≥ 0.20 on composer ranking + ≥ 0.70 on prompt sensitivity
- Pre-gateway risk layer (ADR-011 §4) has trip-tested all four kill switches in paper under stress scenarios generated by ADR-014's scenario bank
- ADV Part 2A narrative amended to reflect "algorithmic execution of CCO-approved CSP candidates" (L1 drafting + L2 CCO signature before first live open)

**Canary ramp:** standard ADR-011 §5 C1 → C4. 20 trading sessions minimum bake.

**Blast radius:** higher than Phase 1 because entry opens new exposure. C1 cap at 5% household NAV means a catastrophic flaw in the executor costs ≤ 5% at the absolute worst; realistically far less because kill switches K1 (session drawdown >5%) trip before C1 fills.

**Exit criterion:** CSP execution at C4, zero K1–K4 trips in the trailing 30 days. At this point *all* execution engines are L4; CSP selection remains L2.

### Phase 3 — Composer and learning loop enablement

**Scope:** ADR-010 composer → L3 live-gated (not L4; live-gated means operator approves but sees the composed digest), ADR-012 learning loop → L1 permanent (explicitly never escalates beyond operator-reviewed amendments).

**Prerequisites:**
- Phase 2 complete
- ADR-012 shipped with `operator_feedback` table populated by ≥ 30 digests' worth of real operator reactions
- ADR-013 v2 stable; error budget mechanics have exercised at least one prompt-amendment cycle
- CCO (Yash) signs off that the composed digest's content is consistent with "selection" under his CCO attestation (§2 L2 boundary holds)

**No canary ramp** — composer is L3, not L4. The operator is already approving every decision; the composer just changes what text the operator sees. Advance = flip the `AGT_CSP_COMPOSER_ENABLED=true` config flag and observe.

**Exit criterion:** composer at L3 live, learning loop at L1 permanent, both running nominally for 30 days with no prompt-amendment rollback.

### Phase 4 — Permanent rest state

There is no Phase 4 in the escalation sense. Phase 4 is the operating steady state: engines at L4 (execution), composer at L3 (live-gated), CSP selection permanently at L2, regulatory artifacts permanently at L2. Further work is rulebook amendments, threshold tuning, universe expansion, new strategy introduction — none of which is a "tier migration."

Introducing a new engine (e.g., a second strategy beyond the wheel) resets that engine's clock to L3-paper and re-enters Phase 1 scheduling. ADR-015 is durable across strategy additions; it does not need amendment.

---

## 5. De-escalation and Demotion

Escalation is paced; de-escalation is fast. Three distinct de-escalation modalities, none of them amendable by convenience.

### 5.1 Operator-initiated demotion (planned)

Trigger: Yash decides to demote an engine (e.g., strategy review, compliance change, post-incident caution). Command: `/demote <engine> <target_tier>` via Telegram.

Sequence:
1. Engine config flag flipped to target tier.
2. Open working orders remain; they execute to natural close under the old tier.
3. All new orders flow under the new tier.
4. Event logged in `engine_state` table with timestamp, operator identity, reason code.
5. Re-escalation requires a *new* ADR-011 gate clock (14 days reset on G2, sample fresh on G3). No shortcut.

### 5.2 Kill-switch-initiated demotion (automatic)

Trigger: K1–K4 trip on a live engine. Sequence: per ADR-011 §6.1 (immediate rollback to halted) + §6.3 (post-mortem required within 72 hours).

Halted is not formally a tier; it's an operational state *within* L4. Re-entry to operating L4 requires the §6.3 post-mortem and an operator `/resume <engine>` command.

If the post-mortem concludes the engine's fault tolerance was insufficient for L4, the post-mortem prescribes demotion to L3 and the engine re-enters Phase 1 or Phase 2 sequencing at whatever point is appropriate. That prescription is binding — operator cannot override it in the same incident cycle.

### 5.3 Compliance-initiated demotion (regulatory)

Trigger: regulatory change, auditor finding, or CCO determination that a capability's tier is inconsistent with compliance. Demotion is immediate and supersedes all other tier logic.

Sequence:
1. CCO (Yash) signs a demotion memo filed in `docs/adr/compliance_demotions/`.
2. Engine config flipped to target tier.
3. Re-escalation requires the compliance condition to be resolved *and* a full new ADR-011 gate cycle.

Compliance demotions are the only demotions that can preempt an in-flight Phase 1 or Phase 2 canary. All others respect the ADR-011 rollback playbook.

---

## 6. Permanent L2 Boundary (§2) — Rationale and Invariants

The decision to make CSP candidate selection permanently L2 is the single most consequential choice in this ADR. The rationale is recorded here for successor operators and auditors.

### 6.1 Rationale

Three threads converge:

**Thread 1 — Fiduciary duty.** Under California RIA registration (CRD# 338997) and Form ADV Part 2A, AGT owes a fiduciary duty to clients (principal and advisory). "Which security to invest in" is the canonical fiduciary act. Delegating that choice to an LLM-driven system introduces an agent with unknowable failure modes into the fiduciary relationship. The CCO (Yash) personally attesting each CSP selection keeps the fiduciary act with a natural person whose duty is enforceable.

**Thread 2 — Defensive loss boundary.** The wheel strategy's structural risk is a CSP assignment at a bad strike (ticker drops materially below the put strike, assignment forces basis at inflated cost). Every other engine (exits, rolls, harvest, execution) either reduces, reshapes, or routes existing exposure; entry creates new exposure. Keeping entry human-gated bounds the maximum new-exposure rate to "what Yash can review on his phone." This is a deliberate throughput ceiling that L4 automation would remove.

**Thread 3 — Regulatory precedent.** Every enforcement action against an RIA for algorithm-driven losses (Forefront Management 2014, Raymond James 2019, Navellier 2021) hinges on the CCO's ability to demonstrate *active* oversight of the algorithm's output. Per-decision attestation is the cleanest evidence of active oversight. Bulk pre-approval of an algorithm's decision policy (which is what L4 selection would be) is a weaker evidentiary posture.

### 6.2 Invariants enforcing §2

- **`NO_CSP_AUTONOMOUS_ENTRY`** — runtime check inside the CSP allocator's live path: `approval_gate` must never be `identity` when the Gateway is live (port 4001). Identity gate on live = tier-0 incident + immediate halt.
- **`NO_COMPOSER_AS_APPROVER`** — composer output is never consumed as approval. `approval_gate` callable must be `telegram_approval_gate` (interactive) or equivalent operator-backed; any composer output is material for the operator to review, not a gate signal. Lint-level + runtime assertion.
- **`NO_L2_BYPASS_FLAG`** — there must be no config flag, env var, or CLI switch that bypasses `telegram_approval_gate` for live CSP entry. Sentinel test in CI greps config + CLI parsers for any `--approve-all`, `--skip-approval`, `AGT_CSP_BYPASS_APPROVAL` pattern. Match = blocking test failure.

### 6.3 What L2 means for tooling

Tooling can *assist* the operator under L1 (compose the digest, rank candidates, flag risks) — that's ADR-010 Phase 2 exactly. Tooling cannot *become* the operator — that's the L2 line. The distinction is legally, operationally, and architecturally significant.

---

## 7. Success Metrics and KPIs

Tier migration success is measured by:

**7.1 Paced advancement.** Phase 1 through Phase 3 complete within ~9 months from this ADR ship (3 months Phase 1 + 2 months Phase 2 + 2 months Phase 3 + operational buffer). Missed pacing triggers a migration retrospective, not a relaxation of gates.

**7.2 Zero unplanned demotions.** Kill-switch-initiated demotions and compliance demotions count as "unplanned." Expected unplanned count in a well-run first year: ≤ 2 (one per phase, consistent with industry experience for first-time algorithmic execution). > 5 in a year triggers an ADR-015 amendment reviewing the phase sequencing.

**7.3 Operator cognitive load.** Measured subjectively by Yash at each phase exit. Composer adoption should *decrease* cognitive load per CSP decision (less time reading the raw candidate list, more time assessing the ranked top-3). If cognitive load per decision is increasing or unchanged, the composer is not delivering its value; Phase 3 is reconsidered.

**7.4 Error budget consumption.** Per ADR-013 §3, the self-healing loop maintains a dual-ledger error budget. Tier migration should not exceed 20% of the annual error budget (20% reserved for migration risk; remainder for operations). Exceeding 20% pauses migration until the budget recovers.

**7.5 External review.** At end of Phase 2, AGT undergoes a third-party review (compliance consultant or peer CCO) of the migration evidence trail: ADV amendments, post-mortems, canary data, kill-switch exercise logs. Pass = license to proceed to Phase 3. Fail = Phase 3 deferred with specific remediations prescribed.

---

## 8. Relationship to Other ADRs

This ADR is the roadmap; other ADRs provide the mechanics. Authority ordering:

- **ADR-015 (this) sets the sequence and permanence.** Any other ADR making a claim about tiers defers to this one.
- **ADR-011 provides the per-engine gate mechanics.** Changes to ADR-011 (gate thresholds, canary %, bake time) must be consistent with ADR-015 phase intent; inconsistencies are resolved by ADR-015 amendment, not silent ADR-011 drift.
- **ADR-013 provides the self-healing substrate.** Error budget mechanics gate migration pace; migration does not gate error budget.
- **ADR-014 provides the quantitative evaluation.** MC harness thresholds (KS ≥ 0.10, Spearman ≥ 0.20 / 0.30) are ADR-015 prerequisites; ADR-014 owns the thresholds' implementation, ADR-015 owns their use as gates.
- **ADR-010 provides the composer.** ADR-010 operates within ADR-015's permanent L2 boundary on selection; composer is assistive tooling, not approval authority.
- **ADR-012 provides the learning loop.** Prompt amendments are L1 permanent per §3.1; the learning loop is not a tier-migration mechanism, it's an ongoing L1 refinement.

---

## 9. Migration Risks and Mitigations

The three non-trivial risks that could invalidate the sequence:

**Risk 1 — Regulatory shift.** California Department of Financial Protection and Innovation (DFPI) or SEC guidance emerges that restricts algorithmic execution by RIAs. Mitigation: CCO monitors DFPI + SEC risk alerts monthly; trigger a compliance demotion (§5.3) on any adverse guidance. AGT retains legal counsel capable of rapid compliance review.

**Risk 2 — Composer regression in production.** ADR-010 composer's ranking quality (Spearman vs. MC forward return) degrades post-flip due to market regime change. Mitigation: ADR-014 weekly scenario regen catches per-ticker drift within one week; composer auto-disable invariant (ADR-010 §6.2) falls back to Phase 1 static digest automatically.

**Risk 3 — Kill-switch false positive cluster.** K3 (latency) or K4 (correlation drift) fires repeatedly due to infrastructure noise rather than real engine fault. Mitigation: ADR-011 §10 open question #3 explicitly flags latency threshold re-evaluation; K4 correlation threshold (0.95) is revisited after first 2 weeks of live data with actual decision-sequence variance.

Risks 1 and 3 are operational; risk 2 is the most technically interesting and is the primary reason ADR-014 must ship before Phase 2.

---

## 10. Non-Goals

Explicitly out of scope of this ADR:

- **Multi-strategy tier sequencing.** AGT runs the wheel. If a second strategy is added, that strategy's engines re-enter §4 sequencing independently; ADR-015 does not pre-define cross-strategy interactions.
- **Broker diversification tier policy.** AGT is IB-exclusive. Multi-broker considerations are deferred to a successor ADR if ever relevant.
- **Client account onboarding as a tier-migration event.** Onboarding a new advisory client is a compliance act, not a tier migration. The tier posture applies to the client's account inheriting whatever posture their IMA documents.
- **Crypto, futures, or non-equity-options expansion.** Tier framework assumes US equity + equity-options universe. Alternative asset class introduction requires a separate tier classification ADR.
- **Successor operator continuity plan.** If Yash is incapacitated or replaced as CCO, AGT's operations require a succession plan that is out of scope for this ADR. Separate business-continuity document governs.

---

## 11. Open Questions

- **Phase 1 gate window size.** 14 trailing days (per ADR-011 §2 G2) may be too short if engine decision volume is thin. Revisit after Phase 1 §G3 sample-size data comes in.
- **Composer Spearman threshold split.** §3 table uses 0.20 for composer enablement (ADR-010 MR-E.6) and implicitly 0.30 for broader confidence. Should the latter become an explicit §3 gate, or is it soft guidance? Resolve after first 2 weeks of composer production Spearman distribution.
- **Recovery clock after compliance demotion.** §5.3 says "full new ADR-011 gate cycle." Is the 14-day G2 window sufficient for post-compliance confidence, or should compliance demotions carry an extended observation window (e.g., 30 days)? Deferred to first actual compliance event — hopefully never.
- **Annual CCO-attestation ceremony for L2.** Should there be a scheduled annual re-affirmation of L2 boundaries (signed by CCO, filed with ADR archive)? Argument for: evidentiary discipline. Argument against: ceremony inflation when CCO is the sole operator and attests per-decision already. Currently deferred; revisit at first compliance consultant review.

---

## 12. Notes

- This ADR is deliberately short on engineering detail. Engineering lives in ADR-010, ADR-011, ADR-013, ADR-014. This ADR is the roadmap.
- The permanent L2 boundary (§2, §6) is the most consequential decision in the ADR. Treat it as irrevocable. Successor CCOs or Architects should not relax it without a dedicated superseding ADR that surveys the three rationale threads in §6.1 explicitly.
- Phase 1 kickoff is gated on ADR-013 v2 shipping + ADR-014 MR-D.3 (metrics module) landing. Both are on the 2026-04-19 queue.
- External review at end of Phase 2 (§7.5) is a recommendation, not a regulatory requirement. Budget cost estimate: $3–8K for a compliance consultant spot review. Plan for it in the Phase 2 operational budget.

**End of ADR-015.**
