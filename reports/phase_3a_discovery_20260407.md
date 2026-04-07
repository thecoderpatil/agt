# Phase 3A Discovery Report — Cure Console Foundation

**Date:** 2026-04-07
**Author:** Coder (Claude Code)
**Status:** DISCOVERY — awaiting Yash review before implementation

---

## A. Current Rule Math Inventory

### Rules with centralized evaluator functions (`agt_deck/risk.py`)

| Rule | Function | Lines | Inputs | Output | Pure | Notes |
|------|----------|-------|--------|--------|------|-------|
| **2** | `vix_required_el_pct(vix)` | 39-45 | float vix | float (retain %) | YES | VIX bracket table v9. No EL comparison — just returns the *required* retain %. |
| **1** | `concentration_check(cycles, hh_nlv, spots)` | 48-73 | cycles, household NLV dict, spot prices | (ticker, pct, household) worst | YES | Returns single worst position. Does not evaluate the 20% threshold — caller decides. |
| **3** | `sector_violations(cycles, industry_map)` | 126-141 | cycles, ticker-to-industry dict | list of (industry, tickers) | YES | Returns all industries with >2 active tickers. |
| **11** | `gross_beta_leverage(cycles, spots, betas, hh_nlv)` | 84-123 | cycles, spots, betas, household NLV | dict{hh: (leverage, status)} | **NO** — mutates `_leverage_breached` for hysteresis | Thresholds: 1.50 breach, 1.40 release, 1.30 amber. |
| **8** | `dynamic_exit_threshold(redeploy_yield, wait_months, ...)` | 148-191 | floats | float threshold | YES | W3.8 helper. Not a compliance evaluator — it's a decision tool. |

### Rules evaluated inline in `telegram_bot.py`

| Rule | Location | Lines | Notes |
|------|----------|-------|-------|
| **11** (blocking gate) | `_check_rule_11_leverage(household)` | 1109-1173 | Calls `gross_beta_leverage()`, blocks `/scan` if BREACHED. Fail-open on exceptions. |
| **1** (informational) | `/health` handler | 8737-8748 | Iterates positions, flags >20% NLV. Display only. |
| **3** (informational) | `/health` handler | 8750-8795 | Per-household + all-book sector count. Display only. |
| **8** (trigger detection) | `/health` + watchdog | 6897, 9967-10000 | Checks 3+ consecutive low-yield cycles. Fires alert. |
| **1** (drawdown exception) | Dynamic Exit payload | 7327-7330 | Exempts 30%+ drawdown positions from Rule 1 concentration limit. |

### Rules with NO current evaluator code

| Rule | Description | Status |
|------|-------------|--------|
| **4** | Pairwise 6-month correlation ≤0.6 at entry | **NOT IMPLEMENTED.** No correlation check anywhere in codebase. |
| **5** | Capital velocity > nominal breakeven | **NOT IMPLEMENTED.** Referenced in `/dynamic_exit` payload logic but no standalone evaluator. |
| **6** | Vikram IND EL ≥20% NLV | **NOT IMPLEMENTED.** EL not persisted in DB (see section D finding). Mentioned in Rulebook but no code evaluates it. |
| **7** | Mode 1/Mode 2 CC/CSP procedure | **PROCEDURAL, not evaluable.** These are operational rules about strike selection and rolling — not portfolio-level compliance metrics. No automated evaluator makes sense. |
| **9** | Red Alert (2+ simultaneous breaches) | **NOT IMPLEMENTED.** Would be a meta-rule over Rules 1-8/11. No code evaluates it. |
| **10** | Exclusions (SPX box spreads, legacy picks, negligible) | **CONFIG, not evaluable.** EXCLUDED_TICKERS already handles this in Walker. |

### Summary: Of 11 rules, only 4 have evaluator functions (R1, R2, R3, R11). R8 has a helper but not a compliance evaluator. R4/R5/R6/R9 are not implemented. R7/R10 are procedural/config.

---

## B. CIO Payload Call Sites in `telegram_bot.py`

23 meaningful CIO references across 3 pipelines:

| Pipeline | Lines | Description | Phase 3D action |
|----------|-------|-------------|-----------------|
| `/scan` CSP entry | 5324 | Generates "CIO Payload: CSP Entry Scan" header | Remove entire CIO payload generation |
| `/cc` covered call | 9003-9425 | Generates CIO payload with exit commands; sends as Message 2 | Remove CIO payload; keep CC logic |
| `/cc` scheduled watchdog | 9773-9788 | Same as manual /cc but auto-fired | Same |
| `/dynamic_exit` | 7394-7603 | Generates V7 Dynamic Exit CIO payload | Merge into Cure Console |
| `/exit` | 7665-7782 | Executes CIO-authorized dynamic exit | Keep but re-gate via mode engine |
| CIO conviction override | 7194-7338 | Override system for conviction tiers | Remove (CIO silo deleted) |

**Total call sites to rip out in Phase 3D: ~6 payload generators + 1 override system.**

---

## C. Proposed `agt_equities/rule_engine.py` Module

```python
@dataclass
class PortfolioState:
    """Snapshot of portfolio state for rule evaluation."""
    household_nlv: dict[str, float]           # {household: NLV}
    household_el: dict[str, float | None]     # {household: EL or None if unavailable}
    active_cycles: list[Cycle]
    spots: dict[str, float]                    # {ticker: spot price}
    betas: dict[str, float]                    # {ticker: beta}
    industries: dict[str, str]                 # {ticker: industry_group}
    vix: float | None
    open_positions: list[dict]                 # raw position rows for OPT notional

@dataclass
class RuleEvaluation:
    rule_id: str                               # "rule_1", "rule_2", etc.
    household: str | None                      # None for portfolio-wide
    ticker: str | None                         # None for portfolio-wide
    raw_value: float | None                    # current measured value
    baseline_value: float | None               # grandfathered baseline
    target_value: float | None                 # target from glide path
    expected_value_today: float | None         # interpolated expected
    status: str                                # GREEN / AMBER / RED / PAUSED
    cure_math: dict                            # {action: str, qty: int, impact: float}
    message: str                               # human-readable summary
```

**Proposed evaluators for Phase 3A (rules with computable state):**

| Function | Rule | Inputs | Output |
|----------|------|--------|--------|
| `evaluate_rule_1(ps, household)` | Concentration | NLV, spots, cycles | Per-ticker concentration % |
| `evaluate_rule_2(ps, household)` | EL deployment | VIX, NLV, EL | EL retain % vs required |
| `evaluate_rule_3(ps, household)` | Sector | cycles, industries | Per-industry ticker count |
| `evaluate_rule_11(ps, household)` | Leverage | cycles, spots, betas, NLV | Gross beta-weighted leverage |

**Deferred evaluators:**
- Rule 4 (correlation): requires 6-month price history, yfinance dependency. Not in Phase 3A.
- Rule 5 (capital velocity): requires per-cycle annualized return calc. Not currently computed.
- Rule 6 (Vikram EL): EL not persisted. See HARD STOP below.
- Rules 7, 9, 10: procedural/meta/config — not evaluable as compliance metrics.

---

## D. Live Household State (2026-04-06 Flex data)

### NAV

| Account | Household | NAV | Cash |
|---------|-----------|-----|------|
| U21971297 | Yash | $109,217.87 | -$31,286.40 |
| U22076329 | Yash | $152,661.32 | $1,689.70 |
| U22076184 | Yash | $23.17 | $23.17 |
| U22388499 | Vikram | $80,787.00 | -$94,472.43 |
| **Yash total** | | **$261,902.36** | |
| **Vikram total** | | **$80,787.00** | |

### Leverage (Rule 11) — beta=1.0

| Household | Leverage | Status | Handoff said |
|-----------|----------|--------|-------------|
| Yash | 1.60x | BREACHED | 2.18x (with yfinance betas) |
| Vikram | 2.17x | BREACHED | 2.88x (with yfinance betas) |

**Note:** Handoff leverage values used real yfinance betas. With default beta=1.0, values are lower. The rule_engine must use yfinance betas (REFERENCE only, per R4 constraint) for accurate leverage computation. This is the same pattern `build_top_strip()` already uses.

### Concentration (Rule 1)

