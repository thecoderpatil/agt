# AGT Equities — Rulebook Reference (LLM Context)
# Condensed from Portfolio_Risk_Rulebook_v11.md
# This is NOT the governing document — the full v11 Rulebook is authoritative.

## Governing Principle
Maximum income for minimum risk under Act 60 Chapter 2 (PR). Premium and gains tax-exempt; losses have no recovery value. Aggressive on premium collection, conservative on incremental risk.

## Rule Precedence
1. Avoid forced broker liquidation (Rule 6) — overrides Rule 5.
2. Portfolio deployment governor (Rule 2 EL).
3. Concentration / sector / correlation (Rules 1, 3, 4).
4. Operating procedures (Rule 7, then Rule 8).

Operator Attestation is an execution gateway, not a state-machine rule. Failed attestation blocks staging; status quo preserved.

## Key Definitions
- **Household NLV:** Combined NLV across Individual + Vikram IND + Roth IRA. Excludes SPX box notional.
- **Excess Liquidity (EL):** IBKR Current Excess Liquidity = Equity with Loan Value − Maintenance Margin. "Current" metric is governing.
- **Adjusted Cost Basis:** Cost Basis − (Accumulated Premium ÷ Shares Owned).
- **Tested:** Underlying within 2% of short strike, or through it.
- **Operator Attestation:** Mandatory synchronous digital affirmation via the Command Deck. Final governance gate for below-basis sells. Mathematically binds operator to exact financial impact and logs strategic rationale to Bucket 3 for Act 60 structural intent. Implementation per ADR-004.

## Accounts
| Account | Margin | CSP | Notes |
|---|---|---|---|
| Individual (U21971297) | Yes | Yes | Primary margin account |
| Vikram IND (U22388499) | Yes | Yes | Rule 6: 20% EL floor |
| Roth IRA (U22076329) | No | No | Shares + CCs only |
| Trad IRA (U22076184) | No | No | **Dormant** |

R2 EL measured across margin-eligible accounts only (Individual + Vikram IND). Roth excluded from BOTH numerator and denominator (ADR-001).

## Rule 1: Concentration
Max 20% of household NLV per name at entry. Drawdown exception: extends to 30% if stock fell ≥30% from basis. Breach (decline or appreciation): Freeze (no new shares, no CSPs). Continue CCs. No forced selling.

## Rule 2: EL by VIX (5-tier)
| VIX | Min EL Retain | Max Deploy |
|---|---|---|
| <20 | 80% | 20% |
| 20-25 | 70% | 30% |
| 25-30 | 60% | 40% |
| 30-40 | 50% | 50% |
| 40+ | 40% | 60% |

Max deployment capped at 60% regardless of VIX. Last 40% EL is the survival bunker. Denominator: margin-eligible NLV only (ADR-001).

## Rule 3: Sector
Max 2 names per Yahoo Finance industry bucket (`Ticker.info["industry"]`). Refresh via `/sync_universe`. Legacy violations cured by natural assignment or Rule 8, never forced selling.

## Rule 4: Correlation
Max 0.6 rolling 6-month pairwise at entry. Recalc monthly for existing pairs. Breach: freeze both, write CCs on smaller, evaluate Rule 8 if eligible.

## Rule 5: Capital Velocity > Nominal Breakeven
Never sell shares below basis EXCEPT (all four require **Operator Attestation**):
1. **Rule 8 Dynamic Exit** — passes Gates 1 and 2 + attestation.
2. **Thesis Deterioration** — bearish rationale logged.
3. **Forced Liquidation Avoidance** — Rule 6 <10% EL. Requires Operator Attestation. Overrides R5 per precedence.
4. **Emergency Risk Event** — fraud, delisting, bankruptcy. Risk catalyst logged.

Never write CCs intending forced assignment at a loss without Rule 8 + attestation. Rallies to/above basis: assignment welcomed.

## Rule 6: Vikram IND Margin Backstop
Maintain ≥20% Current EL. Breach: freeze entries, write CCs on most concentrated, apply premium to debt. <10% EL: evaluate sale of smallest position (overrides R5, requires Operator Attestation).

**Severity tiers (R6 evaluator):**
| Ratio | Status |
|---|---|
| ≥0.25 | GREEN |
| 0.20–0.25 | AMBER |
| 0.10–0.20 | RED (freeze entries) |
| <0.10 | RED + `severity=CRITICAL` (R5 override authorized) |

