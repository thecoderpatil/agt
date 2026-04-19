# ADR-014 — Synthetic Data and AI-Driven Evaluation Harness

**Status:** Draft
**Date:** 2026-04-18
**Author:** Architect (Cowork, Opus)
**Inputs:** RULING_ADR_BACKLOG_20260419.md §3 (ship items), DR-4 (`reports/DR_OUTPUT_synthetic_data_evaluation_20260418.md`), existing scenario bank (`project_backtest_results_2026_04_15.md` — 172 CC / 596 CSP / 555 harvest on BS chains)
**Related:** ADR-011 (Live-Execution Promotion — MC eval feeds canary ramp), ADR-012 (Learning Loop — counterfactual P&L uses MC-priced settlement), ADR-013 (Self-Healing v2 — eval produces per-prompt Pydantic-validated benchmark rows), ADR-010 (Composer — `cached_client` contract is reused by eval-time LLM calls), ADR-015 (Tier Migration — MC pass rate is a tier-promotion gate)

---

## 1. Context

AGT trades ~1–5K decisions per year. That's 10–100× too few for direct supervised ML or frequentist-significant strategy calibration. Every promotion gate, every prompt-amendment validation, every new engine has to validate against a synthetic ground-truth bank — because the production trace alone doesn't have statistical power.

Current state (2026-04-18):

- `reports/synthetic_chains_bs_20260415.db` has 1,323 BS-synthesized option chains across 172 CC / 596 CSP / 555 harvest scenarios.
- `pxo_scanner.py` + `csp_harvest.py` + `roll_engine.py` all have offline backtest entry points that consume this bank.
- Phase 1 backtest (2026-04-15) cleared all engines: 0 CC violations, 0 harvest bugs, roll sim wins 76% at $2.07 avg debit.
- **All of it is Black-Scholes.** BS under-prices OTM puts — which is exactly the tail AGT sells. The "clean pass" signal is contaminated; we're validating CSP entries against a pricing model that systematically overstates their edge.

Three concrete problems drive this ADR:

**Problem 1 — BS contamination.** BS says our 30-delta 45-DTE puts have P(assignment) ≈ 10% and expected premium decay matches theta. In reality, earnings gaps and crash-tail events cluster at precisely the strikes we sell. ADR-014's primary deliverable is replacing BS with Bates (Heston + Merton jumps) so MC trajectories capture the left-tail risk the wheel strategy is structurally short.

**Problem 2 — No eval harness for LLM additions.** The composer (ADR-010 Phase 2) ships with test coverage for schema drift, budget, fallback — but zero coverage for "does the LLM's ranking actually produce better forward P&L than alphabetical order?" Without a 10K-scenario MC harness, every prompt amendment in ADR-012 is a blind change.

**Problem 3 — Promotion gates need quantitative backing.** ADR-011 ramps live execution via canary stages (5 → 15 → 50 → 100%). Each stage needs an explicit "eval pass" criterion, not operator judgment alone. ADR-015 tier migration is the same: Level-4 autonomy requires a scoring rubric we can point to.

This ADR builds the infrastructure that makes all three quantitative.

---

## 2. Scope

**In scope (Phase 1 — foundation):**
- Bates characteristic-function pricer (`agt_equities/synth/bates_fft.py`) — FFT-based, PyPy or numpy-only
- Bates + rough-vol calibration worker — calibrates per-ticker against CBOE DataShop snapshots
- MC harness (`agt_equities/synth/mc_wheel.py`) — 10K-trajectory wheel simulator with antithetic + Sobol variance reduction
- Fidelity metrics module (`agt_equities/synth/metrics.py`) — KS test, drawdown tail, Spearman rank on LLM ranker
- Scenario-bank regeneration worker (`scripts/regen_scenario_bank.py`) — weekly cron, produces `reports/synthetic_chains_bates_YYYYMMDD.db`
- CI integration: promotion-gate job `mc_eval_10k` runs on any PR touching engines (`pxo_scanner`, `csp_harvest`, `roll_engine`, `csp_allocator`, `csp_digest_composer`)
- Pydantic v2 schema for eval-output rows (per ADR-013 Layer 1)

