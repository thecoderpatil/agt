# ADR-007: Self-Healing Loop Architecture

**Status:** Proposed v0.2 (cost-envelope overlay added §9)
**Date:** 2026-04-16
**Authors:** Architect (Claude) + Gemini Deep Research synthesis
**Context:** AGT Equities paper trading is autonomous today; live cutover in 4–8 weeks. The current self-healing loop (weekly Opus review + weekly Remediation task + Telegram `/approve_rem` gate) was built ad-hoc. Before live, we lock down a canonical pattern.

---

## 1. Context

Current state as of 2026-04-16:

- **Opus (weekly, Saturday 09:00 ET)** reads session logs, writes `_WEEKLY_ARCHITECT_DIRECTIVE.md` as prose. Role overloaded: detect + classify + dispatch + email in one task.
- **Remediation (weekly, Saturday 12:00 ET)** reads directive, classifies mechanical vs architectural, authors MRs, escalates to Architect.
- **Human gate** via `/approve_rem`, `/reject_rem`, 3-reject ladder → `rejected_permanently` + `needs_architect`.
- **Cadence:** weekend-only. Rail violations discovered Monday wait until Saturday.
- **Desired state:** not codified. Opus invents invariants per run.
- **MR ceremony:** not rigid. Human review is interpretive.
- **Post-merge:** no auto-rollback, no canary, no SLO watch.

Two independent research passes (Claude agent + Gemini Deep Research) converged on the same gap list. This ADR records the target shape and the implementation order.

---

## 2. Forces

Blocking for live cutover:

1. **Opus invents invariants** — "no live account_id in paper pending_orders" should be a codified rail, not a weekly judgment call.
2. **Weekly cadence violates institutional MTTR norms** — rail violations in live need minute-scale response, not week-scale.
3. **Interpretive human review** fails FINRA Rule 3110 "reasonable supervision" — an approved fix with no documented delta is indefensible.
4. **No error budget** — 3-reject is a primitive stop; a self-healing loop that keeps retrying against a pathological class of bug can destabilize the system.
5. **No audit trail separation** — SEC Reg SCI wants immutable versioned changes with authorization + testing + approval documented per change. GitOps gives this free; our ad-hoc prose directive does not.
6. **No Author/Critic split** — a single LLM authoring and self-evaluating fixes is higher-hallucination risk than a two-agent setup where the Critic enforces guardrails.

Non-blocking but high-value:

7. **Post-merge canary** — approved fix that breaks live paper autonomy needs automated revert.
8. **Rejection-learning** — reject reasons currently vanish into a JSON list, not fed back into the next attempt's prompt.
9. **Scrutiny tiers** — a docstring fix and an order-sizing fix receive the same human review weight. They should not.

---

## 3. Research Synthesis

The canonical pattern for AI-author + human-approve remediation is a **composite** of four distinct traditions, stacked:

### 3.1 Kubernetes operator / reconciliation loop
Declarative desired state → controller observes actual → diffs → applies idempotent actions → re-queues on failure with exponential backoff. Source: controller-runtime SDK, Kubebuilder good practices. Applicability: Opus should not write "fix this", it should detect a diff between a codified spec (e.g., `safety_invariants.yaml`) and observed DB/log state, emit a structured incident. Remediation reconciles the diff.

### 3.2 Jane Street Iron + "ddiff" (delta-of-delta) review
Human review is expensive; focus attention on the delta that matters. "Feature-based review" bundles logical changes, not raw commits. A "Scrutiny" metric sets different human-review depth per file path (low for config, high for order-sizing logic). Source: Jane Street tech blog. Applicability: `/approve_rem` should show a structured ddiff (what changed vs current buggy state), not a link to a raw GitLab MR. Scrutiny tier decides the gate (auto-merge vs human-only).

### 3.3 Google SRE error budgets + incident command
Mathematical stop condition: SLO defines allowed unreliability; when the self-healing loop burns budget faster than it earns it, the loop **autonomously disables itself** and alerts a human. Incident command separates roles: Commander owns state, Ops executes, Communication updates. Source: Google SRE book. Applicability: Opus = Commander (owns state doc), Remediation = Ops, Telegram = Communication, Yash = Planning. Error budget: paper = infinite (auto-approve on green CI), live = strict (Telegram gate mandatory for money-path).

