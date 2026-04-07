# Phase 3A.5c2 Discovery Report — R8 + Smart Friction + Full Rule Stack

**Date:** 2026-04-07
**Status:** STOP — awaiting Architect review before implementation
**Pre-flight:** v10 read end-to-end. ADR-004 read end-to-end. All 7 patches internalized. All 5 decisions internalized.

---

## 1. Confirmation of Pre-Flight Reads

- Portfolio_Risk_Rulebook_v10.md: Read in full (713 lines). Changelog v9->v10 confirmed. R8 Gate 1/2/3 rewritten per Operator Attestation. R5 exceptions updated. R9 condition D deferral note at line 28 is now STALE per Patch 7 — this sprint implements it.
- ADR-004: Read in full. All 9 implementation specifications internalized. Schema for bucket3_dynamic_exit_log confirmed with Patch 1 overrides.
- All 7 Architect patches applied as overrides.
- HANDOFF_CODER_latest.md: Read. 26 gotchas current.
- telegram_bot.py: /scan (line 5337), /cc (line 9422), /exit (line 7709), /dynamic_exit (line 7648), 3:30 PM watchdog (line 9978) all inventoried.
- walker.py: No compute_walk_away function. Walk-away P&L lives inline in telegram_bot.py at line 2967 and 7590.

---

## 2. Schema Designs (Q1 + Q2)

### Q1: bucket3_dynamic_exit_log (Patch 1 applied)

```sql
CREATE TABLE IF NOT EXISTS bucket3_dynamic_exit_log (
    audit_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    household TEXT NOT NULL,
    desk_mode TEXT NOT NULL CHECK (desk_mode IN ('PEACETIME', 'AMBER', 'WARTIME')),

    -- Gate math (frozen at render time)
    gate1_freed_margin REAL NOT NULL,
    gate1_realized_loss REAL NOT NULL,
    gate1_conviction_tier TEXT NOT NULL,
    gate1_conviction_modifier REAL NOT NULL,
    gate1_ratio REAL NOT NULL,
    gate2_target_contracts INTEGER NOT NULL,
    gate2_max_per_cycle INTEGER NOT NULL,
    walk_away_pnl_per_share REAL NOT NULL,

    -- Trade details (Patch 1: strike/expiry/contracts NULLABLE for STK_SELL)
    action_type TEXT NOT NULL CHECK (action_type IN ('CC', 'STK_SELL')),
    strike REAL,                          -- NULL for STK_SELL
    expiry TEXT,                          -- NULL for STK_SELL
    contracts INTEGER,                    -- NULL for STK_SELL
    shares INTEGER,                       -- for STK_SELL
    limit_price REAL,                     -- attested price (frozen at render)

    -- Forensic reconstruction (Patch 1)
    campaign_id TEXT,                     -- FK to dynamic_exit_campaigns, NULL for one-shot
    household_nlv REAL NOT NULL,
    underlying_spot_at_render REAL NOT NULL,

    -- Attestation
    operator_thesis TEXT,
    attestation_value_typed TEXT,
    checkbox_state_json TEXT NOT NULL,
    render_ts REAL NOT NULL,
    staged_ts REAL NOT NULL,
    transmitted INTEGER NOT NULL DEFAULT 0,
    transmitted_ts REAL,
    re_validation_count INTEGER NOT NULL DEFAULT 0,

    -- Outcome
    final_status TEXT CHECK (final_status IN (
        'STAGED', 'ATTESTED', 'TRANSMITTED', 'CANCELLED',
        'DRIFT_BLOCKED', 'LOCKED', 'ABANDONED', 'FILLED')),
    fill_ts REAL,
    fill_price REAL,
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_dyn_exit_ticker_date
    ON bucket3_dynamic_exit_log(ticker, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_dyn_exit_household
    ON bucket3_dynamic_exit_log(household, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_dyn_exit_status
    ON bucket3_dynamic_exit_log(final_status);
```

