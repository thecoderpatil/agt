# ADR-016 — Paper Pipeline First-Fire Validator

**Status:** Proposed
**Date:** 2026-04-20
**Author:** Architect (Cowork)
**Supersedes:** None — extends ADR-007 (Self-Healing Loop) into functional-correctness territory

## One-line plain English

After every deploy and every morning fire, push a known-good synthetic CSP order through the entire paper pipeline (allocator → stager → executor → paper IB). If it doesn't land, page Yash. Today's bugs would have been caught at deploy-time instead of after a lost trading day.

## Context

In the 48 hours from 2026-04-18 to 2026-04-20 the paper pipeline silently failed three times in three different shapes:

1. **42hr DB split-brain** (2026-04-18 to 2026-04-19) — `db.py` `__file__`-anchored path resolution wrote operational state to `bridge-current\agt_desk.db` after NSSM Phase 2 deploy. Self-healing loop wrote invariants to the orphaned DB too, so the loop was blind to its own outage. Caught by Yash noticing nothing was happening.

2. **Approval-gate misconfiguration** (2026-04-20 09:35 ET) — `AGT_CSP_REQUIRE_APPROVAL=true` in `.env` forced the digest path on paper, blocking `PAPER_AUTO_EXECUTE`. Caught by Yash receiving an unexpected approval digest.

3. **Circuit breaker hardcoded path** (2026-04-20, latent) — `scripts/circuit_breaker.py` had `DB_PATH = "agt_desk.db"` (relative). In the production NSSM cwd this resolved to a non-existent file, blocking all `_auto_execute_staged` calls. Caught only because Coder dug into the code while triaging incident #2.

ADR-007 self-healing detects *deployment-shaped* failures (heartbeat drops, DB path mismatch on boot, schema invariants). It does not detect *configuration-shaped* failures where the service is alive, heartbeats are clean, no exceptions throw, and the pipeline silently produces zero output. From the database's perspective, "wrong env flag silently blocked execution" is indistinguishable from "no candidates today."

Each new MR that adds a code path adds another silent failure surface. The marginal cost of finding the next bug post-deploy continues to grow. We need an active probe, not more passive checks.

## Decision

Implement an active end-to-end pipeline validator that constructs a synthetic CSP candidate, pushes it through the real allocator → stager → executor → paper IB pipeline, verifies the order lands, and cleans up. Run it after every deploy, after every morning autonomous fire, and on demand via Telegram `/validate`. Page on any failure with structured reason indicating which gate blocked.

This is distinct from ADR-007 invariants in three ways: invariants are passive ("is X true right now?"), the validator is active ("does the pipeline actually work right now?"). Invariants run continuously at low cost; the validator runs at fixed checkpoints. Invariants catch state drift; the validator catches functional regression.

## Architecture

### Synthetic candidate

The validator constructs a fixed CSP candidate, bypassing the screener entirely. The screener is non-deterministic (depends on universe state, IV, news, time of day) and not what we're validating. We're validating that *given a candidate, the rest of the pipeline executes correctly*.

```
ticker:   SPY (highest liquidity, always tradeable on paper)
strike:   ~10% OTM put, nearest standard strike
expiry:   next Friday weekly (Heitkoetter-spec)
qty:      1 contract
account:  designated paper validator account
notes:    f"SYNTHETIC_VALIDATOR_{run_id}"
```

Cleanup discipline: distinct `notes` prefix is the canonical marker. Validator orders are cancelled immediately after IB acknowledgement, marked `CANCELLED_VALIDATOR` in `pending_orders`, and reconciled at the next validator run.

### Pipeline stages exercised

The validator must traverse every gate the production pipeline traverses, no shortcuts:

1. Synthetic candidate constructed
2. Approval gate respects current config (validator passes a flag forcing `auto_path=True` regardless of `.env` — this is testing the executor downstream, not the gate logic itself; gate logic is tested separately)
3. Allocator produces a staged order
4. Stager writes `pending_orders` row + `pending_order_children`
5. Executor calls `_auto_execute_staged`
6. Circuit breaker passes (or fails with known reason)
7. Paper IB returns `ib_order_id`
8. DB consistency: `pending_orders.ib_order_id` populated, children consistent

Each stage emits a structured `ValidatorStage` event. Failure at any stage halts the validator and produces a `ValidatorResult` indicating exactly where the wheel stopped turning.

### Output schema

New table `validator_runs`:

```sql
CREATE TABLE validator_runs (
    run_id TEXT PRIMARY KEY,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    trigger TEXT NOT NULL,         -- 'post_deploy' | 'post_morning_fire' | 'on_demand'
    success INTEGER NOT NULL,
    stage_reached TEXT NOT NULL,   -- highest stage successfully completed
    blocked_at TEXT,               -- stage where failure occurred, NULL if success
    blocked_reason TEXT,           -- structured reason code
    pending_order_id INTEGER,      -- FK to pending_orders.id
    ib_order_id INTEGER,           -- paper IB order ID if reached
    cleanup_status TEXT NOT NULL,  -- 'CANCELLED' | 'STUCK' | 'NOT_NEEDED'
    evidence_json TEXT             -- structured payload for triage
);
```

### Triggers

**Hook 1 — post-deploy (`deploy.ps1`):** after NSSM restart and heartbeat verification, call `python -m agt_equities.paper_validator --trigger post_deploy`. Exit code non-zero blocks the deploy script from declaring success. The deploy is not done until a synthetic order has actually traversed the pipeline.

**Hook 2 — post-morning-fire (PTB JobQueue):** scheduled 5 minutes after `csp_scan_daily` completes (~09:40 ET). Validator runs in parallel with whatever the morning fire produced. Independent: the morning fire might have legitimately produced zero candidates, but the validator's synthetic still goes through.

