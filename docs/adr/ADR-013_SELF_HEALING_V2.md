# ADR-013 — Self-Healing Loop v2

**Status:** Draft
**Date:** 2026-04-19
**Author:** Architect (Cowork, Opus)
**Inputs:** RULING_ADR_BACKLOG_20260419.md §3.3 (SHIP items 9–12) + DR-3 + Sonnet self-healing research
**Supersedes:** None — this is the next evolution of ADR-007, not a replacement.
**Related:** ADR-007 (Self-Healing Loop), ADR-008 (Shadow Scan), ADR-011 (Live-Execution Promotion — kill switches feed incidents), ADR-012 (Learning Loop — prompt amendments are governed by §3 error budget here), ADR-015 (Tier Migration Roadmap)

---

## 1. Context

ADR-007 shipped through Step 7b (MR !94, hydrate-on-reconnect) by 2026-04-17. The Self-Healing Loop v1 surface today provides:

- An invariants module (`agt_equities/invariants/checks.py`) running per-minute against the production DB.
- An `incidents` table with detect/resolve lifecycle.
- An Author-Critic LLM split (MR !81 / Step 6) where the Author proposes patches and a mechanical Critic checks them against tests.
- A flat 5-MR-per-week cap as a remediation throttle.
- A daemon heartbeat invariant (`NO_MISSING_DAEMON_HEARTBEAT`).
- Saturday-only weekly remediation pipeline.

Three concrete frictions have surfaced since Step 7b:

**Friction 1 — Invariant violations only catch state, not schema.** When ADR-008 pushed `OrderSink` through every engine, several ingest-time bugs slipped past the minute-tick invariants because the data was structurally wrong before it ever wrote to a table the invariants checked. Per-minute SQL is the wrong layer for "the shape of this Python dict is broken."

**Friction 2 — Flat 5-MR cap is overcautious for low-severity work and undercautious for live-capital work.** Tier-2 observability patches eat the same slot as Tier-0 invariant fixes. We ran into this twice in the ADR-008 sprint when we wanted to ship a docs-only MR Tuesday but were already at quota from Monday's incident response.

**Friction 3 — Critic process inherits Author context.** Step 6 runs the Critic in the same Claude session as the Author. Despite separate-conversation framing, both are Sonnet, both share recent system prompt, and we have observed at least one case (MR !85 venv-launcher false positive) where the Critic missed a defect the Author had subtly waved away. Sycophancy is the real risk; full subprocess isolation is the cheap mitigation.

This ADR defines the next evolution: a Pydantic v2 invariant layer at the API boundary (catching Friction 1), a severity-weighted dual-ledger error budget (Friction 2), and isolation-with-evidence for the Critic (Friction 3). Plus time-based canary baking on every post-merge main commit.

What this ADR does **not** do: it does not replace the per-minute SQL invariants (those still run, in addition), it does not introduce multi-vendor model diversity (deferred per ruling §2), and it does not break ADR-007 backward compatibility — every ADR-007-era invariant continues to fire under v2.

## 2. Invariant Enforcement Layer — Pydantic v2 + checks.py

Two layers, each owning the failure mode it is best at.

**Layer 1 — Pydantic v2 at the API boundary.** Every function in `agt_equities/` that accepts data from outside the package (Telegram input, IBKR `ib_async` callbacks, Flex XML ingest, scheduler ticks) gets a Pydantic model wrapping the input. Validation runs at the boundary, not deep in business logic. Failures raise `ValidationError`, get caught at the engine entry-point, and write a Tier-1 incident (`SCHEMA_DRIFT_<engine>`).

Concrete scope for first shippable:

- `agt_equities/schemas/` (new package)
- `IBOrderEvent`, `IBPositionEvent`, `IBExecutionEvent` Pydantic models for `ib_async` callbacks.
- `TelegramCommand`, `TelegramApprovalDecision` models for the bot's message handlers.
- `FlexTradeRow`, `FlexPositionRow` models for `flex_sync_eod` ingest.
- `SchedulerTickEvent` for the scheduler daemon.
- `CSPDigestPayload` for the LLM call boundary (cross-feeds ADR-012 Dispatch A's `raw_input_hash` — same hash should be derivable from this Pydantic model's `.model_dump()`).

Pydantic v2 is already an implicit transitive dependency (FastAPI Cure Console, several `ib_async` paths). Pinning to a specific minor version explicitly in `requirements.txt` is in scope.