**Constraint validation:**
- CC action: strike, expiry, contracts NOT NULL; shares, limit_price may be NULL. Satisfiable.
- STK_SELL action: strike/expiry/contracts NULL allowed; shares + limit_price filled. Satisfiable.
- campaign_id nullable: one-shot exits have no campaign. OK.
- household_nlv + underlying_spot_at_render NOT NULL: always available at render time. OK.
- No constraint crash identified on synthetic STK_SELL or phased CC.

### Q2: bucket3_dynamic_exit_campaigns (Patch 2)

```sql
CREATE TABLE IF NOT EXISTS bucket3_dynamic_exit_campaigns (
    campaign_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    household TEXT NOT NULL,
    inception_date TEXT NOT NULL,
    locked_target_shares INTEGER NOT NULL,
    locked_conviction_modifier REAL NOT NULL,
    locked_conviction_tier TEXT NOT NULL
        CHECK (locked_conviction_tier IN ('HIGH', 'NEUTRAL', 'LOW')),
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'COMPLETE', 'ABANDONED')),
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) WITHOUT ROWID;
```

**Lifecycle:**
- **Created:** At first Smart Friction STAGE of an R8 exit on a position not currently in an active campaign.
- **COMPLETE:** When cumulative shares exited via staging rows linked to this campaign_id >= locked_target_shares.
- **ABANDONED:** When the position drops below 20% household NLV via natural assignment (R8 trigger removed). Also ABANDONED if operator explicitly cancels via Cure Console.
- **Multi-week phasing:** Subsequent staging rows reference the same campaign_id. Conviction modifier read from campaign row, NOT recomputed. State lock per Gemini Q1.

No lifecycle ambiguity identified.

---

## 3. Gate 1 Implementation Spec (Q3)

```python
def evaluate_gate_1(
    ticker: str,
    household: str,
    strike: float,
    premium: float,        # option mid at render time
    contracts: int,
    adjusted_cost_basis: float,
    conviction_tier: str,  # 'HIGH' | 'NEUTRAL' | 'LOW'
    tax_liability_override: float = 0.0,
) -> dict:
```

**Math:**
```
CONVICTION_MODIFIERS = {'HIGH': 0.20, 'NEUTRAL': 0.30, 'LOW': 0.40}
modifier = CONVICTION_MODIFIERS[conviction_tier]
freed_margin = strike * 100 * contracts
walk_away_loss_per_share = strike + premium - adjusted_cost_basis
net_walk_away_loss = abs(walk_away_loss_per_share) * 100 * contracts + tax_liability_override
ratio = (freed_margin * modifier) / net_walk_away_loss if net_walk_away_loss > 0 else float('inf')
passed = ratio > 1.0
```

**Return shape:**
```python
{
    'passed': bool,
    'ratio': float,
    'freed_margin': float,
    'net_walk_away_loss': float,
    'walk_away_pnl_per_share': float,
    'conviction_tier': str,
    'conviction_modifier': float,
    'tax_liability_override': float,
}
```

**whatIfOrder:** Per Patch 6, fires ONLY at modal render (not ranking). Falls back to static haircut.

---

## 4. Gate 2 Implementation Spec (Q4)

```python
def evaluate_gate_2(
    walk_away_loss: float,
    position_market_value: float,
    available_contracts: int,
    desk_mode: str = 'PEACETIME',
) -> dict:
```

**Math:**
```
severity = walk_away_loss / position_market_value if position_market_value > 0 else 0
if severity <= 0.02:
    max_contracts = available_contracts  # 100% full liquidation
else:
    pct = 0.33 if desk_mode == 'PEACETIME' else 0.25  # Architect lean
    max_contracts = max(1, int(available_contracts * pct))
```

**25-33% resolution:** 33% in PEACETIME, 25% in AMBER/WARTIME. Per Architect lean.

---

## 5. Watchdog Architecture (Q5)

**Location:** Existing `_scheduled_watchdog` at telegram_bot.py:9978. Already runs at 3:30 PM ET Mon-Fri (lines 10432-10437).

**Current R8 detection:** Lines 10133-10166 check cc_cycle_log for 3+ LOW_YIELD cycles. Lines 10168-10255 auto-generate CIO payloads for overweight positions.

