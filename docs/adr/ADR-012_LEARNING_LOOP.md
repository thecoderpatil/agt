# ADR-012 — Learning Loop

**Status:** Draft
**Date:** 2026-04-19
**Author:** Architect (Cowork, Opus)
**Inputs:** RULING_ADR_BACKLOG_20260419.md §3.2 (SHIP items 5–8) + DR-2 + Sonnet learning-loop research
**Related:** ADR-010 (CSP Approval Digest), ADR-011 (Live-Execution Promotion — G5 override-variance gate depends on this ADR's schema), ADR-013 (Self-Healing v2), ADR-015 (Tier Migration Roadmap)

---

## 1. Context

AGT's LLM surface today is the CSP approval digest (ADR-010). The digest ranks candidates; Yash approves or skips each one; approved candidates enter the paper or live executor. Every approve / skip / modify decision carries information that, at present, we throw away. We do not record:

- What the LLM recommended, in a form comparable to what happened.
- What the operator did (approve, skip, modify, null-for-autonomous on paper).
- What the realized P&L was on the approved position after natural close.
- What the counterfactual P&L would have been on a skipped candidate (the "regret" we accumulate silently every time Yash's override is wrong).
- The market-state context at the moment of decision, in a form the next-week LLM can retrieve.

Without that feedback loop, the LLM's CSP-ranking prompt is improved only via ad-hoc manual iteration. That does not scale to Level-4 autonomy, and — more urgently — it means the ADR-011 §G5 gate ("operator override variance does not statistically beat the engine") has nothing to evaluate. ADR-012 is the schema + ingest + weekly optimizer pass that makes that gate computable.

The constraint shaping this ADR is **data volume**. Roughly 20 CSP decisions per day × 5 approvals × 52 weeks ≈ 260 preference pairs per year on the approval side, and ~5,200 total decisions per year on the ranking side. This is small-data territory. Methods that assume deep-learning scale (DPO fine-tuning, vector-DB retrieval at 10⁶ rows, reinforcement learning over policy gradients) are misallocated compute at our volume. Methods that work at this scale: prompt optimization with per-example feedback, rank-correlation metrics, and lightweight counterfactual bookkeeping.

This ADR specifies what we build. What we explicitly do not build is documented in §8.

## 2. Memory Layer — SQLite Decisions Schema

A single new table, `decisions`, in the production DB. No separate datastore.

```sql
CREATE TABLE decisions (
    decision_id           TEXT PRIMARY KEY,           -- ULID
    engine                TEXT NOT NULL,              -- 'csp_entry' | 'cc_exit' | 'cc_roll' | 'cc_harvest'
    ticker                TEXT NOT NULL,
    decision_timestamp    TIMESTAMP NOT NULL,         -- UTC, when LLM recommended or engine staged
    raw_input_hash        TEXT NOT NULL,              -- SHA-256 of the input payload (chain + account state + rulebook hash)
    llm_reasoning_text    TEXT,                       -- CSP entry only; NULL for non-LLM engines
    llm_confidence_score  REAL,                       -- CSP entry only; [0.0, 1.0]
    llm_rank              INTEGER,                    -- CSP entry only; position in ranked slate
    operator_action       TEXT NOT NULL,              -- 'approved' | 'rejected' | 'modified' | 'autonomous'
    action_timestamp      TIMESTAMP NOT NULL,
    strike                REAL,
    expiry                DATE,
    contracts             INTEGER,
    premium_collected     REAL,                       -- NULL until filled
    realized_pnl          REAL,                       -- NULL until position natural-closes (CC) or expires (CSP)
    realized_pnl_timestamp TIMESTAMP,
    counterfactual_pnl    REAL,                       -- the opposite-action P&L; computed by shadow settlement
    counterfactual_basis  TEXT,                       -- 'shadow_settled' | 'natural_close' | 'pending' | 'unresolvable'
    market_state_embedding BLOB,                      -- 384-dim float16, VIX + SPY-ret + SPY-rv + per-ticker feats
    operator_credibility_at_decision REAL,            -- α weight active at decision time
    prompt_version        TEXT NOT NULL,              -- hash of the digest prompt used; cross-references prompt_revisions
    notes                 TEXT
);

CREATE INDEX idx_decisions_engine_ts ON decisions(engine, decision_timestamp DESC);
CREATE INDEX idx_decisions_ticker_ts ON decisions(ticker, decision_timestamp DESC);
CREATE INDEX idx_decisions_pending_pnl ON decisions(realized_pnl) WHERE realized_pnl IS NULL;
```

Three ingest points, none on a hot trading path:

1. **`decisions_repo.record_decision()`** — called from the CSP digest composer and from each of the three CC engines at the moment of stage. Writes the row with `realized_pnl = NULL` and `counterfactual_pnl = NULL`. Idempotent on `decision_id`.
2. **`decisions_repo.record_operator_action()`** — called from the Telegram approve/reject/modify handler. Updates `operator_action`, `action_timestamp`. For paper-autonomous, the autonomous executor writes `operator_action = 'autonomous'` at stage-time itself (merged into step 1).
3. **`decisions_repo.settle_realized_pnl()`** — called by a nightly cron (post-17:00 ET, after `flex_sync`) that walks the `pending_realized_pnl` index and closes out rows whose underlying position has natural-closed. Joins against `master_log_trades` for authoritative fills.

Counterfactual P&L ingest is its own engine (§5), not a repo function.

## 3. Prompt Caching Tier

The CSP digest prompt today is ~8-10K tokens: system prompt + rulebook excerpt + 3-5 few-shot examples + current-day candidate payload. Roughly 70% of that is static week-over-week. The dynamic portion is the candidate payload plus any daily risk-regime notes.

Adopt Anthropic prompt caching on the static block. Concrete structure:

```python
messages = [
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": SYSTEM_PROMPT + RULEBOOK_EXCERPT + FEW_SHOT_EXAMPLES,
                "cache_control": {"type": "ephemeral"},   # cached for 5 min after last use
            },
            {
                "type": "text",
                "text": f"Today is {today}. Candidates:\n{payload_json}",
            },
        ],
    },
]
```

The `cache_control` block marks everything before it as cacheable. Anthropic bills cached-read at 10% of base input-token cost; cache-write at 125% of base (amortized across the cached-read wins).

**Expected economics** (verified via DR-2 + Sonnet independent cross-check):

- Uncached: 20 decisions × 10K input tokens × $3/MTok input = ~$0.60/day → ~$18/month.
- Cached: 20 × (7K cached @ $0.30/MTok + 3K uncached @ $3/MTok) = ~$0.02 cache-read + ~$0.18 uncached/day → ~$6/month. One cache-write per 5-min TTL at 125% cost = amortized negligible.

Actual steady-state target **~$12/month** accounting for cache misses on low-traffic hours and any model-version changes. If the observed monthly cost exceeds $25, something is wrong with the cache-block markers (common cause: static text includes a rotating timestamp or model-version string inside the cache boundary) and the wrapper needs inspection.

Implementation ships as **Dispatch E** — a wrapper in `agt_equities/llm/cached_client.py` that sets `cache_control` markers on the static portion of any digest payload, exposing the call through a stable function signature so upstream callers do not change.

## 4. Feedback Signal Design

Composite reward per decision, weighted to align with Level-4 autonomy goals:

```
r_i = 1.0 * realized_pnl_i
    + 0.5 * α_i * regret_delta_i
    + 0.3 * rank_correctness_i
```

Where:

- `realized_pnl_i` is the actual dollar P&L of the position, settled on natural close.
- `regret_delta_i` is the difference between the LLM's recommended action outcome and what the operator actually did. For a skipped candidate where the LLM ranked it #1: `regret_delta = counterfactual_pnl - 0` (we got zero, the counterfactual says what we missed). For an approved candidate where the LLM ranked it lower: `regret_delta = realized_pnl - counterfactual_pnl_of_top_rank` (could be positive if operator found alpha, negative if the operator ignored the LLM's better pick).
- `α_i` is the **operator credibility weight** at decision time, in [0.3, 1.0]. Starts at 0.8. Updated weekly based on the trailing 20-decision window.
- `rank_correctness_i` is Spearman rank correlation coefficient between the LLM's ranked slate and the realized forward return of each ranked candidate, computed at position-close time, normalized to [-1, 1].

**Operator credibility update rule.** Every Saturday after the weekly autonomous pipeline completes:

```
Let O = trailing 20 operator-overridden decisions (approve when LLM ranked low, or skip when LLM ranked high).
Let E = trailing 20 LLM-recommended decisions where operator agreed.

cf_operator = mean(realized_pnl_i for i in O) + mean(counterfactual_pnl_i for i in O where operator skipped)
cf_engine   = mean(realized_pnl_i for i in E) + mean(counterfactual_pnl_i for i in E where engine recommended skip)

if cf_operator > cf_engine + 2σ: α_{t+1} = min(α_t * 1.05, 1.0)        # operator adds alpha, trust them more
elif cf_operator < cf_engine - 2σ: α_{t+1} = max(α_t * 0.9, 0.3)       # operator subtracts alpha, trust engine more
else: α_{t+1} = α_t                                                     # inconclusive, hold
```

The 0.3 floor is deliberate. The operator never loses credibility below 0.3 because the operator retains fiduciary authority — the system does not get to conclude "ignore Yash entirely" from noisy P&L data. The 1.0 ceiling is similar: the operator cannot become *more* credible than the ground truth.

**This credibility rule directly feeds ADR-011 §G5.** G5 asks: does the operator's override statistically beat the engine? The answer is computed from the same two series (`cf_operator` vs `cf_engine`). When α drops below ~0.6 for 60 consecutive days, G5 is green for CSP entry — the engine is demonstrably good enough to promote. When α stays above 0.9, G5 is red and the engine stays paper.

## 5. Counterfactual P&L for Skipped Candidates

Every CSP candidate the operator skipped — or the engine did not recommend — carries an unmeasured P&L counterfactual. Without this measurement, we have no way to tell whether the operator's caution was alpha-positive (avoiding bad trades) or alpha-negative (passing on winners we should have taken).

**Shadow settlement engine.** Nightly cron at 17:30 ET (after `flex_sync`). For each row in `decisions` with `operator_action IN ('rejected', 'autonomous_skip')` and `counterfactual_pnl IS NULL`:

1. Look up the strike + expiry + ticker that was recommended.
2. If expiry has passed: compute counterfactual P&L as premium-collected-at-entry (hypothetical) minus assignment-or-expiry settlement value. Source for the settlement: `master_log_trades` for actual assignments near the strike, or a synthesized price from the CBOE DataShop chain (ADR-014 dependency — until ADR-014 ships, use `yfinance` historical close as a coarser proxy).
3. If expiry is in the future but > 30 days since decision: mark `counterfactual_basis = 'pending'` and try again next night.
4. If expiry is > 60 days from decision and still open: mark `counterfactual_basis = 'unresolvable'` and give up. This is a known gap — long-dated CSPs that we never actually wrote won't have any residual market signal to anchor on.

For rows where the operator *approved* and the engine recommended: counterfactual is "what if we had skipped." That counterfactual is trivially 0 (no position, no P&L) — record and move on.

For rows where the operator *modified* (changed strike or contracts): counterfactual is the engine's original recommendation. Settle both the modified version (via `realized_pnl`) and the original (via `counterfactual_pnl`).

**Known limitations.** This is not a perfect counterfactual. It ignores slippage on the order we never placed. It assumes the premium we'd have collected matches the premium at the exact minute of the LLM decision, which may not survive a delay-to-fill. These imperfections are acceptable at our volume — they produce a noisy but unbiased estimator of regret.

## 6. Weekly Batch Prompt Optimization Pass

Saturday 10:00 ET, after the autonomous pipeline. Runs in a scheduled task named `weekly_prompt_optimization`. Uses the last 7 days of `decisions` rows as training data.

**Approach.** DSPy + TextGrad over the CSP digest prompt. DSPy handles the prompt-as-differentiable-program framing; TextGrad is the gradient-free LLM-judge optimizer that proposes natural-language amendments to the prompt.

**Judge.** Opus (`claude-opus-4-6`) acts as the critic-judge. For each training example:

```
Given: the input payload, the LLM-generated digest, the operator action, the realized + counterfactual P&L.
Question: If the prompt had been amended thus <candidate amendment>, would this decision's reward r_i have been higher? Explain.
Emit: a scalar (expected r_i delta) + a 1-sentence justification.
```

TextGrad aggregates the Opus judgments into a gradient-like direction over prompt amendments. The optimizer emits a candidate new prompt version.

**Promotion gating — prompt amendments are Level-3, not L4.** The optimizer writes the candidate prompt to a `prompt_revisions` table with `status = 'proposed'`. Architect (not Coder, not the optimizer itself) reviews the proposed amendment in the Sunday Architect session before it becomes active. This is deliberate: the prompt is the one place where LLM-proposed self-modification touches the investment decision surface directly. Level-4 autonomy on prompt self-mod is deferred until we have ≥ 6 months of optimizer output history showing no regressions.

**Amendment rate cap.** At most one amendment per week, regardless of how many the optimizer proposes. Prevents compounded-drift scenarios where 4 consecutive amendments each feel defensible in isolation but collectively move the prompt somewhere Architect would not have signed off on.

**Rollback.** The `prompt_revisions` table retains full history. Any weekly evaluation-harness regression (§7) relative to the pre-amendment baseline auto-reverts the prompt to the prior version and writes a Tier-1 incident (ADR-013).

## 7. Evaluation Harness

Four metrics, computed weekly on the trailing 30-day decisions window. All four must be stable-or-improving for the prompt to survive an amendment (§6). Any single metric regression > 2σ triggers rollback.

| Metric | Definition | Source | Target trajectory |
|--------|-----------|--------|-------------------|
| **RankIC** | Mean weekly Spearman rank correlation between LLM candidate rank and realized (or counterfactual) 30-day forward return | `decisions.llm_rank` cross `realized_pnl` + `counterfactual_pnl` | Monotonic improvement; baseline = 0 (random) |
| **Sharpe Attribution** | Decomposition of strategy Sharpe ratio into engine contributions: (engine-recommended + operator-approved P&L) vs (operator-overridden P&L) vs (skipped-counterfactual drag) | Walker-reconstructed strategy P&L | Engine contribution growing; override drag falling |
| **Approval-Agreement Rate** | Fraction of LLM recommendations the operator approved without modification, trailing 20 | `decisions.operator_action` | Trending toward equilibrium at α-weighted level, not 100% |
| **Operator Override Delta** | cf_operator − cf_engine from §4 operator-credibility rule | `decisions` joined on override vs agreement cohorts | Trending toward zero (operator and engine converged) or positive (operator still adds alpha) |

The four metrics together answer the ADR-011 §G5 gate question computably. They also provide the weekly scoreboard for Architect review.

**Anti-overfitting guard.** The optimizer (§6) and the evaluation harness (§7) share data by necessity — we do not have a cleanly separable test set at our volume. To prevent circularity we enforce one rule: the evaluation harness uses a **full 30-day trailing window**, while the optimizer uses a **7-day trailing window**. A prompt amendment must clear the 30-day harness on rolling days 8 through 30 — data the optimizer did not train on. This is not true out-of-sample but it is the best we can manufacture with this much data.

## 8. Non-Goals

Explicit rejections, each with a one-line reason matching the ruling §2 cuts:

- **DPO preference-pair fine-tuning.** ~260 preference pairs/year is insufficient data. Prompt optimization captures the same signal without training infrastructure overhead.
- **pgvector or Postgres migration for memory.** SQLite with embedding blobs handles 7K decisions/year indefinitely; revisit at > 50K.
- **Letta / MemGPT or Mem0 as a dependency.** Architecture is inspirational; actual vendor lock-in at our scale buys nothing.
- **LLM-as-judge on quantitative simulator output.** When ADR-014 ships the Bates MC harness, the harness output is the ground truth. Layering an LLM opinion on top adds hallucination risk without informational gain.
- **"Agent Stability Index" as a separate framework name.** The behavioral drift concept (log LLM confidence distribution, flag > 2σ drift week-over-week) lives in the `decisions` + `incidents` tables already. No separate subsystem.
- **Multi-vendor critic Star Chamber.** Sycophancy concern is real; cost is three-vendor integration + consensus-logic layer. Deferred to first documented sycophancy incident.
- **Fine-tuned or RL-based CSP ranking.** Scale and opacity both wrong for an RIA fiduciary surface.

## 9. First Shippable Sub-Dispatch

**Dispatch A (Codex-tier, ~120 LOC).** Landing sequence:

1. SQL migration `scripts/migrate_decisions_schema.py` creating the `decisions` table + indexes.
2. Repo module `agt_equities/decisions_repo.py` with `record_decision(...)`, `record_operator_action(...)`, `settle_realized_pnl()`.
3. Ingest hook patched into the CSP digest composer path (`agt_equities/csp_digest.py`) and the three CC engines (`_run_cc_logic`, `_scan_and_stage_defensive_rolls`, `scan_csp_harvest_candidates`).
4. Unit tests `tests/test_decisions_repo.py` asserting: idempotent insert, correct state transitions, index hit on pending-P&L lookup.

Ships as a single MR. No prompt changes. No optimizer. No shadow settlement. Just the table + the three repo functions + the four ingest calls. This unblocks ADR-011 §G5 evaluation as soon as two weeks of data accumulate.

**Dispatch E (Codex-tier, ~80 LOC).** `agt_equities/llm/cached_client.py`. Wrapper over the existing Anthropic client call path. Marks the system prompt + rulebook + examples block with `cache_control = ephemeral`. No behavior change; monetary-only optimization. Ships independently of Dispatch A.

**Downstream dispatches (not this turn):**

- Counterfactual shadow settlement engine — Coder-tier (~250 LOC). Gated on Dispatch A + ADR-014 CBOE DataShop data flow.
- Weekly TextGrad/DSPy optimizer — Coder-tier (~400 LOC). Gated on two weeks of Dispatch-A data + Architect review of first prompt amendment proposal.
- Evaluation harness + Architect Sunday scoreboard — Coder-tier (~200 LOC). Gated on Dispatch A data.

## 10. Open Questions

- **α (operator credibility) update cadence.** Weekly is conservative. If volume supports it (confirmed post first 30 days of decisions data), move to bi-weekly or event-based (after every 20 decisions regardless of calendar). Revisit at first ADR-012 amendment.
- **Embedding model for `market_state_embedding`.** Currently specified as 384-dim blob. Placeholder. Decision between a local small embedding model (MiniLM-class, no API call, 384 dim) vs a retrieval-friendly Anthropic embedding (larger, API-dependent, inference cost per decision) deferred to Dispatch A landing — pick whichever does not add a network hop to the hot trading path. Default: local MiniLM.
- **Counterfactual for multi-contract modifications.** If operator modifies contracts from 3 → 1, counterfactual is the 3-contract outcome. Arithmetic scaling is straightforward. Strike modifications are harder — the counterfactual requires a chain walk at decision time. Decide at implementation whether to capture the full chain snapshot at decision time (cheap — payload is already being hashed for `raw_input_hash`) or to reconstruct from EOD Flex. Default: snapshot.
- **Does prompt optimization require Yash approval per amendment, or batch weekly Architect review?** Currently Architect-review-gated. If the first 8 weeks of amendments show zero regressions and < 1% reward-delta noise, consider relaxing to Yash-rubber-stamp. Revisit at the first quarterly sprint review.

---

## Appendix A — Verify-Pending Citations

Per RULING_ADR_BACKLOG_20260419.md §5, the following DR-2 citations are flagged Verify-Pending. **None are load-bearing for any decision in this ADR.** They are listed here so subsequent amendments can upgrade or cut the references after Coder returns verification findings.

- **DR-2 "5 reference architectures" (Hubble / Trading-R1 / Alpha-GPT 2.0 / AMA / FactorMiner)** with arXiv IDs 2604.09601, 2602.14670, 2509.11420, 2402.09746, 2412.20138. Flagged in the ruling as probable hallucination (future-dated arXiv IDs + unverified framework names). **Not cited in this ADR.** If verified, consider adding as §4 prior-art reference. If hallucinated, no action — ADR stands.
- **DR-2 operator-credibility decay formula.** The *mechanism* (trailing-window counterfactual comparison → α update) is sound and adopted. The specific α=0.8 start and 0.9/1.05 update multipliers are first-principles tuning knobs, not cited from DR-2. No citation risk.
- **Letta / MemGPT / Mem0.** Confirmed real (ruling §1 trust tier). Not adopted as dependencies — architecture inspiration only. No citation in ADR body.

Verified citations used in this ADR:

- **Anthropic prompt caching** — pricing and `cache_control` API surface from Anthropic's own published documentation. Cross-confirmed by DR-2 and Sonnet cost math converging at ~$12/month.
- **DSPy / TextGrad as prompt-optimization frameworks** — both have published GitHub repositories and peer-reviewed papers (DSPy: Khattab et al. 2024; TextGrad: Yuksekgonul et al. 2024). Well-established.

## Appendix B — Data-Volume Sanity Check

Assumed steady-state:

- 20 CSP candidates/day × 252 trading days = ~5,040 CSP decisions/year
- ~4-5 CC/roll/harvest decisions/day × 252 = ~1,260/year
- Total: ~6,300 decisions/year = ~17K-20K rows over 3 years

SQLite is trivially comfortable at this scale. 384-dim float16 embedding = 768 bytes/row. At 20K rows, embedding storage = ~15 MB. Indexes on `(engine, decision_timestamp)` and `(ticker, decision_timestamp)` negligible.

Throughput requirement: 30 writes/day peak (decision + operator action + later realized_pnl update). Well under SQLite WAL-mode single-writer ceiling.

If decision volume ever exceeds 50K/year (implying either multi-client expansion or strategy broadening), revisit §8 pgvector deferral. Not before.