**In scope (Phase 2 — regime + stress):**
- HMM regime identification on SPY/VIX (≥2000 history)
- VIX-bucket conditioning (oversample VIX>40 for 2008-style paths)
- Student-t copula for multi-ticker crash synthesis
- Kill-switch exercise — every eval run must trip at least one kill switch; if none trip across 10K paths, the scenario bank is too benign

**In scope (Phase 3 — LLM-in-loop):**
- Spearman rank correlation between composer's LLM ranking and MC-computed forward return
- Prompt-sensitivity eval: same scenario × perturbed prompts → rank-stability check
- Composer-vs-static baseline: does LLM ranking actually beat alphabetical?

**Out of scope (deferred to later ADRs or cut):**
- Agent-based market simulation (ABIDES) — AGT wheel size (1–10 contracts) below granularity where top-of-book assumption breaks
- Deep Pricing NN / Deep Hedging — academic tooling; Bates FFT is sufficient for wheel-strategy scales
- FinRL Safe RL shielding — Level-4 concern, defer to ADR-015
- Per-ticker individual calibration at scale (>50 tickers) — MVP calibrates the top-20 wheel tickers only
- OPRA real-time tick feed — $10K/month prohibitive; CBOE DataShop EOD is sufficient

**Out of scope (cut per ruling):**
- "FEBF" + "DafnyPro" + "Cleric"/"Anyshift"/"Hubble"/"Trading-R1"/"Alpha-GPT 2.0"/"AMA"/"FactorMiner" — DR-marked hallucinations per ruling §1; techniques adopted under generic names where evidence-backed, named frameworks dropped.

---

## 3. Synthetic Chain Generation

### 3.1 Primary model — Bates (Heston + Merton jumps)

Bates combines Heston stochastic volatility with Merton Poisson jumps. It is the first non-BS model with enough degrees of freedom to match both the short-term ATM skew (stochastic vol's contribution) and the far-OTM put premium (jump component's contribution).

For the wheel strategy — where we sell OTM puts and are structurally short left-tail — Bates is the minimum-viable pricing model. Rough vol is marginally better at ultra-short-term (<7 DTE) skew but adds calibration complexity; defer to Phase 2 as an optional enhancement.

**Dynamics (reference for implementation):**
```
dS = (r - d - λ·k̄)·S·dt + √v·S·dW₁ + (J-1)·S·dN
dv = κ·(θ - v)·dt + σ_v·√v·dW₂
corr(dW₁, dW₂) = ρ
N ~ Poisson(λ), log(J) ~ Normal(μ_J, σ_J²), k̄ = E[J-1]
```

7 parameters per ticker: `{v0, κ, θ, σ_v, ρ, λ, μ_J, σ_J}`.

### 3.2 Pricer — FFT via characteristic function

`agt_equities/synth/bates_fft.py` implements the Carr-Madan FFT approach:

```python
def bates_fft_call_price(
    S0: float,
    strikes: np.ndarray,      # array of K values, sorted
    T: float,                 # time to expiry (years)
    r: float,                 # risk-free rate
    q: float,                 # dividend yield
    params: BatesParams,      # (v0, kappa, theta, sigma_v, rho, lambda, mu_J, sigma_J)
    alpha: float = 1.5,       # damping coefficient
    N_fft: int = 4096,        # FFT grid size
    eta: float = 0.25,        # FFT step in log-moneyness
) -> np.ndarray:              # call prices aligned with strikes
    """Bates model call price via FFT. Puts via put-call parity."""
```

Put prices from put-call parity: `P = C - S*exp(-q*T) + K*exp(-r*T)`.