**Migration path:** Replace CIO payload auto-generation (lines 10168-10255) with:
1. Write candidate rows to `bucket3_dynamic_exit_log` with `final_status='STAGED'`
2. Emit Telegram pager: "Dynamic Exit candidate ready: {TICKER}. Open Cure Console."
3. Cure Console reads STAGED rows for display

**Static haircut margin model (Patch 6):** For candidate ranking (NOT whatIfOrder):
```
haircut = position_value * 0.35  # conservative 35% maintenance margin
freed_margin_estimate = strike * 100 * contracts - haircut
```
This avoids the whatIfOrder rate-limit trap. whatIfOrder fires once at modal render.

**Crash handling:** Watchdog wrapped in try/except (existing pattern at line 9978). Crashes logged, do not block other scheduled jobs.

---

## 6. Smart Friction Widget (Q6)

**Endpoint:** `POST /cure/dynamic_exit/{audit_id}/attest`
**Modal render:** `GET /cure/dynamic_exit/{audit_id}/attest` returns HTMX fragment

**Template:** New `cure_smart_friction.html` partial, loaded via `hx-get` from the Dynamic Exit panel's [Begin Attestation] button.

**Form fields (PEACETIME):**
- Hidden: `render_ts`, `audit_id`, all frozen gate math values
- Checkbox 1: "I acknowledge this trade locks in a -${loss:,d} unrecoverable loss"
- Checkbox 2: "I confirm the cure target: reduce {ticker} from {current_pct}% to {target_pct}%"
- Textarea: "Strategic rationale for exit" (required, min 10 chars)
- [STAGE] button: DOM-disabled until all checkboxes checked + thesis non-empty

**WARTIME variant (Patch 3+5):**
- Hidden: same frozen values
- Gate 1/2 option math fields HIDDEN for STK_SELL
- Integer Lock input: "Type the exact integer {loss} to authorize"
- [STAGE EMERGENCY EXIT] button: DOM-disabled until input matches
- No thesis textarea, no checkboxes

**HTMX pattern:** `hx-target="#smart-friction-modal"`, `hx-swap="innerHTML"`. First modal in the Deck — new pattern using Tailwind fixed overlay + centered card.

---

## 7. Smart Friction Submission Handler (Q7)

**Validation flow:**
1. `render_ts` freshness: `server_now - render_ts <= 60_000ms`. Fail: HTTP 409.
2. Checkbox states: all TRUE. Fail: HTTP 422.
3. Thesis non-empty (PEACETIME) or Integer Lock matches (WARTIME). Fail: HTTP 422.
4. `attestation_value_typed == str(gate1_realized_loss)` or ticker symbol for $0/$1 loss. Fail: HTTP 422.

**State transition:** `final_status: STAGED -> ATTESTED`

**Telegram push:** Inline keyboard `[TRANSMIT] [CANCEL]` with `callback_data=f"dyn_exit:{audit_id}:transmit"` / `f"dyn_exit:{audit_id}:cancel"`.

---

## 8. JIT Re-Validation Handler (Q8)

**Telegram callback:** `handle_dynamic_exit_callback` matching pattern `r"^dyn_exit:"`

**Flow:**
1. Parse audit_id from callback_data
2. Load bucket3_dynamic_exit_log row
3. `IPriceAndVolatility.get_spot(ticker)` for live spot
4. Re-run `evaluate_gate_1()` with live spot (recompute freed_margin and walk_away_loss)
5. Re-run `evaluate_gate_2()` with live position_market_value (Patch 4)
6. `IOptionsChain.get_chain_slice()` for live mid of the exact strike/expiry
7. Unmarketable drift check: `abs(live_mid - attested_limit_price) > 0.10` -> block (Patch 4)
8. **Pass:** placeOrder with transmit=True + GTC. `final_status='TRANSMITTED'`.
9. **Fail:** Increment re_validation_count. `final_status='DRIFT_BLOCKED'`. Reply with reason.
10. **3-strike (PEACETIME only, Patch 5):** If re_validation_count >= 3, 5-min ticker lock. `final_status='LOCKED'`.
11. **WARTIME:** 3-strike DISABLED. Operator can re-stage indefinitely.

---