### 3.4 Multi-agent Author/Critic with confidence-based escalation
Source: SHIELD framework (arXiv), AutoResearchClaw SmartPause (2026), Self-REF confidence tokens. A single agent writing and approving its own fix has no internal check. Splitting into Author + Critic catches phantom API calls, disabled security controls, and sign errors before the human sees them. Confidence tokens on the Author's output allow automated routing: high-confidence → standard gate, low-confidence → SmartPause + escalate or retry with bigger model.

### 3.5 Regulatory
- **SEC Reg SCI** — immutable versioned audit trail per change. GitOps satisfies this; every commit IS the audit entry.
- **FINRA Rule 3110** — "reasonable supervision" requires documented review. ddiff + CI pass + reviewer's `/approve_rem` with incident_id satisfies.
- **SOX ITGC** — authorize → test → approve → deploy. Our loop maps: Opus detects (authorize), CI runs (test), `/approve_rem` (approve), merge + bot picks up (deploy).

---

## 4. Decision

Adopt the following architecture. Names in `CAPS` are new components.

### 4.1 Codified desired state: `SAFETY_INVARIANTS`
A versioned file at `agt_equities/safety_invariants.yaml` with machine-checkable rails. Initial set:

```yaml
invariants:
  - id: NO_LIVE_IN_PAPER
    kind: sql_must_return_zero
    paper_mode_only: true
    query: |
      SELECT COUNT(*) FROM pending_orders
      WHERE json_extract(payload, '$.account_id') IN
        ('U21971297','U22076329','U22076184','U22388499')
  - id: NO_BELOW_BASIS_CC
    kind: sql_must_return_zero
    query: |
      SELECT COUNT(*) FROM pending_orders po
      WHERE json_extract(po.payload,'$.right')='C'
        AND json_extract(po.payload,'$.action')='SELL'
        AND json_extract(po.payload,'$.strike') <
            (SELECT paper_basis FROM ... -- TBD in Sprint)
  - id: NO_ORPHAN_CHILDREN
    kind: sql_must_return_zero
    query: |
      SELECT COUNT(*) FROM pending_order_children c
      JOIN pending_orders p ON c.parent_id = p.id
      WHERE p.status IN ('filled','superseded') AND c.status='sent'
```

Rules:
- Every new rail violation detected by Opus becomes a new invariant. Opus cannot "escalate" without first checking whether codification is possible.
- `scripts/check_invariants.py` runs against the DB on every scheduler heartbeat (currently 60s). Violations enqueue an `INCIDENT` row.

### 4.2 Structured incident queue: `incidents` table
Replaces prose directive as the machine-readable source of truth.

```sql
CREATE TABLE incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_key TEXT NOT NULL,        -- stable hash (e.g., ORDER_266_LIVE_ACCOUNT_GUARD)
  invariant_id TEXT,                  -- matches SAFETY_INVARIANTS.yaml if applicable
  severity TEXT NOT NULL,             -- info | warn | crit
  scrutiny_tier TEXT NOT NULL,        -- low | medium | high | architect_only
  detected_at TEXT NOT NULL,
  detector TEXT NOT NULL,             -- invariant_check | opus_weekly | manual
  observed_state JSON,                -- what was detected
  desired_state JSON,                 -- spec being violated
  confidence REAL,                    -- 0.0-1.0 from detector
  status TEXT NOT NULL,               -- open | authoring | awaiting_approval | merged | rejected_* | needs_architect
  mr_iid INTEGER,
  ddiff_url TEXT,
  rejection_history JSON              -- [{at, reason, from_status}]
);
```

Opus's role collapses to: **read incidents table, cluster related events, write a weekly human-readable summary email.** Opus does not write to `_WEEKLY_ARCHITECT_DIRECTIVE.md` anymore (the file is retired).

### 4.3 Continuous detection, batch remediation
- **Detection** runs on scheduler heartbeat cadence (60s). `scripts/check_invariants.py` reads `safety_invariants.yaml`, runs each query, writes `INSERT INTO incidents` for each violation. Idempotent by `incident_key`.
- **Remediation** runs:
  - **Event-driven for paper** — any new `crit` incident in paper-mode pending_orders triggers the Remediation task within 5 minutes.
  - **Weekly for live** — every Saturday 12:00 ET, batched for human review efficiency (FINRA 3110 "reasonable supervision").