FFT grid and damping (`alpha=1.5`, `eta=0.25`, `N=4096`) are standard defaults from Carr-Madan (1999); produce <0.5% pricing error vs. Monte Carlo reference for wheel-relevant strikes (0.1 ≤ K/S ≤ 1.2) and expiries (7–60 DTE). Sentinel test asserts this tolerance on the first 3 canonical strikes per ticker.

Complexity: O(N log N) per chain generation. A full 25-strike × 6-expiry chain computes in ~2ms on a single CPU core. Sufficient for 10K MC runs in <30s without GPU.

### 3.3 Calibration

`agt_equities/synth/bates_calibrate.py` minimizes SSRE weighted by vega:

```python
def calibrate_bates(
    snapshot: MarketChainSnapshot,   # real bid/ask from CBOE DataShop
    initial_guess: BatesParams | None = None,
    max_iter: int = 500,
) -> BatesCalibrationResult:
    """Differential Evolution + Levenberg-Marquardt hybrid.

    DE for global search (Bates non-convex landscape) followed by
    LM polish. Returns params + fit residuals + per-strike error.
    """
```

Hybrid DE+LM because Bates' parameter surface is non-convex with multiple local minima; pure LM gets trapped depending on initial guess. DE is robust but slow; chain them. 500 iterations × 7 params × 150 strikes per ticker ≈ 90s on CPU. Acceptable for weekly batch; not suitable for real-time.

**Cadence:** weekly Saturday job (`scripts/regen_scenario_bank.py`, 4 AM UTC Saturday). Rationale: wheel calibration doesn't need intraday freshness; calibration noise week-to-week is already below signal.

**Top-20 ticker MVP:** calibrate against the 20 tickers that have produced ≥80% of CSP + CC volume in the last 90 days. Query at calibration-start:

```sql
SELECT ticker, COUNT(*) AS n
FROM master_log_trades
WHERE entry_ts_utc >= strftime('%Y-%m-%d', 'now', '-90 days')
GROUP BY ticker
ORDER BY n DESC
LIMIT 20;
```

Out-of-top-20 tickers fall back to a generic Bates profile (median parameters across top-20 with slightly elevated jump intensity). Logged warning in `llm_calls`-adjacent eval log.

### 3.4 Rough-volatility secondary (Phase 2 deferred)

Rough vol (Bayer-Friz-Gatheral 2016) with Hurst H<0.5 is measurably better than Heston/Bates at reproducing short-term ATM skew (7–14 DTE). Rebate: implementation complexity and calibration instability outside the academic setting. Defer until Phase 1 is operational and we have concrete signal that Bates' short-end skew is the limiting factor.

Re-scoping trigger: if MC-harness KS test on 7-DTE puts fails for >30% of top-20 tickers on Phase 1 Bates, escalate Phase 2 rough-vol.

---

## 4. Monte Carlo Wheel Harness

### 4.1 Architecture

`agt_equities/synth/mc_wheel.py`:

```python
def run_wheel_mc(
    params: dict[str, BatesParams],   # per-ticker calibrated params
    n_trajectories: int = 10_000,
    horizon_days: int = 365,
    initial_capital: float = 500_000,  # synthetic account size
    seed: int = 42,                    # deterministic
    variance_reduction: bool = True,   # antithetic + Sobol
    allocator_callable: Callable[[WheelState], list[CSPCandidate]] = default_csp,
    approval_gate_callable: Callable[[list[CSPCandidate]], list[CSPCandidate]] = identity,
    slippage_bps: int = 200,           # 2% of bid-ask penalty (realistic for small RIA fills)
) -> WheelMCResult:
    """Run the wheel strategy against synthetic trajectories.

    Returns per-trajectory equity curves, per-trade P&L, max drawdown,
    and aggregate fidelity metrics vs. historical ground truth.
    """
```

### 4.2 Variance reduction

Both applied by default:

1. **Antithetic variates** — for every path $Z_i$ a paired path $-Z_i$ is added. Halves standard error of expectation estimates at no extra sampling cost. Standard Monte Carlo technique.
2. **Sobol sequences** — quasi-random low-discrepancy sequences replace pseudo-random draws. Convergence rate $O(1/N)$ vs $O(1/\sqrt{N})$ — for $N=10^4$ that's ~100× tighter error bars for the same compute. Numpy's `scipy.stats.qmc.Sobol` is sufficient; no GPU needed.

Combined: 10K Sobol+antithetic runs produce error bars comparable to ~1M naive MC.

### 4.3 Slippage model (hard-coded realism, not optional)

`slippage_bps=200` (2% of bid-ask spread) is the default. This is deliberately punitive — real fills for AGT's wheel-liquidity tickers routinely give up 50–150 bps on the bid-ask; 200 bps adds margin.

Anti-pattern avoided: frictionless mid-price execution is the single most common failure mode in sim-to-real translation. Coding a realistic penalty at MC time eats the backtest Sharpe inflation upfront.

### 4.4 Regime-conditioned generation (Phase 2)

Two flavors:

1. **HMM regime identification** — fit a 3-state HMM on SPY realized vol + VIX from 2000-01-01 onward. States map loosely to (low-vol bull, high-vol mean-revert, systemic contagion). Condition MC generation on the transition matrix to produce regime-realistic sequences.
2. **VIX-bucket conditioning** — upsample VIX>40 days (representing ~0.5% of history since 2000) to ~10% of synthetic days. The wheel strategy's left-tail exposure needs over-representation of crash-adjacent days, not representative sampling.

**Tail dependence — Student-t copula:** for multi-ticker crash synthesis (all CSP positions assign simultaneously), use a Student-t copula with DoF=3 during contagion regimes. Kill-switch exercise invariant: every 10K run must trip at least one kill switch (margin exceeded, daily loss cap, mode escalation). If none trip, the scenario bank is too benign and fails the eval.

### 4.5 Kill-switch exercise invariant

`NO_KILL_SWITCHES_EXERCISED_IN_MC` — Phase 2 sprint_a test. Asserts that a 10K MC run over top-20 tickers trips at least one of:
- `DAILY_LOSS_CAP_REACHED`
- `MARGIN_CALL_SIMULATED`
- `MODE_ESCALATION_AMBER_OR_WARTIME`
- `NLV_DRAWDOWN_GT_10_PCT`

Over 10K trajectories with VIX-bucket conditioning, the expected number of kill-switch trips is >50. Zero trips = bank composition bug (e.g., synthetic chains have no left-tail jumps, copula DoF too high, VIX bucket weights wrong). Fails the eval run; blocks promotion of any strategy change until scenario bank is re-validated.

---

## 5. Fidelity Metrics

`agt_equities/synth/metrics.py`:

### 5.1 P&L distribution alignment — KS test

Kolmogorov-Smirnov two-sample test on the empirical CDF of per-trade P&L:
- `real` = last 365 days of `master_log_trades` closed cycles
- `synthetic` = 10K MC trajectory per-trade P&L

Null hypothesis: real and synthetic come from the same distribution. Reject at p < 0.05 = fidelity failure.

Threshold: **p-value ≥ 0.10** required for promotion-gate pass. Rationale: we actively want the synthetic distribution to include more tail than observed (VIX-bucket conditioning). A KS p-value between 0.10 and 0.40 is healthy — indicates fat-tailed synthetic without being identical to observed.

Edge case: p-value > 0.95 is suspicious (synthetic too close to train data → leakage). Investigate.

### 5.2 Max drawdown tail

Compare the 95th-percentile and 99th-percentile drawdown from MC vs. historical. Synthetic must dominate historical on both (realistic stress injection). Failure mode: historical 99p DD = 18%; synthetic 99p DD < 15% → scenario bank understates crash risk.