| Household | Ticker | Market Value | % NLV | Rule 1 status |
|-----------|--------|-------------|-------|---------------|
| Yash | ADBE | $122,178 | 46.7% | BREACHED (>20%) |
| Yash | PYPL | $104,604 | 39.9% | BREACHED (>20%) |
| Yash | MSFT | $74,576 | 28.5% | BREACHED (>20%) |
| Yash | UBER | $50,519 | 19.3% | OK |
| Yash | QCOM | $37,719 | 14.4% | OK |
| Vikram | ADBE | $48,871 | 60.5% | BREACHED (>20%) |
| Vikram | MSFT | $37,288 | 46.2% | BREACHED (>20%) |
| Vikram | PYPL | $36,384 | 45.0% | BREACHED (>20%) |
| Vikram | UBER | $21,651 | 26.8% | BREACHED (>20%) |
| Vikram | CRM | $18,503 | 22.9% | BREACHED (>20%) |
| Vikram | QCOM | $12,573 | 15.6% | OK |

### Sector (Rule 3)

**Violation:** Software - Application has 3 tickers: ADBE, CRM, UBER (limit 2).

### EL (Rule 2/6)

**HARD STOP FLAG:** Excess Liquidity is NOT persisted in any table. The Flex report does not include EL in `master_log_nav` or any other `master_log_*` table. Rule 2 and Rule 6 evaluators need EL as input but it's unavailable.

**Options:**
1. Add EL capture to a future Flex query configuration (would need IBKR Flex query update)
2. Compute EL as `cash + margin_available` from existing columns (if available)
3. Defer Rule 2/6 evaluation to a later phase
4. Use `cash` column as a rough proxy (underestimates actual EL)

**Recommendation:** Defer Rule 2/6 to when EL data source is resolved. Include in PortfolioState as `Optional[float]` so the evaluator can return PAUSED when EL unavailable.

---

## E. Proposed Baseline Seed Data

| household | rule_id | ticker | baseline | target | start | target_date | pause |
|-----------|---------|--------|----------|--------|-------|-------------|-------|
| Yash_Household | rule_11_leverage | NULL | 1.60 | 1.50 | 2026-04-07 | 2026-06-30 | none |
| Vikram_Household | rule_11_leverage | NULL | 2.17 | 1.50 | 2026-04-07 | 2026-07-28 | none |
| Yash_Household | rule_1_concentration | ADBE | 46.7 | 25.0 | 2026-04-07 | 2026-08-25 | earnings +/-5d |
| Yash_Household | rule_1_concentration | PYPL | 39.9 | 25.0 | 2026-04-07 | 2026-08-25 | earnings +/-5d |
| Yash_Household | rule_1_concentration | MSFT | 28.5 | 25.0 | 2026-04-07 | 2026-08-25 | none |
| Vikram_Household | rule_1_concentration | ADBE | 60.5 | 25.0 | 2026-04-07 | 2026-08-25 | earnings +/-5d |
| Vikram_Household | rule_1_concentration | MSFT | 46.2 | 25.0 | 2026-04-07 | 2026-08-25 | none |
| Vikram_Household | rule_1_concentration | PYPL | 45.0 | 25.0 | 2026-04-07 | 2026-08-25 | earnings +/-5d |
| Vikram_Household | rule_1_concentration | UBER | 26.8 | 25.0 | 2026-04-07 | 2026-08-25 | none |
| Vikram_Household | rule_1_concentration | CRM | 22.9 | 20.0 | 2026-04-07 | 2026-08-25 | none |
| Yash_Household | rule_3_sector | SW-App | 3 | 2 | 2026-04-07 | 2026-08-25 | none |

**Note on leverage baselines:** Values above use beta=1.0. With real yfinance betas (handoff says Yash=2.18x, Vikram=2.88x), baselines would be higher. **Decision needed:** use beta=1.0 baselines or yfinance baselines? I recommend yfinance for accuracy, but then the rule_engine must call yfinance on every evaluation (same pattern as `build_top_strip()`).

**Earnings dates:** Not available from current data sources. yfinance `Ticker.calendar` can provide earnings dates, but that's a REFERENCE use per R4 constraint. Recommend fetching at seed time and hardcoding as `pause_conditions` JSON.