**Layer 2 — checks.py per-minute SQL invariants.** Unchanged from ADR-007. Continues to catch the failure modes Pydantic does not see: stale daemon heartbeat, drifting position counts, breaker thresholds, Walker reconciliation gaps, master-log integrity. These are runtime-state invariants, not schema invariants. Different failure mode, different layer.

The two layers do not share code. They share the `incidents` table.

## 3. Error Budget — Severity-Weighted Rolling 72h Dual-Ledger

Replaces the ADR-007 flat 5-MR-per-week cap.

### 3.1 Severity tiers

| Tier | Definition | Burn weight | Examples |
|------|------------|-------------|----------|
| **Tier 0** | Live-capital invariant breach. Position correctness, kill-switch trip, breaker trip on real money, schema drift on order-emitting code path. | **100** | NO_PHANTOM_FILLS, K1 session DD trip on live engine, Walker reconciliation drift > $100, ACB segregation violation |
| **Tier 1** | Portfolio-math or paper-execution invariant breach. Incorrect calculation that did not yet touch live capital, paper-mode kill-switch trip, schema drift on a non-emitting path. | **10** | Glide-path math drift, paper-engine K1 trip, harvest threshold mis-applied, CSP allocator double-counts notional |
| **Tier 2** | Observability, logging, or operational hygiene. No money or correctness impact; degraded ops surface only. | **1** | Heartbeat-stale alert that resolved cleanly, Telegram delivery retry, log rotation lag, doc-only inconsistency |

Tier classification is encoded per-invariant at definition time in `checks.py` and on every Pydantic boundary failure. Reclassification requires an ADR amendment.

### 3.2 Budget mechanics

Rolling 72-hour window. Burn = sum of (incident_count × tier_burn_weight) over the window. Window slides forward continuously, not bucketed.

| Status | Total burn (rolling 72h) | Effect |
|--------|--------------------------|--------|
| **Green** | < 200 | Normal operation. All MR tiers permitted. |
| **Amber** | 200 – 499 | Tier-2 MRs blocked. Tier-0 and Tier-1 permitted. Architect alerted at first Amber entry. |
| **Red** | 500 – 999 | All MRs blocked except those that reduce burn (revert, fix, mitigation). Yash explicitly approves every MR. |
| **Black** | ≥ 1000 | Autonomous pipeline halted. Bot enters read-only mode. Architect drafts post-mortem before any MR. |

Why these thresholds: a single Tier-0 incident (100) is amber. Two Tier-0s (200) is amber edge. A Tier-0 plus a kill-switch trip (200) is amber. Five Tier-0s in 72 hours is Red. A Knight-Capital-class compounding failure (10 Tier-0 incidents) is Black. Tier-2 chatter alone — even 100 entries in 72h — stays Green at 100 burn. The math weights what matters.

### 3.3 Dual ledger — fault attribution

Every incident carries a `fault_source` column:

| `fault_source` | Definition | Counts toward AGT burn? |
|----------------|------------|--------------------------|
| `internal` | Caused by AGT code, AGT config, or AGT operator action. | **Yes** |
| `broker` | Caused by IBKR Gateway, IB OMS, or IB account-state change AGT observed but did not cause. | **No** (logged but does not burn) |
| `exchange` | Exchange halt, OPRA outage, market maker withdrawal, settlement glitch. | **No** |
| `vendor` | GitLab CI quota, Anthropic API outage, Telegram bot throttle, OneDrive sync stall. | **No** |

A heartbeat-stale alert during the 2026-04-16 GitLab CI quota outage was AGT-internal-counted under v1. Under v2 it would attribute to `vendor` and not burn AGT's budget. Same for the 1190 paper-account UNKNOWN_ACCT errors (broker config mismatch, not AGT defect).

The dual ledger is **logged unconditionally** on every incident — even external faults are persisted, because they inform regression analysis ("is the IBKR Gateway flakier this week than last?"). They just do not consume AGT's autonomy budget.

### 3.4 Burn telemetry

Architect Saturday review reads the rolling-72h burn from a SQL view:

```sql
CREATE VIEW v_error_budget_72h AS
SELECT
  SUM(CASE WHEN fault_source = 'internal' THEN burn_weight ELSE 0 END) AS internal_burn,
  SUM(CASE WHEN fault_source != 'internal' THEN burn_weight ELSE 0 END) AS external_burn,
  COUNT(*) FILTER (WHERE severity_tier = 0 AND fault_source = 'internal') AS tier0_count,
  COUNT(*) FILTER (WHERE severity_tier = 1 AND fault_source = 'internal') AS tier1_count,
  COUNT(*) FILTER (WHERE severity_tier = 2 AND fault_source = 'internal') AS tier2_count
FROM incidents
WHERE detected_at >= datetime('now', '-72 hours');
```

Cron at 09:00 ET Sat reads this view, posts to Telegram if Amber/Red/Black, surfaces in the Architect kickoff prompt.

## 4. Author + Critic with Subprocess Isolation

Replaces the same-session Author-Critic of ADR-007 Step 6.

**The change in one sentence:** the Critic runs in a fresh subprocess (`env -i`, no inherited environment, no inherited memory, no system prompt sharing), and receives only deterministic runtime evidence — not the Author's reasoning chain.

### 4.1 Evidence bundle

The Critic gets exactly four artifacts as serialized JSON over stdin. Nothing else.

```json
{
  "diff": "<git unified diff against the parent commit>",
  "test_output": "<verbatim pytest stdout/stderr from the dispatched test scope>",
  "ast_structure_delta": {
    "functions_added": [...],
    "functions_removed": [...],
    "functions_signature_changed": [...],
    "imports_added": [...],
    "imports_removed": [...]
  },
  "dependency_graph_delta": {
    "modules_now_importing": {...},
    "modules_no_longer_importing": {...}
  }
}
```

The Critic does not see: the Author's reasoning, the dispatch text, prior conversation, the codebase outside the diff, the user's intent statement, the Architect's design document. It sees what the change *is*, what the tests *do*, and what the call graph now *looks like* — nothing about why the Author thinks the change is correct.

### 4.2 Critic verdict

The Critic emits one of:

```
{ "verdict": "approve", "rationale": "<one paragraph>" }
{ "verdict": "request_changes", "rationale": "<one paragraph>", "specific_concerns": [...] }
{ "verdict": "block", "rationale": "<one paragraph>", "blocking_invariant": "<which invariant the change appears to violate>" }
```

Block is reserved for invariant-touching changes (any file under `agt_equities/invariants/`, any change to `checks.py` thresholds, any Pydantic model where the change is field-removal or type-narrowing). Block requires a Yash override to merge.

### 4.3 Subprocess invocation

```python
import subprocess, json, os
p = subprocess.run(
    ["python", "-m", "agt_equities.critic.subprocess_runner"],
    input=json.dumps(evidence_bundle),
    env={"PATH": os.environ["PATH"], "ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]},  # nothing else
    capture_output=True,
    text=True,
    timeout=120,
)
verdict = json.loads(p.stdout)
```

The `env` dict is explicitly minimal. Critical: no `AGT_*` variables, no `PYTHONPATH` manipulation, no model-version overrides. The Critic gets a clean room. If the Critic needs to know the rulebook, it reads it from disk like any other process.

### 4.4 Why not multi-vendor

Per ruling §2 deferral. Multi-vendor (Opus + GPT-5.4 + Gemini 3 Pro consensus) addresses the same sycophancy concern but requires three vendor integrations, three billing surfaces, three rate-limit regimes, and a consensus-arbitration layer. Subprocess isolation captures ~80% of the benefit at ~20% of the cost. We re-evaluate multi-vendor at the first documented sycophancy incident on the v2 isolated Critic — not before.

## 5. Time-Based Canary Baking — Per-Commit

Every post-merge main commit must survive a continuous market session before the autonomous remediation pipeline can ship the next change. "Continuous market session" = pre-open 04:00 ET → post-close 20:00 ET.

### 5.1 Bake clock

Cron at every 15 minutes during 04:00–20:00 ET. Reads `git log -1 --format=%ct main` and computes `now - last_main_commit_seconds`. Records bake-progress to a `canary_bake_state` table.

### 5.2 Bake outcome

If at any point during the bake window an incident with `severity_tier <= 1 AND fault_source = 'internal'` is recorded against the post-merge main:

1. The bake fails.
2. An automatic git revert commit is generated by the autonomous remediation pipeline (no Author, no Critic — pure mechanical revert).
3. The revert is pushed to a new MR with `auto_revert = true` flag, auto-approved, auto-merged at next CI green.
4. A Tier-1 incident `BAKE_FAILED_<original_squash_sha>` is recorded.