### 5.3 Realized vol term structure + option skew

For each ticker in top-20, compute realized vol term structure (7/14/30/60 DTE) from MC paths; compare against OptionMetrics-equivalent historical term structure. Divergence >25% at any tenor = calibration failure for that ticker; fallback to generic profile for that ticker.

### 5.4 Spearman rank correlation — LLM ranker validation

This is the composer validation surface. Per MC scenario:

1. Run composer (`compose_digest`) against the synthetic candidate list for that scenario.
2. Extract the composer's `ranking` (integer order of candidate preference).
3. Compute MC-forward-return per candidate over the scenario's horizon (synthetic ground truth).
4. Compute Spearman rank correlation between composer ranking and MC forward returns.

Aggregate across scenarios:
- **mean Spearman ≥ 0.20** required for composer enablement (ADR-010 MR-E.6 live flip prerequisite)
- **mean Spearman ≥ 0.30** required for Level-4 tier migration (ADR-015 prerequisite)

Baseline comparison: alphabetical-by-ticker static ranking produces Spearman ≈ 0 by construction. Random ranking → 0 in expectation with high variance. Composer must clear a meaningful threshold above zero.

### 5.5 Prompt-sensitivity analysis

Run the composer against the same scenario with 5 prompt variants (instructed-conservatism, instructed-aggression, explicit-risk-flags-required, no-system-prompt, system-prompt-shuffled-bullet-order). Compute pairwise Spearman across the 5 rankings.

Threshold: **mean pairwise Spearman ≥ 0.70** across variants. Below = ranking is prompt-fragile = not production-safe. Blocks composer enablement.

### 5.6 Fill-quality reproduction

Sanity metric. Synthetic bid-ask-midpoint fills with 200bps slippage penalty should produce an average per-contract slippage cost distribution aligned with the observed slippage on filled `master_log_trades` rows. Within 30% of observed mean, or calibrate slippage bps.

---

## 6. CI integration — promotion-gate job

New `.gitlab-ci.yml` job `mc_eval_10k`:

```yaml
mc_eval_10k:
  stage: test
  image: python:3.11
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
      changes:
        - agt_equities/pxo_scanner.py
        - agt_equities/csp_harvest.py
        - agt_equities/roll_engine.py
        - agt_equities/csp_allocator.py
        - agt_equities/csp_digest_composer.py
        - agt_equities/csp_approval_gate.py
        - agt_equities/synth/**
  script:
    - pip install -e .
    - python scripts/run_mc_eval.py --n-trajectories 10000 --output reports/mc_eval_${CI_MERGE_REQUEST_IID}.json
    - python scripts/assert_mc_eval_gates.py reports/mc_eval_${CI_MERGE_REQUEST_IID}.json
  artifacts:
    paths:
      - reports/mc_eval_*.json
    expire_in: 30 days
  timeout: 30 minutes
```

Gates enforced by `scripts/assert_mc_eval_gates.py`:

| Metric | Threshold | On fail |
|---|---|---|
| KS p-value (P&L) | ≥ 0.10 | FAIL — block merge |
| 99p drawdown ratio (synth/real) | ≥ 1.0 | FAIL — scenario bank benign |
| Kill-switch exercise count | ≥ 10 | FAIL — regime not stressed |
| Spearman on composer ranking (if composer-touching) | ≥ 0.20 | FAIL — composer regression |
| Prompt-sensitivity Spearman (if composer-touching) | ≥ 0.70 | FAIL — prompt-fragile |

All tests in a single script; all failures are hard FAIL (no allow_failure). Blast radius is a single merge block, operationally cheap to re-run.

**Escape hatch:** `mc_eval_10k` can be marked `allow_failure: true` by hand for MRs that demonstrably don't affect engine behavior (e.g., docs, ADR updates, test-only changes). That's a manual override per-MR in the job description, not a rule-level allowlist.