- **Opus (summary only)** runs weekly Saturday 09:00 ET, reads last week of `incidents` + `autonomous_session_log`, emails Yash the digest. No directive file.

### 4.4 Author + Critic split
Remediation spawns two sub-agents:
- **Author** — reads incident, authors branch + fix + regression test. Produces a confidence score.
- **Critic** — reads Author's output + the `safety_invariants.yaml` + a rejection-checklist (phantom API calls, removed security guards, sign errors). Approves or rejects-with-reason.

Only Critic-approved fixes go to the human. Critic-rejected fixes loop back to Author with the reason (max 2 internal loops before human escalation).

### 4.5 Scrutiny tiers + error-budget gate
Each incident gets a `scrutiny_tier` by invariant:

| Tier | Examples | Gate |
|---|---|---|
| `low` | Config drift, version bump, docstring, missing import | Auto-merge on CI green (paper only) |
| `medium` | Defense-in-depth guard, new test, scheduled task | Telegram `/approve_rem` (any mode) |
| `high` | Order-sizing logic, pricing model, new invariant, schema change | Multi-step approval: Yash reviews ddiff + explicit `/approve_rem --tier high` |
| `architect_only` | Walker, flex_sync, kill switch logic | Never autonomous; incident becomes a Notes item |

**Error budget:** monthly SLO of 99% clean autonomous paper execution (measured as (successful_runs / total_runs) on scheduler + auto-executor). Budget = 1% ≈ 7 hours/month of tolerated unreliability. If consumed by > 50% before the month ends, `low`-tier auto-merge disables itself. All MRs require human review until the next monthly reset. Hard freeze if consumed 100%.

### 4.6 ddiff presentation
`/list_rem` and the remediation Telegram message no longer link to GitLab raw MRs. Instead, they link to a generated ddiff URL showing:
- **Before:** current buggy state (observed_state from incident row)
- **After:** state post-fix (authored diff)
- **Delta:** how the fix compares to the current buggy state, not vs main
- **CI:** green checkmark + which tests ran
- **Scrutiny:** tier badge

Implementation: static HTML generator at `scripts/gen_ddiff.py` writing to `logs/ddiffs/` + served via the existing FastAPI Cure Console (currently a placeholder — this gives it real purpose).

### 4.7 Rejection-learning: ALHF-lite
`/reject_rem <id> <reason>` REQUIRES a non-empty reason (already enforced in `telegram_bot.py`). New: the reason is prepended to the Author's next attempt's context window as `PRIOR REJECTION: <reason>`. After 3 rejections, escalate to `needs_architect` as today. Over time, recurring rejection reasons cluster into new invariants (added to `safety_invariants.yaml` by the next Opus run).