If the bake completes (~12-16 hours of session-time) with no Tier-0 or Tier-1 internal incident:

- The commit is marked `bake_passed = true` in `canary_bake_state`.
- The next remediation MR is permitted to ship.

### 5.3 Bake exemptions

Three commit categories skip the bake:

- **Docs-only commits** (every changed file under `docs/`, `reports/`, or matching `*.md`).
- **Auto-revert commits** (the revert that fixes a failed bake — we do not bake the bake-fix).
- **Yash-override commits** with explicit `[skip-bake]` in the commit message and a documented justification.

Tier-2-only fixes still bake. We do not relax bake for "small" fixes — the whole point of canary is that small fixes are how Knight Capital happens.

### 5.4 Why not statistical-volume canary

Per ruling — at AGT's volume, statistical-volume canary takes weeks per change. Time-based canary aligns with how this system actually fails: the failure modes are temporally bound (an EOD process miscomputing, a market-open scheduler misfiring), not volume-bound. One full session is the minimum unit of behavioral evidence for a code change.

## 6. Behavioral Drift Logging

Concept adopted; no new framework name (per ruling §1 hallucination guard).

The CSP digest LLM (and any future LLM-driven ranking surface) writes to `decisions.llm_confidence_score` (ADR-012). A weekly cron computes the distribution of confidence scores over the trailing 7 days vs the trailing 28 days and writes a Tier-2 incident `LLM_CONFIDENCE_DRIFT_<engine>` if the K-S test rejects distribution-equality at p < 0.01.

That is the entire behavioral-drift mechanism. No "Agent Stability Index" subsystem, no separate drift-detection module, no version branding. One cron, one K-S test, one incident type. The signal is real; the framework name is not.

## 7. Non-Goals

Explicit rejections:

- **Multi-vendor model diversity ("Star Chamber").** Deferred to first sycophancy incident. See §4.4.
- **OpenTelemetry / topological knowledge graph.** Flat SQLite incidents table is adequate at our incident volume (< 50/week). Revisit at > 50/day.
- **Event-driven replacement of Saturday remediation pipeline.** We already have Architect wake-on-demand for emergencies. Saturday cadence is for non-critical accumulated debt, not urgent live-capital response.
- **Formal verification (TLA+, Dafny, Liquid Haskell).** Both Sonnet and DR-3 agree this is overkill for single-broker single-operator architecture. Revisit only if multi-broker order aggregation appears.
- **DafnyPro framework citation.** Flagged as probable hallucination in the ruling. **Not cited in this ADR.** If verification clears it, no action — the technique (LLM-generated annotations on Dafny proofs) is not in our adoption path regardless.
- **"Finance Error Budgeting Framework" (FEBF) as a named external framework.** The mechanism is adopted (§3); the acronym is not. Just describe the math.
- **Per-commit canary bake skip for "trusted" authors.** No tier of trust skips bake. The bake exists because trusted humans wrote the Knight Capital code.

## 8. First Shippable Sub-Dispatch

**Dispatch B (Codex-tier, ~80 LOC).** Migration script + backfill.

1. `scripts/migrate_incidents_dual_ledger.py` — `ALTER TABLE incidents ADD COLUMN fault_source TEXT NOT NULL DEFAULT 'internal'; ALTER TABLE incidents ADD COLUMN severity_tier INTEGER NOT NULL DEFAULT 1; ALTER TABLE incidents ADD COLUMN burn_weight REAL NOT NULL DEFAULT 10;`
2. Backfill rules for existing incidents: known broker/exchange/vendor incidents (heartbeat-stale during GitLab outage, paper UNKNOWN_ACCT errors) reclassified by name pattern; default `internal` + tier-1 + weight-10 for all others.
3. View `v_error_budget_72h` per §3.4.
4. Unit tests `tests/test_incidents_dual_ledger.py` asserting backfill correctness on a fixture DB.

Ships independently. No code on the hot path changes — the existing `incidents_repo.create()` continues to write rows; Pydantic boundary work + Critic isolation + canary bake are subsequent dispatches off the back of this schema.

**Downstream dispatches (not this turn):**