## 9. R9 Condition D Activation (Q9)

**Function signature:**
```python
def evaluate_condition_d(
    active_cycles: list,
    spots: dict[str, float],
    household: str,
    chain_provider: IOptionsChain,
) -> bool:
```

**Logic:** For each active cycle in household where `spot < paper_basis` (Mode 1), check if `get_chain_slice(ticker, "C", 14, 30, 0.30)` returns any contract with `annualized_return >= 30%` at a strike >= paper_basis. If NO position can satisfy this, condition_d = True.

**Integration:** Update `evaluate_rule_9_composite`:
- `condition_d = evaluate_condition_d(...)` instead of `condition_d = False`
- Fire threshold: 2-of-4 (was 2-of-3)
- Clear threshold: all-4 (was all-3)
- Update R9_FIRE_THRESHOLD and R9_CLEAR_THRESHOLD constants

**Day 1 projection per household:**

Yash positions (all Mode 1, below basis):
- ADBE: basis ~$329, spot ~$240. Gap 27%. Need 30% annualized CC at strike >= $329. Very unlikely — 37% OTM call at 14-30 DTE has near-zero premium.
- CRM: basis ~$185, spot ~$232. **ABOVE basis = Mode 2.** Has 30%+ CC availability.
- QCOM: basis ~$126, spot ~$155. **ABOVE basis = Mode 2.** Has 30%+ CC availability.
- PYPL: basis ~$45, spot ~$67. **ABOVE basis = Mode 2.** Has 30%+ CC availability.
- MSFT: basis ~$373, spot ~$370. Near basis. Likely has 30%+ CC at/near basis strike.
- UBER: basis ~$72, spot ~$68. Close to basis. May have 30%+ CC.

**Condition D Yash: FALSE.** Multiple positions (CRM, QCOM, PYPL, MSFT) can generate 30%+ at/above basis. NOT all names in Mode 1.

Vikram: Similar profile — CRM, QCOM, PYPL are above basis.

**Condition D Vikram: FALSE.**

**R9 Day 1 projection: OFF (Green).** With all 4 conditions now active:
- A: 0 softened RED R1 positions (glide paths) -> FALSE
- B: 0 softened RED R2 (glide paths) -> FALSE
- C: Vikram EL > 20% -> FALSE
- D: Multiple Mode 2 positions exist -> FALSE
- Result: 0/4 conditions met. R9 = OFF.

**No hard stop.**

---

## 10. R5 Sell Gate Wiring (Q10)

**Current state:** No /sell_shares command exists. Stock sales are manual via TWS direct entry.

**New command:** `/sell_shares TICKER LIMIT_PRICE SHARES HOUSEHOLD EXCEPTION_FLAG`
- Validates exception_flag against SellException enum
- Routes to Smart Friction modal (STK_SELL variant per Patch 3)
- Each exception has different attestation requirements:
  - RULE_8_DYNAMIC_EXIT: full Gate 1/2/3 flow (already the standard R8 path)
  - THESIS_DETERIORATION: thesis required + checkbox attestation
  - RULE_6_FORCED_LIQUIDATION: WARTIME Integer Lock (R6 <10% = emergency)
  - EMERGENCY_RISK_EVENT: WARTIME Integer Lock + logged rationale

**Registration:** Near line 10407 alongside existing command registrations.

---

## 11. R7 Earnings Fail-Closed (Q11)

**Insertion point:** In `/scan` handler after line 5352 (post-Rule 11 check), per-candidate filter.

**Logic:** For each CSP candidate, call `ICorporateIntelligence.get_corporate_calendar(ticker)`. If returns None or `next_earnings` is None: BLOCK with "Earnings data unavailable for {ticker}. Use /override_earnings {ticker} {date|none}."

**New command:** `/override_earnings TICKER DATE|none`

**Override schema:**
```sql
CREATE TABLE IF NOT EXISTS bucket3_earnings_overrides (
    ticker TEXT PRIMARY KEY,
    override_value TEXT NOT NULL,  -- ISO date or 'none'
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL       -- created_at + 24h
);
```