**Hook 3 — on-demand (Telegram `/validate`):** manual trigger for incident triage or post-fix confirmation. Same code path as Hook 1.

### Failure paging

On `success=0`:
- Telegram message to Yash with: stage reached, stage blocked, blocked_reason, run_id
- Row written to `incidents` table at severity HIGH
- Cleanup attempted; if cleanup fails, severity escalates to CRITICAL and validator self-disables until next deploy

### Failure modes the validator catches that today's stack does not

- Approval gate misconfigured (today's bug #2)
- Circuit breaker DB path or other gate logic broken (today's bug #3)
- `_auto_execute_staged` path broken
- Allocator returning empty staged list given valid input
- Stager writing wrong account_id or missing children
- IB gateway not accepting orders despite reachable connection
- `ib_order_id` not propagating back into DB
- Reconnect-after-restart not hydrating in-flight orders correctly

### Failure modes still not caught

- Screener correctness (wrong candidates surfaced) — separate concern, addressed by golden-snapshot tests
- Allocator math errors — addressed by hh_snapshot fixture suite
- Live-only bugs — live gateway is RO at IB, end-to-end execution validation impossible by design

## Why not alternatives

**Extend invariants.yaml with "csp_decisions row produced today":** doesn't catch "pipeline staged but didn't execute" (today's bug). Also produces false positives on legitimately-empty universe days.

**Extend ADR-007 critic to read `pending_orders` age:** same shape problem. Stale `pending_orders` rows happen for benign reasons (after-hours staging, weekend carry-over).

**Mock or dry-run mode:** doesn't exercise IB gateway, doesn't catch network/auth/contract-format failures. The whole point is end-to-end including IB.

**Scheduled `/scan_csp` with watch:** depends on screener producing real candidates (non-deterministic) and adds noise to actual trading.

## Implementation phases

**P1 (load-bearing, ships next):** standalone validator script `agt_equities/paper_validator.py` + `validator_runs` table migration + manual `python -m agt_equities.paper_validator` invocation. Must run synthetic order through to IB and clean up correctly.

**P2:** `deploy.ps1` integration. Block deploy on validator failure.

**P3:** PTB JobQueue integration for post-morning-fire hook.

**P4:** Telegram `/validate` command + page-on-failure alerting.

**P5 (deferred):** Multi-account validation when paper grows beyond single sub-account.

## Acceptance criteria

P1 is complete when:
- Synthetic SPY put order traverses screener-bypass → allocator → stager → executor → paper IB
- `pending_orders` row created with `notes LIKE 'SYNTHETIC_VALIDATOR_%'`
- `pending_order_children` row created and consistent
- `ib_order_id` populated within 60s of submission
- Order cancelled within 30s of IB acknowledgement
- `validator_runs` row written with success=1, stage_reached='ib_acknowledged', cleanup_status='CANCELLED'
- Manual invocation from Coder shell with `$env:AGT_DB_PATH` set produces exit 0
- Re-running the validator with `AGT_CSP_REQUIRE_APPROVAL=true` (the today-bug scenario) reproduces the failure and pages with `blocked_at='approval_gate'` and `blocked_reason='AGT_CSP_REQUIRE_APPROVAL_TRUE'`

## Priority

**Ship P1 ahead of remaining MODE-SCRAP chain (-1, -2a-flex, -2b, -3, -4e).**

MODE-SCRAP is hygiene work — retiring already-dead concepts. The validator is the thing standing between AGT and the next silent paper-pipeline failure. Today cost a full trading day. The next bug — if we don't have the validator — costs another. Compounding cost dominates the hygiene benefit.

## Risk register

- **Synthetic orders pollute production data:** mitigated by `notes` prefix marker + `CANCELLED_VALIDATOR` status filter in all reporting queries.
- **Validator runs concurrent with real scan:** mutex on validator job; documented as a single-instance daemon function.
- **Cleanup fails, stuck pending row:** validator second-run reconciles before new submission; on persistent failure, validator self-disables until human intervention.
- **IB rejects synthetic for benign reason** (e.g., contract not found, weekend): validator distinguishes "pipeline broken" from "IB rejected with known code" via explicit error-code allowlist. Benign rejections do NOT page; only pipeline failures do.
- **Validator becomes load-bearing for deploy script — what if validator itself is broken?** Document `--skip-validator` deploy override for emergency cases. Use sparingly; default is fail-closed.

## Dependencies

- Designated paper sub-account for validator orders (avoid mixing with live-spec test accounts)
- Paper IB gateway 4002 reachable (validator depends on IB, not just DB)
- `pending_orders.notes` column exists (verify in P1 schema audit)

## Open questions

- Should the validator probe each engine (CSP, CC, harvest, roll) separately, or just CSP-as-canary? **Architect leaning:** CSP-as-canary for P1, expand to per-engine in P5 once positions exist to test against.
- Should validator failures during 09:35 ET window suppress autonomous trading until next manual all-clear? **Architect leaning:** no — false positives in the validator should not halt real engines. Pages-but-doesn't-block is the right shape.
- Where does the validator live: `agt_equities/paper_validator.py` (importable module) or `scripts/paper_validator.py` (CLI tool)? **Architect leaning:** module form, with thin `__main__` wrapper, so PTB JobQueue can import it directly without subprocess overhead.

## Related ADRs

- ADR-007 (Self-Healing Loop) — passive runtime invariants; this is the active complement
- ADR-008 (Shadow Scan) — read-only validation of screener output; this validates execution path
- ADR-010 (CSP Approval Digest) — the approval gate this validator must respect (and exercise both branches of)