### 4.8 Post-merge canary
After `/approve_rem` merges an MR, a one-shot scheduled task (`canary_<incident_key>`) runs 30 minutes post-merge. It re-runs `check_invariants.py` + greps `autonomous_session_log` for new errors. If the invariant re-trips OR new error rate exceeds baseline, the task:
1. Writes `INSERT INTO incidents` with severity=`crit`, detector=`canary`
2. Alerts Yash via `alert_yash` severity=`crit`
3. **Does NOT auto-revert** (we don't trust that yet). Yash manually merges the revert MR.

Auto-revert is a deliberate non-goal for v1 — too easy to pathologically flap.

---

## 5. Consequences

### Accepted trade-offs
- **Codified invariants must be exhaustive enough to catch classes of bugs** — if the invariant list is thin, Opus falls back to human escalation. That's acceptable; we iterate by adding invariants from observed incidents.
- **Author/Critic doubles LLM cost per remediation.** Acceptable for the correctness win. Gate with cost-cap if it runs away.
- **Event-driven paper remediation can fire during market hours.** But it only authors MRs — bot isn't restarted, trading isn't paused. The old "weekend only" policy was about human attention, not trading impact. Retained for live.
- **ddiff generator is new UI surface** — additional maintenance burden. Offset by retiring the prose directive file.

### Non-goals
- Auto-revert on canary failure (v2)
- LLM fine-tuning on rejection history (would require training pipeline — not our style)
- Real-time drift monitoring against live broker statements (separate sprint)
- Merging the Cure Console placeholder with ddiff generator (separate cleanup)

### Risks
- **Invariant false positives** — a too-tight SQL query flags clean state as violation. Mitigation: every new invariant requires a test in `tests/test_invariants.py` that asserts it's false against current main state before landing.
- **Critic too lenient** — Author authors phantom fix, Critic approves. Mitigation: Critic prompt includes explicit "before approving, verify every function call exists in the codebase" + regression tests executed pre-Critic-review.
- **Error budget too loose** — 99% SLO on paper is aspirational; measuring is first task. Start logging and backfill the SLO once we have 4 weeks of baseline.

---

## 6. Implementation roadmap

Ordered MRs, each self-contained:

1. **ADR-007 commit** — this file, to `docs/adr/` + `.gitignore` the old `_WEEKLY_ARCHITECT_DIRECTIVE.md`. Closes the boot_desk.bat-wipes-directive bug.
2. **`safety_invariants.yaml` + `scripts/check_invariants.py` + `tests/test_invariants.py`** — codify the 3 known rails (NO_LIVE_IN_PAPER, NO_BELOW_BASIS_CC, NO_ORPHAN_CHILDREN). CLI: `python3 scripts/check_invariants.py [--json]`. Test against current DB asserts zero violations for existing-good state.
3. **`incidents` table schema + CRUD module** — `agt_equities/incidents.py` parallels `remediation_incidents` but generalizes. Migration in `schema.py`. Deprecates `remediation_incidents` (keep both for one sprint, dual-write, drop after validation).
4. **Invariant-triggered incident writes** — `scripts/check_invariants.py` inserts `incidents` rows on violation. Wire into scheduler heartbeat (currently 60s).
5. **Retire `_WEEKLY_ARCHITECT_DIRECTIVE.md`** — DONE. Opus task prompt consumes `scripts/incidents_digest.py` (markdown or JSON) instead of parsing the prose file. `/list_rem`, `/approve_rem`, `/reject_rem` redirected off `remediation_incidents` onto `incidents_repo.list_by_status` / `get_by_key(active_only=True)`; args accept numeric id or ALL_CAPS key. `scripts/circuit_breaker.py::check_directive_freshness` renamed to `check_incident_detector_heartbeat` (8h threshold against `MAX(last_action_at)`). Legacy `remediation_incidents` table kept dual-written for one more sprint (see section 8 below).
6. **Author/Critic split in remediation task prompt** — two LLM calls per incident. Confidence tokens captured in `incidents.confidence`.
7. **Scrutiny tiers + error budget** — each invariant has a `scrutiny_tier`; `scripts/error_budget.py` computes monthly burn + gates `low`-tier auto-merge. Dashboard row in `/report`.
    - **(a) DONE (MR !91, 2026-04-17):** `max_consecutive_violations` from `safety_invariants.yaml` is now enforced downstream via `incidents_repo.list_authorable()` + `scripts/incidents_digest.py --authorable`. Flappy invariants (max=3 or 5) no longer burn LLM spend on first detection; below-threshold rows stay visible in `/report` and are summarized (counts only) in a `## Below threshold` section of the authorable digest.
8. **ddiff generator** — `scripts/gen_ddiff.py` + static HTML template, served from FastAPI. `/list_rem` and Telegram messages link to ddiff URL instead of GitLab raw.
9. **Post-merge canary** — one-shot scheduled task per merge. Re-runs invariants at T+30min. Alerts on re-trip. No auto-revert.
10. **ALHF-lite** — Author prompt template includes `{PRIOR_REJECTIONS}` interpolation.

Each MR follows the rigid ceremony: regression test required, CI green required, ddiff presented, scrutiny tier set. This ADR is tier=`architect_only` — human-only, no autonomous authoring.

---

## 7. Citations

Primary sources:
- Google SRE Book: Managing Incidents, Embracing Risk, Eliminating Toil
- Anthropic Engineering: Building Effective Agents; Effective Harnesses for Long-Running Agents
- Kubernetes Controller-Runtime SDK + Kubebuilder Good Practices
- Jane Street Tech Blog: Iron / Feature-Based Review / Scrutiny
- SEC Regulation SCI; FINRA Rule 3110; SOX ITGC
- arXiv: SHIELD (self-healing intelligent evolving LLM defense); Self-REF (confidence-token self-reflection); AutoResearchClaw HITL intervention modes
- GitHub Docs: Dependabot / CodeQL Autofix architecture

---

## 8. Open questions

- Where does `safety_invariants.yaml` live in the repo — next to `walker.py` (read by many consumers) or in `config/` (less coupling)?
- Should the ddiff generator be a skill-skill (Cowork-local) or a real repo artifact? Real artifact wins on audit trail but adds maintenance.
- How do we codify "order-sizing logic" as an invariant? SQL won't reach it. Likely needs a Python-callable invariant kind in addition to `sql_must_return_zero`.
- ~~Do we keep `remediation_incidents` or fully replace with `incidents`?~~ RESOLVED Step 5 (2026-04-16). Dual-write continues for one more sprint so the existing weekly remediation pipeline and any ad-hoc scripts that still read the legacy table keep working. Retirement MR will: (a) archive remaining `new` / `authoring` / `awaiting_approval` rows into `incidents` via a one-shot migration script, (b) drop `_mirror_register` + `_mirror_update_status` from `agt_equities/incidents_repo.py`, (c) drop `remediation_incidents` DDL from `agt_equities/schema.py`, (d) remove the back-compat `check_directive_freshness` shim.

---

## 9. Compute budget and tier assignment (v0.2, 2026-04-16)

The SHIELD/Jane Street composite from §3 was built for enterprise budgets 10–100× ours. AGT's compute landscape is different: **three tiers of near-zero-marginal compute stacked together**, with zero enterprise-API line items.

**Available tiers:**
- **T1 — Anthropic Max ($250/mo):** quota-based, covers Cowork Architect sessions + scheduled tasks (Sonnet default, Opus available). 5-hour rolling window.
- **T2 — ChatGPT Pro + Gemini AI Pro subscriptions:** near-unlimited chat/Deep Research for manual Architect escalation.
- **T3 — Prepaid Anthropic + Gemini API credits:** per-token spillover; used only if quota saturates or for subagents that need a distinct key.

**Tier assignment:**
- Autonomous loop (scheduled authoring, Critic escalations, weekly digest) → **T1**
- `needs_architect` deep reasoning (manual) → **T2**
- Specialized subagents (e.g., a second-opinion Critic with a different provider) or T1-saturation spillover → **T3**

Because T1 is quota-capped rather than USD-metered, the stop-conditions in §9.2 are about **not starving Architect sessions** and **rate-limit survival**, not per-token spend. The §9.1 mechanical Critic is retained but re-motivated: its win is **latency + determinism**, not LLM cost avoidance.

### 9.1 Critic is mechanical by default, not a second LLM

Replace the "Author LLM + Critic LLM" split from §4.4 with a two-tier Critic:

**Default Critic = mechanical pipeline** (zero LLM cost):
- `pytest -x -q` on changed paths + regression test authored by Author
- `ruff check` + `pyflakes` for syntax + phantom-import detection
- Path-whitelist check: no changed files in `walker.py`, `flex_sync.py`, `cure_*.html`, `tests/test_command_prune.py`, or any path mapped to `scrutiny_tier=architect_only`
- Grep-based phantom-API check: every new `.call(` target must resolve in the import graph
- Invariant-regression check: `scripts/check_invariants.py` against the branch's DB state

**Escalation Critic = single LLM call**, only when:
- `scrutiny_tier == high` AND Author-reported confidence < 0.6
- Hard limit: one call, no internal retry loop

Critic-rejected fixes escalate to human directly. The one exception: pytest failure gets the Author a single fixup attempt (max 1 re-author). Hard cap: **max 4 LLM calls total per incident** (1 Sonnet Author + ≤1 Sonnet fixup + ≤1 Opus SmartPause retry per §9.4 + ≤1 LLM-Critic at high-tier).

Under Max plan (T1), this is ~0 marginal USD. Under API fallback: ≈$0.30 at Sonnet pricing, up to ≈$1.30 worst-case with Opus retry. At 10 incidents/week worst-case ≈$52/month — comfortably inside the §9.2 API fallback cap ($75/mo).

### 9.2 Quota-aware budget — two metering modes

AGT's scheduled tasks run under the Anthropic Max plan ($250/month), not the developer API. This means autonomous-authoring LLM cost is **$0 marginal** for the first quota tier — what matters is the rolling 5-hour quota window, not per-token USD.

Two budget knobs in parallel:

**Quota mode (Max plan, default):**
- Autonomous authoring must not consume more than **50% of the 5-hour rolling Max quota**. The other 50% is reserved for Yash's manual Architect sessions (they must stay responsive).
- Estimate the ceiling from observed `input_tokens + output_tokens` per incident × rate of incidents. Log to `autonomous_session_log.llm_usage` JSON column (added in Step 3 schema migration).
- On quota-pressure signal (HTTP 529 / rate-limit error from Anthropic), autonomous authoring backs off with exponential delay (1h → 2h → 4h) and the breach event goes to `alert_yash` severity=`warn`.

**API mode (fallback, if a task ever runs against the developer API):**
- Default cap: **$75/month** autonomous-authoring USD spend (sized for worst-case 10 incidents/week with Opus retry enabled — see §9.1).
- Cap breach → autonomous authoring halts until the 1st. Fallback: weekly batched review (v1 cadence).
- Adjustable via env var `AGT_MONTHLY_LLM_CAP_USD`.

Both modes share the same stop-condition semantics: on breach, autonomous authoring disables itself, queue flushes to `needs_architect`, Yash is notified. The Max plan is the primary lever; API cap is the defense-in-depth fallback if the scheduler ever authenticates via API key.

### 9.3 Incident deduplication + rate limit

Remediation does **not** re-author when an `incidents` row already exists with the same `incident_key` in status `open | authoring | awaiting_approval`. Same key = append observation to existing row's `observation_history`, no new LLM call.

This prevents pathological-bug cost explosion: if a bug generates 20 violations/hour of the same invariant, we author one fix, not twenty.

Rate limit: max **5 new incident authorings per hour** globally (across all keys). Excess incidents queue to the next hour. Prevents API rate-limit + cost spikes from incident storms.

### 9.4 SmartPause-to-Opus: opt-in bounded retry

Gemini Deep Research's SmartPause pattern ("on low confidence, route to more powerful LLM") is **enabled as a feature-flagged bounded retry** under the Max plan. The cost objection collapses — Opus calls are quota-metered, not USD-metered — leaving only rate-limit and determinism concerns, both bounded below.

**Trigger conditions (all must hold):**
- Author confidence < 0.6
- `scrutiny_tier in (medium, high)` — never for `low` (low auto-merges on green CI anyway)
- Feature flag `AGT_AUTHOR_RETRY_WITH_OPUS=1` (default off; enable after 1 month of baseline metrics to verify no quota-starvation pattern)

**Bounds:**
- Exactly 1 Opus retry per incident. No retry loop on the Opus call itself.
- Opus retry counts against the per-incident total in §9.1 (revised cap: max **4 LLM calls per incident** = 1 Sonnet Author + ≤1 Sonnet fixup on pytest-fail + ≤1 Opus retry + ≤1 LLM-Critic escalation).
- If Opus also returns confidence < 0.6 → straight to `needs_architect`, no further retries.

**Escalation ladder for `needs_architect` incidents** (exhausts T1, moves to T2):
1. Yash opens the ddiff + incident row in a Cowork Architect session (T1 quota).
2. If deeper reasoning needed, Yash lifts the incident summary into ChatGPT Pro or Gemini 2.5 Pro / Deep Research (T2, zero marginal cost).
3. Conclusions fed back into a fresh Cowork session as the kickoff for an authored fix.

Opus is therefore invoked on the autonomous path in exactly two places: (a) weekly digest from §4.3, and (b) opt-in low-confidence Author retry here. Both bounded.

### 9.5 Roadmap impact

Step 7 (§6) expands scope to "scrutiny tiers + error budget + **cost cap**". No new roadmap step. Step 6's "Author/Critic split" becomes "Author + mechanical Critic with optional LLM-Critic escalation."