**Read path:** During /scan, check `bucket3_earnings_overrides WHERE ticker=? AND expires_at > datetime('now')` before calling ICorporateIntelligence. If fresh override exists, use override_value as the earnings date (or skip earnings check if 'none').

---

## 12. IBKRProvider Migration Map (Q12)

| Call Site | Current | Target ABC | Method |
|-----------|---------|-----------|--------|
| `data_provider.py:IBKRProvider.get_historical_daily_bars` | Real | IBKRPriceVolatilityProvider | get_historical_daily_bars |
| `data_provider.py:IBKRProvider.get_account_summary` | Real | state_builder.build_account_el_snapshot | Already migrated |
| `data_provider.py:IBKRProvider.get_option_chain` | Stub | IBKROptionsChainProvider | get_chain_slice |
| `data_provider.py:IBKRProvider.get_fundamentals` | Stub | YFinanceCorporateIntelligenceProvider | get_conviction_metrics |
| `data_provider.py:IBKRProvider.get_earnings_date` | Stub | YFinanceCorporateIntelligenceProvider | get_corporate_calendar |
| `state_builder.py:build_correlation_matrix` | Uses old IBKRProvider | IBKRPriceVolatilityProvider | get_historical_daily_bars |
| `state_builder.py:build_account_el_snapshot` | Uses old IBKRProvider | Keep (account state, not market data) |

**Deletion plan:** After all call sites migrated, mark IBKRProvider as deprecated with a 1-sprint grace period. Delete in 3A.5c3 or Phase 3B.

---

## 13. Watchdog Scheduling (Q13)

**Trigger:** Existing `job_queue.run_daily()` at telegram_bot.py:10432, 15:30 ET Mon-Fri.

**Interaction with 9:45 AM /cc:** The /cc scheduled job at line 10424 runs at 9:45 AM. The 15-minute STAGED sweeper (Patch 6, Decision D) will be added as a preamble to _run_cc_logic(): before writing defensive CCs, sweep abandoned STAGED rows older than 15 minutes.

**Crash handling:** Existing try/except in _scheduled_watchdog. Logged, does not block.

**Cure Console refresh:** HTMX polls `/api/cure` every 60s. New candidates appear within 60s of watchdog writing them. No manual refresh needed. Optional: Telegram pager notification for immediate awareness.

---

## 14. Test Scope Projection (Q14)

| Category | Estimated Tests |
|----------|----------------|
| Gate 1 unit tests | 12 |
| Gate 2 unit tests | 6 |
| Smart Friction submission handler | 10 |
| JIT re-validation (3-strike, drift, WARTIME) | 12 |
| Watchdog candidate scanner | 8 |
| bucket3_dynamic_exit_log schema/lifecycle | 8 |
| bucket3_dynamic_exit_campaigns lifecycle | 6 |
| R9 condition D | 6 |
| R5 sell gate wiring | 5 |
| R7 earnings fail-closed | 6 |
| /override_earnings command | 4 |
| IBKRProvider migration verification | 5 |
| Day 1 baseline regression | 3 |
| **Total new** | **~91** |
| **Running total** | **282 + 91 = ~373** |

Revised from prompt's ~88 estimate. The WARTIME variant tests and STK_SELL path add ~3 more.

---

## 15. Day 1 Baseline Projection (Q15)

| Rule | Yash | Vikram | Notes |
|------|------|--------|-------|
| R1 | GREEN (glide) | GREEN (glide) | Unchanged |
| R2 | GREEN (glide) | GREEN (glide) | Unchanged |
| R3 | GREEN | GREEN | Unchanged |
| R4 | GREEN (glide) | GREEN (glide) | Unchanged |
| R5 | GREEN (placeholder) | GREEN | Unchanged |
| R6 | GREEN (N/A) | GREEN (54%) | Unchanged |
| R7 | PENDING | PENDING | Procedural, no evaluator change |
| R8 | NEW: reports candidates | NEW: reports candidates | See below |
| R9 | GREEN (0/4 conditions) | GREEN (0/4 conditions) | Condition D = FALSE |
| R10 | PENDING | PENDING | Config rule |
| R11 | GREEN (glide) | GREEN (glide) | Unchanged |