- Pydantic boundary models — Coder-tier, ~400 LOC across 5-8 files. Gated on Dispatch B.
- Critic subprocess runner — Coder-tier, ~250 LOC. Gated on Dispatch B + Pydantic landing (Critic uses Pydantic to validate evidence-bundle shape).
- Canary bake clock — Coder-tier, ~150 LOC + cron config. Gated on Dispatch B (uses `severity_tier` filter).
- Behavioral drift K-S cron — Codex-tier, ~80 LOC. Gated on ADR-012 Dispatch A landing (needs `decisions.llm_confidence_score` populated).

## 9. Open Questions

- **Is `fault_source = 'broker'` distinguishable from `internal` in every case?** Some incident types (heartbeat-stale, daemon-missing) could plausibly be either. The migration's name-pattern backfill is best-effort. New incidents going forward must classify at create-time, which means `incidents_repo.create()` gains a required `fault_source` argument. Coder-side decision at Dispatch B implementation: enforce required argument, or default + warn-log for backward compat.
- **Burn-weight tuning.** The 100/10/1 weights are first-principles calibrated. Real-world burn distributions over the first 60 days may show one tier dominating in a way that argues for re-weighting. Revisit at first ADR-013 amendment.
- **Bake window during exchange holidays.** Half-day Fridays + full-day holidays compress the bake clock. Default behavior: bake counts only continuous-session minutes, so a half-day Friday produces ~6 hours of bake credit instead of ~16. Acceptable for ~10 trading days/year. Document but do not special-case.
- **Critic timeout.** §4.3 sets 120s. Sonnet typical Critic latency is ~30-60s for a small diff. Large diffs (Coder-tier > 300 LOC) may exceed. Decision deferred: either chunk the evidence bundle or extend timeout to 300s based on actual Critic invocation telemetry.
- **Bake on a Yash-override `[skip-bake]` commit — does the *next* commit still need to bake?** Currently yes (next non-exempt commit bakes from its own merge time). Argument for resetting the clock: a skipped bake means we did not actually validate the prior commit, so the next bake is bearing more risk. Argument against: would compound to weeks of rolling skip if Yash overrides repeatedly. Default: next commit bakes normally; skip-overrides accumulate as a Tier-2 incident `BAKE_SKIPPED_BY_OVERRIDE` for telemetry.

---

## Appendix A — Verify-Pending Citations

Per RULING_ADR_BACKLOG_20260419.md §5:

- **DR-3 "Lightrun 2026 State of AI-Powered Engineering Report"** (43% manual debug / 0% single-cycle / 88% 2-3 cycles). **Not cited in this ADR.** Coder verifies in `reports/investigation_citation_verification_20260419.md`. If verified, reference can be added to §4 motivation paragraph in a future amendment.
- **DR-3 "DafnyPro framework"** (86% verification pass rate). **Not cited.** Technique not in adoption path regardless.
- **DR-3 "Cleric / Anyshift" topological knowledge graph vendors.** **Not cited.** Topological-graph approach explicitly deferred per §7.
- **DR-3 "FEBF" / "ASI" acronyms.** **Not cited as external frameworks.** Mechanisms adopted; names rejected per ruling §1.

Verified citations used:

- **Knight Capital 2012 incident.** Already cleared in ruling §1 trust tier. Cited in §5 as motivation for non-skippable bake.
- **Google SRE error-budget-policy literature.** Industry-standard, well-documented. Cited indirectly via §3 burn-weight rationale.
- **Pydantic v2.** First-party Anthropic-and-Python-ecosystem standard. No citation risk.

## Appendix B — Backward Compatibility with ADR-007

Every ADR-007 invariant continues to fire under v2. The migration adds columns to `incidents` with defaults that match v1 semantics (`fault_source='internal'`, `severity_tier=1`, `burn_weight=10`). The flat 5-MR weekly cap is **deprecated but not removed in this ADR** — the new burn calculator and the legacy cap run side-by-side for the first 30 days post-migration. If the new burn calculator produces equivalent or stricter behavior over that window, the legacy cap is removed in a follow-on MR. If not, this ADR is amended.

The Author-Critic same-session path (Step 6) continues to work for any Coder-side dispatch that does not opt into the v2 isolated subprocess runner. Migration is per-dispatch, not whole-system. The first dispatch that uses the v2 Critic explicitly opts in via a `--critic-isolated` flag on the remediation runner.

ADR-007 is not superseded. ADR-013 is its successor; both remain authoritative on their respective scopes. Conflicts are resolved in favor of v2.