---

## 7. Scenario bank regeneration

`scripts/regen_scenario_bank.py`:

Weekly Saturday 4 AM UTC cron. Writes `reports/synthetic_chains_bates_YYYYMMDD.db` with:

```sql
CREATE TABLE synthetic_chains (
    ticker TEXT,
    observation_date TEXT,           -- date this calibration reflects
    expiry_date TEXT,
    strike REAL,
    option_type TEXT,                -- 'C' or 'P'
    mid_price REAL,
    bid REAL,                        -- derived from mid + synthetic spread model
    ask REAL,
    implied_vol REAL,
    delta REAL,
    gamma REAL,
    theta REAL,
    vega REAL,
    bates_params_json TEXT           -- frozen calibration for audit
);
CREATE INDEX idx_synth_ticker_expiry ON synthetic_chains(ticker, expiry_date);

CREATE TABLE synthetic_regime_paths (
    path_id INTEGER,
    step INTEGER,                    -- day index
    spot REAL,
    iv REAL,
    regime TEXT                      -- 'low_vol_bull' | 'high_vol_mr' | 'contagion'
);
CREATE INDEX idx_synth_paths_path_id ON synthetic_regime_paths(path_id);
```

Retention: keep 8 weekly snapshots (~2 months). Older snapshots archived to `C:\AGT_Runtime\backups\scenario_banks\`.

Promotes from `bridge-staging` to `bridge-current` via standard deploy.ps1 pattern (no direct DB write to production). Invariant tick asserts `observation_date` freshness ≤ 14 days; stale snapshot = tier-2 incident.

---

## 8. Pydantic v2 schemas (per ADR-013 Layer 1)

`agt_equities/synth/schemas.py`:

```python
class BatesParams(BaseModel):
    v0: confloat(gt=0, lt=4)           # initial variance (vol²)
    kappa: confloat(gt=0, lt=10)       # mean reversion speed
    theta: confloat(gt=0, lt=4)        # long-run variance
    sigma_v: confloat(gt=0, lt=2)      # vol of vol
    rho: confloat(ge=-0.99, le=0.99)   # correlation (dW1, dW2)
    lambda_jump: confloat(ge=0, le=5)  # jump intensity
    mu_J: confloat(ge=-0.5, le=0.5)    # log-jump mean
    sigma_J: confloat(ge=0.001, le=1)  # log-jump stddev
    model_config = {"extra": "forbid", "frozen": True}

class MCEvalResult(BaseModel):
    run_id: str
    commit_sha: str
    n_trajectories: int
    ks_pvalue: float
    drawdown_99p_ratio: float
    kill_switches_exercised: int
    spearman_composer: float | None
    spearman_prompt_sensitivity: float | None
    per_ticker_fidelity: dict[str, dict[str, float]]
    duration_seconds: float
    model_config = {"extra": "forbid"}