**Day 1 GREEN guarantee:** Because baselines == today's values, `expected_value_today == baseline` on day 0. `delta = actual - expected = 0`. Status = GREEN for all.

---

## F. Glide Path Math

```
expected_value_today = baseline + (target - baseline) * min(days_elapsed / total_days, 1.0)
delta = actual - expected_value_today
weekly_rate = (target - baseline) / total_days * 7

GREEN:  delta <= 0 (on or ahead of schedule)
AMBER:  delta > 0 AND delta < abs(weekly_rate * 2)
RED:    delta >= abs(weekly_rate * 2) OR actual has worsened past baseline
PAUSED: pause_conditions active → always GREEN, labeled "PAUSED: <reason>"
```

**For reduction targets** (leverage, concentration): baseline > target, so `target - baseline < 0`. We want actual to decrease. The "actual - expected" delta is positive when behind (actual higher than expected).

---

## G. `glide_paths` Table Schema

```sql
CREATE TABLE IF NOT EXISTS glide_paths (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  household_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  ticker TEXT,
  baseline_value REAL NOT NULL,
  target_value REAL NOT NULL,
  start_date TEXT NOT NULL,
  target_date TEXT NOT NULL,
  pause_conditions TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  UNIQUE(household_id, rule_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_glide_household ON glide_paths(household_id);
```

## `mode_history` Table Schema

```sql
CREATE TABLE IF NOT EXISTS mode_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  old_mode TEXT NOT NULL,
  new_mode TEXT NOT NULL,
  trigger_rule TEXT,
  trigger_household TEXT,
  trigger_value REAL,
  notes TEXT
);
```

Both are Bucket 3 (operational state). Idempotent DDL.

---

## H. 3-Mode State Engine Spec

| Mode | Trigger | Effect |
|------|---------|--------|
| PEACETIME | All rules GREEN (against glide path) | Normal operations |
| AMBER | Any rule AMBER | New CSP entries blocked across all scans; exits/rolls allowed |
| WARTIME | Any rule RED | Cure Console only decision surface; LLM calls disabled; mandatory audit memo before revert |

- Computed as `max(rule.status for rule in all_evaluations)` against glide path.
- Transitions logged to `mode_history`.
- Evaluator runs on: `/reconcile`, Deck page load, 5-min background cron.
- Manual `/declare_wartime` and `/declare_peacetime` with audit log.
- **Day 1: PEACETIME** (all baselines == current values → all GREEN).

---

## I. Cure Console Spec

- **Route:** `GET /cure` on FastAPI
- **Template:** `agt_deck/templates/cure_console.html`, extends `base.html`
- **Mobile-first:** Tailwind responsive, stacks vertically on narrow screens
- **Content per household:**
  - Rule evaluations table: current | baseline | expected | target | status pill
  - Progress bars (% of glide path complete)
  - Cure math for next step (e.g., "Sell 50 ADBE shares to hit this week's target")
  - Earnings pause indicators
- **Top strip:** Mode Badge (PEACE/AMBER/WAR) between Warn and Sync badges
- **Lev cells:** become `<a href="/cure">` links
- **HTMX:** `hx-get="/api/cure" hx-trigger="every 60s"` for auto-refresh

---

## J. `desk_state.md` Auto-Generator Spec

- **Location:** `C:\AGT_Telegram_Bridge\desk_state.md`
- **Writer:** `agt_deck/desk_state_writer.py`
- **Trigger:** Every 5 min via APScheduler (or similar) + synchronously at end of `flex_sync.py` run
- **Atomic write:** Write to `desk_state.md.tmp`, then `os.replace()` to prevent partial reads
- **Contents:** Markdown with timestamp, mode, per-household metrics, all rule evaluations, walker warnings, mode transitions, glide paths

---

## K. Tailscale / Mobile Access

- FastAPI currently binds to `127.0.0.1:8787` (line 411 of `main.py`)
- **Config change needed:** Bind to `0.0.0.0:8787` or add Tailscale interface IP
- Token auth (`AGT_DECK_TOKEN`) already protects the endpoint
- **No DNS, no reverse proxy, no public exposure** — Tailscale mesh only
- Setup: Install Tailscale on desk machine + phone, share the Tailscale IP