CRITICAL exposed in `RuleResult.detail['severity']`.

## Rule 7: Covered Call / CSP

### Mode 1 (BELOW basis) — Defensive
- Strike: start at basis, move down until ≥5% annualized. Min $0.03 premium. Min 3% OTM.
- DTE: 14-21d (near basis), 21-30d (far below).
- Rolling: tested 5+ DTE hold; 3-4 DTE prepare; <3 DTE roll up/out for ANY credit. Never allow assignment below basis except R8 + attestation.
- LOW-YIELD flag: <5% annualized for 3+ cycles → Dynamic Exit eval.
- Extended-DTE supplement: 6+ months Mode 1 AND >25% below basis → additional 45-60 DTE CC, strike ≥10% above price AND ≥15% below basis, min $0.30.
- Earnings: strike within 10% of price + earnings in window → wait. Strike >15% above → OK.

### Mode 2 (AT/ABOVE basis) — Harvest
- 30%/130% framework: Annualized = (Premium/Strike) × (365/DTE).
- **Delta ≤ 0.30.** If only 30%-floor strikes have Delta >0.30, do not sell.
- 50% profit: close and re-enter.
- Rolling: tested 3+ DTE hold; ITM <3 DTE roll for ≥$0.20 credit; cannot roll → let assign (tax-exempt).
- Dividend: ITM + ex-div approaching + extrinsic < dividend → roll/close. Dividend rolls do NOT require attestation (cash-flow adjustment, not basis destruction).
- Assignment: welcomed. Re-enter via CSP.

### CSP (new names)
- 30%/130% framework. **Delta ≤ 0.25.**
- **No CSP within 7 calendar days of earnings.**
- 50% profit GTC BTC simultaneous with STO.
- Pre-entry: R2 EL OK, R1 ≤20%, R3 sector OK, R4 correlation OK.
- Roth IRA: no CSPs.

### Order Execution
Limit at mid minus $0.03-0.05. Enter after 9:45 AM ET. Move to mid if unfilled by 10:30.

### Assignment Protocol
Confirm status each morning. Never sell shares while short calls are open.

### Corporate Actions
Suspend all new CC/CSP on affected name until OCC confirms adjusted terms.

## Rule 8: Dynamic Exit

### Activation
Flagged when ANY: LOW-YIELD CC 3+ cycles | unresolvable R3/R4 violation | margin blocks 30%+ deployment. Flagging ≠ authorization. Concentration escalation: >40% NLV every cycle, 25-40% every 2 cycles.

### Exit Scope (R1 violations)
`target_shares = floor((household_nlv × 0.15) / price)`. **Locked at campaign inception** in `dynamic_exit_campaigns` — no mid-campaign recompute.

### Gate 1: Capital Velocity (HARD)
`(Freed Margin × Conviction Modifier) > |Net Walk-Away Loss|`

| Tier | Modifier | Criteria |
|---|---|---|
| High | 0.20 | Positive EPS + above-median revenue + no downgrade |
| Neutral | 0.30 | Default |
| Low | 0.40 | Negative EPS OR revenue decline OR margin <5% |

- Modifier IS the hardcoded yield proxy. Do NOT query live VRP / `/scan` to recompute — circular.
- **Conviction state lock:** locked in `dynamic_exit_campaigns` for the campaign. No mid-campaign downgrade retroactively fails prior phases.
- **Override:** via Command Deck attestation, 90-day expiry, justification logged.
- **Projected post-exit margin check:** must compute `Projected_EL_Post_Exit` against global maintenance margin (use IBKR `whatIfOrder()` when available; conservative haircut fallback). Prevents phantom-margin scenario.
- **Pre-residency shares:** Net Walk-Away Loss must include estimated mainland federal cap-gains liability on built-in gains. Static `tax_liability_override` parameter, default $0.00, manual append for legacy lots. Known compliance gap pending lot-level tracking.

### Gate 2: Position Sizing (HARD)
- Loss ≤2% of position value → 100% liquidation OK
- Loss >2% → 25-33% of contracts per cycle