```

Parse failures at eval-result ingest = tier-2 incident. Forces alignment between eval script output and CI gate script.

---

## 9. Invariants

### 9.1 `NO_STALE_SCENARIO_BANK` (Layer 2)

Medium severity. `synthetic_chains_bates_*.db` most recent `observation_date` > 14 days old = incident. Suggests weekly regen job failed.

### 9.2 `NO_KILL_SWITCHES_EXERCISED_IN_MC` (Layer 2, per-run)

High severity. 10K MC run with zero kill-switch trips = bank composition bug. Asserted at eval-run close.

### 9.3 `NO_BATES_CALIBRATION_OUT_OF_BOUNDS` (Layer 1)

Tier-1 incident from Pydantic validation failure on `BatesParams`. Caught at calibration-result ingest; bad calibration is never promoted to scenario bank.

### 9.4 `NO_MC_EVAL_WITHOUT_COMMIT_SHA` (Layer 2)

Low severity. Eval result rows must have non-null `commit_sha`. Ensures audit traceability of every gate pass.

### 9.5 `NO_COMPOSER_ENABLED_WITHOUT_SPEARMAN_0.20` (Layer 3 — lint)

`scripts/precommit_loc_gate.py` extension. Any commit setting `AGT_CSP_COMPOSER_ENABLED=true` in `.env` or config requires a pointer to an MR artifact with Spearman ≥ 0.20 in the last 7 days. Enforced at pre-commit, not runtime — we can't block runtime env toggles, but we can block ships that quietly flip the flag.

---

## 10. Migration plan

| MR | Title | LOC | Dependencies | Sprint |
|---|---|---|---|---|
| MR-D.0 | Bates FFT pricer + unit tests (pricing accuracy vs. MC reference) | ~300 | None | Sprint 1 (Coder-tier, self-contained — this is Dispatch D) |
| MR-D.1 | Bates calibration worker (DE+LM hybrid) + tests | ~350 | MR-D.0 | Sprint 1 |
| MR-D.2 | MC harness core (Sobol + antithetic, no regime yet) + tests | ~400 | MR-D.0 | Sprint 1 |
| MR-D.3 | Fidelity metrics module (KS, drawdown, Spearman) + tests | ~300 | MR-D.2 | Sprint 2 |
| MR-D.4 | CI promotion-gate job `mc_eval_10k` + `scripts/assert_mc_eval_gates.py` | ~200 | MR-D.3 | Sprint 2 |
| MR-D.5 | Scenario-bank regeneration cron + retention + invariants | ~250 | MR-D.1, MR-D.2 | Sprint 2 |
| MR-D.6 | HMM regime identification + VIX-bucket conditioning | ~300 | MR-D.2 | Sprint 3 |
| MR-D.7 | Student-t copula + kill-switch exercise invariant | ~200 | MR-D.6 | Sprint 3 |
| MR-D.8 | Composer Spearman integration (composer-vs-static baseline in eval) | ~150 | MR-D.3, ADR-010 MR-E.1 | Sprint 4 |
| MR-D.9 | Prompt-sensitivity harness + CI gate | ~150 | MR-D.8 | Sprint 4 |
| (MR-D.10+) | Rough-volatility secondary (deferred Phase 2 trigger) | TBD | MR-D.5 + signal | Deferred |

Estimated 4 sprints (~8 weeks) for Phase 1 + Phase 2. Phase 3 (LLM-in-loop) rides on composer ship.

MR-D.0 is the Dispatch D target per the 2026-04-19 AM-2 queue. Self-contained ~300 LOC. Ships first, alone — no dependency on the rest of the stack.

---

## 11. Data sourcing

### 11.1 Calibration snapshots — CBOE DataShop

**Purchase plan:** targeted per-ticker EOD option chain history for top-20 wheel tickers, 3-year lookback. Pay-per-dataset model.

Pricing as of 2026-04: **Coder verification dispatch owed** — exact current DataShop pricing verified via vendor contact. Budget note: plan for ~$50–$150/month ongoing for weekly refresh of top-20 tickers.

Snapshot into `reports/datashop_chains/<ticker>_<YYYYMMDD>.csv`. Calibration worker reads from this directory.

### 11.2 Forward validation — Alpaca paper

Already integrated. Paper gateway on 4002 writes to `master_log_trades_paper`. Compare paper P&L distribution against MC P&L distribution monthly; drift = investigation trigger.

### 11.3 Historical underlying — existing yfinance integration

`yfinance` for historical spot prices + realized vol. Already used in the existing scenario bank; no change.

### 11.4 Macro data — VIX

CBOE VIX daily close via yfinance. Sufficient for HMM regime identification. OPRA real-time VIX tick not needed at AGT scales.

---

## 12. Anti-patterns guarded against

Per DR-4 + hard-learned:

1. **No test-on-train.** Calibration window = N-3 years through N-1 year. Eval window = last 1 year. Never overlap. Sentinel test: `scripts/assert_no_calibration_eval_overlap.py` inspects `synthetic_chains_bates_*.db` observation dates vs. eval window in `mc_eval_10k`.
2. **LLM-as-judge circularity avoided.** MC harness is quantitative ground truth. Composer is only judged by Spearman vs. MC return, never by another LLM. Explicit anti-pattern; called out in §5.4.
3. **Survivorship bias.** Calibration universe includes delisted tickers from Phase 2 onward (requires CBOE DataShop coverage of delistings; cost permitting). Phase 1 accepts the bias as a known limitation — documented, not hidden.
4. **Kill-switch independence.** Kill switches are enforced by deterministic rule evaluation inside the MC harness, not by LLM reasoning. Per Knight Capital anti-pattern: LLM overrides must NEVER disable kill switches. Hard-coded.
5. **Variance-reduction transparency.** Sobol + antithetic are default-on. Any override requires an explicit `variance_reduction=False` flag at call time; pattern-match blocks that in production code via lint (`test_no_variance_reduction_disabled_in_prod`).
6. **Sim-to-real slippage.** 200bps penalty is hard-coded default. Override requires explicit argument. Lint: `test_slippage_at_least_150bps_in_eval_calls`.

---

## 13. Open questions (owed to next ADR iteration)

- **CBOE DataShop 2026 pricing.** Coder verification dispatch queued. Budget may force Phase 1 to top-10 tickers instead of top-20.
- **Rough-volatility escalation trigger.** §3.4 sets "30% of top-20 fail KS on 7-DTE puts" as re-scope trigger. Is 30% the right bar? Revisit after MR-D.3 produces first real-world KS distribution.
- **HMM state count.** 3 states (Phase 2 default) vs. 4 or 5. Academic literature is noisy. Start at 3, increase if regime-transition fidelity metrics fail.
- **Spearman threshold calibration.** 0.20 for composer enablement, 0.30 for Level-4. These are starting values; refine after MR-D.8 produces the first real composer Spearman distribution.
- **Eval cadence.** Weekly scenario regen vs. daily. Weekly matches wheel strategy cadence; daily adds compute + noise. Default weekly; revisit only if we add higher-frequency strategies.

---

## 14. Success criteria (Phase 1 exit)

1. Bates FFT pricer produces <0.5% RMSE vs. MC reference across 100 wheel-relevant (S, K, T) points. Sentinel test passes.
2. Calibration worker converges for all 20 top tickers within 120s each. Median fit residual (weighted SSRE) ≤ 0.02.
3. 10K MC run completes in ≤ 30 minutes on CI runner.
4. MC run against current engine state produces KS p-value ≥ 0.10 vs. 1-year production P&L.
5. MC run exercises ≥ 10 kill-switches across 10K trajectories.
6. Weekly scenario regen cron runs 4 consecutive weeks without failure.
7. CI `mc_eval_10k` job blocks at least one real engine-affecting PR (demonstrates the gate is load-bearing, not vestigial).

Passing all 7 → Phase 1 exit. Phase 2 eligibility.

---

## 15. Notes

- ADR-014 is the quantitative spine that makes ADR-011 canary ramps and ADR-015 tier promotions defensible. Without the MC harness, both become operator-judgment under-the-guise-of-process.
- Dispatch D (Bates FFT pricer) = MR-D.0. Re-authorized against this ADR's §3.2 spec. Coder-tier, self-contained.
- DR-4 hallucinations (FEBF, DafnyPro, named reference architectures) are not cited in this ADR. Techniques adopted under generic names where evidence-backed (Bates, rough vol, HMM, Student-t copula, KS, Spearman). Named-framework citations dropped per evidence-tier discipline.
- CBOE DataShop cost verification dispatch owed to Coder before MR-D.1 ships. Budget shifts scope (top-20 → top-10 possible).

**End of ADR-014.**