---

## L. Existing `/exit_math` Status

`/exit_math` was planned in W3.8 but deferred — never implemented (see `reports/w3_8_exit_math_20260407.md` line 21). The `dynamic_exit_threshold()` helper function exists in `risk.py` but has no Telegram command wired to it.

**Recommendation:** Merge exit math display into Cure Console rather than building a separate Telegram command. The Cure Console can show "dynamic exit threshold = X% → current drawdown = Y% → EXIT/HOLD" per frozen position.

---

## M. Known Risks, Hard Stops, and Open Questions

### HARD STOPS

1. **EL not persisted.** Excess Liquidity is not in any `master_log_*` table. Rules 2 and 6 cannot be evaluated without EL data. **Options:** defer these rules, add Flex query field, or compute proxy from `cash` column.

### Risks

2. **Leverage baseline disagreement.** Handoff says Yash=2.18x, Vikram=2.88x. Live computation with beta=1.0 gives Yash=1.60x, Vikram=2.17x. The difference is the yfinance betas. **Decision needed:** which values to use as baselines?

3. **`gross_beta_leverage()` is impure.** It mutates module-level `_leverage_breached` dict for hysteresis. The rule_engine should call a pure version or snapshot the state. Recommend creating a pure wrapper that returns leverage without mutating state.

4. **`/cc` has no Rule 11 gate.** Currently only `/scan` blocks on leverage breach. The mode engine needs to also block `/cc` on AMBER/WARTIME if the spec requires it (the prompt says "new CSP entries blocked across all scans; exits/rolls still allowed" — CCs are rolls/exits, so `/cc` should remain allowed).

5. **Sector violation: UBER classified as "Software - Application."** UBER (ride-hailing) being grouped with ADBE and CRM seems like a Yahoo/GICS classification issue, not a real sector concentration. This will show as a glide path violation on Day 1. **Decision needed:** override UBER's industry classification, or accept the violation?

### Open Questions

6. `/declare_peacetime` from WARTIME requires "mandatory post-wartime audit memo before reverting." How is this enforced? Require a text argument? Just log the transition and trust the operator?

7. The 5-min desk_state.md regeneration — what fires it? Options: (a) APScheduler inside the Deck FastAPI process, (b) a separate cron script, (c) a Telegram bot background job. Recommend (a) since the Deck process already runs continuously.

8. VIX data source for Rule 2 — currently fetched from yfinance in `get_vix()` helper (REFERENCE use). This is acceptable per R4 but needs try/except. Confirm VIX is acceptable as a yfinance REFERENCE call.

---

## N. Test Plan Overview

| Area | Tests | Notes |
|------|-------|-------|
| `rule_engine.py` evaluators (R1, R3, R11) | ~8 | Synthetic PortfolioState inputs, known outputs |
| Glide path math | ~6 | exact-halfway, ahead, behind-1wk, behind-3wk, paused, worsening |
| Mode engine state machine | ~6 | All 6 transitions: P→A, P→W, A→P, A→W, W→P, W→A |
| `desk_state.md` atomicity | ~2 | Write + verify content, concurrent read safety |
| Cure Console render | ~5 | Empty, peacetime, amber, wartime, paused rule |
| Telegram commands | ~3 | `/declare_wartime`, `/declare_peacetime`, `/mode` |
| **Total** | **~30** | Full suite must stay green (114 existing + ~30 new = ~144) |

---

**STOP. Awaiting Yash review before implementation.**

### Decisions needed:

1. **EL data source:** Defer Rules 2/6, add Flex field, or proxy from cash?
2. **Leverage baselines:** Use yfinance betas (2.18x/2.88x) or beta=1.0 (1.60x/2.17x)?
3. **UBER sector classification:** Override or accept as violation?
4. **WARTIME → PEACETIME audit memo:** How to enforce?
5. **desk_state.md regeneration:** APScheduler inside Deck process OK?
6. **CCs on AMBER/WARTIME:** Blocked or allowed? (Prompt says "exits/rolls still allowed" — CCs are rolls.)