**R8 detail:** The watchdog would detect ADBE Yash (46.7%), ADBE Vikram (60.5%), and other overweight positions as R8 candidates. It would write STAGED rows to bucket3_dynamic_exit_log with Gate 1 evaluations per option strike. This is **reporting**, not a mode-driving rule. R8 returns its existing PENDING stub in evaluate_all() — the Dynamic Exit system lives outside the mode engine per v10 design.

**R9 condition D:** FALSE for both households. CRM, QCOM, PYPL are above basis with 30%+ CC availability. Not all names in Mode 1.

**Overall mode: PEACETIME.** No regression from 3A.5c1.

**No hard stop.**

---

## 16. Surprises, Gotchas, Hidden Coupling

1. **Walk-away P&L not in walker.py.** Inline in telegram_bot.py at lines 2967 and 7590. ADR-004 references "walker.compute_walk_away_pnl()" as single source of truth, but this function doesn't exist. Need to either create it in walker.py or create a shared utility. Architect decision needed.

2. **Existing /exit command (line 7709) is deprecated per v10 Appendix D.** But it's fully implemented and functional. Migration: /exit stays as-is for backward compat during 3A.5c2, deprecated in Phase 3D.

3. **Existing watchdog (lines 10133-10255) already generates CIO payloads.** The migration to STAGED rows requires replacing the CIO payload generation with bucket3_dynamic_exit_log writes. This is a significant refactor of ~120 lines.

4. **No existing modal pattern in Cure Console.** Smart Friction would be the FIRST modal. Tailwind + vanilla JS (or Alpine.js) dialog pattern needed. No existing framework to reuse.

5. **R7 earnings buffer not implemented in /scan.** The /scan handler has no earnings check at all (confirmed by agent). This is a new insertion, not a modification.

6. **/sell_shares and /override_earnings are entirely new commands.** No existing infrastructure to hook into.

7. **v10 line 28 says condition D is deferred.** Patch 7 overrides this — condition D ships in this sprint. The v10 changelog note must be updated.

---

## 17. Cure Console Template Extensions

Per 3A.5c1 inventory:

| New Template | Purpose | Pattern |
|-------------|---------|---------|
| `cure_dynamic_exit_panel.html` | Partial: STAGED candidates table | Same card pattern as glide_paths |
| `cure_smart_friction.html` | Modal: attestation flow | NEW modal pattern (first in Deck) |
| `cure_smart_friction_wartime.html` | Modal variant: Integer Lock | Same modal, polymorphic content |

**Insertion point:** After glide paths section in cure_partial.html. New `{% include "cure_dynamic_exit_panel.html" %}`.

**HTMX extension:** New endpoint `/api/cure/smart_friction/{audit_id}` returns modal HTML via `hx-get`. Modal overlays the page using Tailwind `fixed inset-0 z-50` pattern.

---

## 18. Architect Review Queue

1. **Walk-away P&L location:** Create in walker.py (purity concern) or shared utility? Currently inline in telegram_bot.py.
2. **Existing /exit deprecation timing:** Keep functional during 3A.5c2 or remove?
3. **Watchdog refactor scope:** Replace CIO payload generation (~120 lines) with STAGED row writes — confirm this is in scope.
4. **Alpine.js dependency:** Add for modal state management, or vanilla JS only?
5. **v10 changelog update:** Update line 28 to "Implemented in Phase 3A.5c2" per Patch 7.
6. **Static haircut margin model:** 35% is conservative. Confirm or adjust.

---

```
Phase 3A.5c2 discovery | schemas drafted: 2/2 | Gate 1 spec: clean
| Gate 2 spec: clean | watchdog: existing _scheduled_watchdog (line 9978)
| Smart Friction: new modal template (first in Deck)
| JIT: Telegram callback handler with 3-strike + drift block
| R9 condition D Day 1: OFF (CRM/QCOM/PYPL above basis)
| R8 Day 1 candidates: ADBE Yash + Vikram (reporting only)
| tests projected: +91 (282->373) | STOP for Architect review
| reports/phase_3a_5c2_discovery_20260407.md
```