### Gate 3: Operator Attestation
Math gates evaluated deterministically in Python. If math passes, Smart Friction flow:
1. Acknowledge exact Act 60 realized loss (whole-dollar precision).
2. Confirm cure target (concentration / sector / margin).
3. Log short-form qualitative thesis.
4. STAGE button activates only after all requirements met.

Telegram is the final transmission remote only. After STAGE, inline `[TRANSMIT]/[CANCEL]` keyboard pushed; TRANSMIT triggers JIT re-validation of Gate 1 vs live spot before TWS routing. JIT fail blocks transmission, requires re-stage.

**Integer Lock:** operator types exact whole-dollar realized loss to unlock STAGE.

### Walk-Away P&L
`Walk-Away P&L per share = Strike + Call Premium − Adjusted Cost Basis`
- >0: profitable exit, full liquidation OK (functionally Mode 2).
- <0: capital liberation trade. Must pass G1 + G2 + G3.

### Management
- All orders staged via Command Deck, transmitted via Telegram JIT flow with `transmit=True` + GTC.
- **Reserve auto-release:** G1/G2 fail or attestation declined → reserved excess shares released back to defensive CC pool.
- `/cc` is Dynamic Exit aware: defensive CCs only on `target_shares`, excess shares reserved.
- **Rule 11 leverage-breach exception:** direct `STK_SELL` limit order permitted in lieu of CC-assignment when waiting creates margin risk. Still requires Operator Attestation + per-order Telegram confirmation.

## Rule 9: Red Alert
**Activation (any 2-of-4):**
1. 3+ names >20% concentration (R1)
2. All-book EL below VIX floor (R2)
3. Vikram EL <20% (R6)
4. No position can generate 30% annualized at strike at/above basis (all in Mode 1)

**Glide path:** R9 reads SOFTENED rule statuses (post-glide-path), not raw evaluator output (ADR-003). Phase 3A.5b: reporting-only, does not auto-transition mode.

**Hysteresis:** Fires on 2-of-4. Clears only when ALL 4 deactivation criteria met (asymmetric thresholds prevent oscillation).

**Deactivation (all-4 required):**
1. All-book EL ≥ VIX floor
2. Vikram EL >20%
3. ≤2 names >20% concentration
4. ≥2 positions can generate 30% annualized at/above basis

## Rule 10: Excluded Instruments
- **SPX box spreads:** excluded from ALL calcs (NLV, EL, R1, R3, R4).
- **Legacy picks (SLS, GTLB):** excluded from Wheel/R3/R4. Included in NLV for R1 only. NOT subject to R8.
- **Negligible (IBKR fractional, TRAW.CVR):** excluded from everything.

## Rule 11: Portfolio Circuit Breaker (Leverage)
`leverage = sum(qty × beta × spot) / household_NLV` (6-mo beta vs SPY).

**Denominator:** ALL-account household NLV (incl. Roth, with R10 exclusions). Distinct from R2 (margin-eligible only). ADR-001.

**Cap: 1.50x.** Breach:
- All new CSP staging halted on that household (`/scan` blocked).
- Existing positions managed normally (Mode 1 harvest continues, de-risks via assignment).
- **Hysteresis release at 1.40x** (10% buffer prevents oscillation).
- Freeze release does NOT require attestation (defensive constraint, not loss-crystallizing).

## Prohibited
- Stage Dynamic Exit without Operator Attestation
- Mode 1 CCs within 3% of current price
- Mode 2 CC Delta >0.30 or CSP Delta >0.25
- CSP within 7 days of earnings
- Sub-30% annualized on Mode 2
- Sell below basis without R5 exception + attestation
- Biotech, Pharma, Chinese equities, meme stocks, airlines in the Wheel
- R3/R4 violations on entry

## ADRs (binding for code, non-normative for portfolio decisions)
- **ADR-001:** R2 denominator = margin-eligible NLV; R11 = all-account NLV.
- **ADR-002:** Glide path symmetric tolerance bands (per-rule flat absolute).
- **ADR-003:** R9 reporting-only in Phase 3A.5b; no auto mode transition until Phase 3B.
- **ADR-004:** Smart Friction, Cure Console, deterministic gate enforcement. Operator Attestation implementation spec.
- **ADR-014:** Mode state machine retired. WARTIME/AMBER/PEACETIME desk modes removed. Leverage gating via Rule 11 (1.50× cap, 1.40× hysteresis).
