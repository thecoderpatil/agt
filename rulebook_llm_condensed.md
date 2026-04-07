# AGT Equities — Rulebook Reference (LLM Context)
# Condensed from Portfolio_Risk_Rulebook_v9.md
# This is NOT the governing document — the full v9 Rulebook is authoritative.

## Rule Precedence (when rules conflict)
1. Avoid forced broker liquidation (Rule 6) — overrides everything
2. Portfolio deployment governor (Rule 2 EL)
3. Concentration, sector, correlation (Rules 1, 3, 4)
4. Operating procedures (Rule 7, then Rule 8)

## Key Definitions
- **Household NLV:** Combined NLV across Individual + Vikram IND + Roth IRA. Excludes SPX box spread notional.
- **Excess Liquidity (EL):** IBKR Current Excess Liquidity = Equity with Loan Value − Maintenance Margin.
- **Adjusted Cost Basis:** Cost Basis − (Accumulated Premium ÷ Shares Owned).
- **Tested:** Underlying within 2% of short strike, or through it.

## Accounts
| Account | Margin | CSP | Notes |
|---|---|---|---|
| Individual (U21971297) | Yes | Yes | Primary margin account |
| Vikram IND (U22388499) | Yes | Yes | Rule 6: 20% EL floor |
| Roth IRA (U22076329) | No | No | Shares + CCs only |

EL measured across margin accounts only (Individual + Vikram IND). Roth excluded.

## Rule 1: Concentration — max 20% of household NLV per name at entry.
- Drawdown exception: extends to 30% if stock fell ≥30% from basis.
- Breach (decline or appreciation): Freeze (no new shares, no CSPs). Continue CCs. No forced selling.

## Rule 2: EL by VIX (v9: 5 tiers, 60% max deploy cap)
| VIX | Min EL Retain | Max Deploy |
|---|---|---|
| <20 | 80% | 20% |
| 20-25 | 70% | 30% |
| 25-30 | 60% | 40% |
| 30-40 | 50% | 50% |
| 40+ | 40% | 60% |

EL denominator = margin-eligible NLV only (Individual + Vikram IND). Roth IRA NLV excluded. The last 40% of EL is the survival bunker — no VIX level unlocks it.

## Rule 3: Sector — max 2 names per Yahoo Finance industry bucket.
## Rule 4: Correlation — max 0.6 rolling 6-month pairwise at entry.
## Rule 5: No selling shares below basis except: Dynamic Exit (Rule 8), thesis deterioration (CIO required), forced liquidation avoidance (Rule 6 <10%), emergency (fraud/delisting).
## Rule 6: Vikram IND — maintain ≥20% EL. Breach: freeze entries, write CCs, reduce margin. <10% EL: evaluate selling smallest position.

## Rule 7: Covered Call / CSP

### Mode 1 (stock BELOW basis) — Defensive
- Strike: start at basis, move down until ≥5% annualized. Min $0.03 premium. Min 3% OTM.
- DTE: 14-21d (near basis) or 21-30d (far below basis).
- Rolling: tested 5+ DTE hold, 3-4 DTE prepare, <3 DTE roll up/out for any credit. NEVER allow assignment below basis (except Rule 8).
- LOW-YIELD flag: <5% annualized for 3+ cycles → Dynamic Exit evaluation.
- Extended-DTE supplement: 6+ months in Mode 1 AND >25% below basis → additional 45-60 DTE CC, strike ≥10% above price AND ≥15% below basis, min $0.30.
- Earnings: strike within 10% of price + earnings in window → wait. Strike >15% above price → OK.

### Mode 2 (stock AT/ABOVE basis) — Harvest
- 30%/130% framework: Annualized = (Premium/Strike) × (365/DTE). Min 30%, max 130%.
- **Delta ≤ 0.30.** If only strikes meeting 30% floor have Delta >0.30, do not sell.
- 50% profit target: close and re-enter.
- Rolling: tested 3+ DTE hold. ITM <3 DTE: roll for ≥$0.20 credit. Cannot roll → let assign (tax-exempt gain).
- Dividend: if ITM + ex-div approaching + extrinsic < dividend → roll or close immediately.
- Assignment: welcome. Re-enter via CSP at same/nearby strike.

### CSP (new names)
- 30%/130% framework applies. **Delta ≤ 0.25.**
- **No CSP within 7 calendar days of earnings.**
- 50% profit: enter GTC BTC at 50% simultaneously with STO.
- Pre-entry: Rule 2 EL OK, Rule 1 ≤20%, Rule 3 sector OK, Rule 4 correlation OK.
- Roth IRA: no CSPs.

### Order Execution
- Limit at mid minus $0.03-0.05. Enter after 9:45 AM ET. If not filled by 10:30 → move to mid.

### Assignment Protocol
- Confirm status each morning. Never sell shares while short calls are open.

### Corporate Actions
- Suspend all new CC/CSP on affected name until OCC confirms adjusted terms.

## Rule 8: Dynamic Exit

### Gates (ALL must pass)
**Gate 1 — Capital Velocity (hard):**
Freed Margin × Conviction Modifier > |Net Walk-Away Loss|
- High conviction (0.20): positive EPS + above-median revenue + no downgrade
- Neutral (0.30): default
- Low (0.40): negative EPS OR revenue decline OR low margin (<5%)
- Override via `/override TICKER TIER reason` (expires 90d)

**Gate 2 — Position Sizing (hard):**
- Loss ≤2% of position value → 100% liquidation OK
- Loss >2% → 25-33% of available contracts per cycle

**Gate 3 — CIO consultation (mandatory).** No bypass.

### Exit Scope (Rule 1 violations)
target_shares = floor((household_nlv × 0.15) / price). Excess = current − target.

## Rule 9: Red Alert — activates when ANY 2: 3+ names >20%, EL below VIX floor, Vikram EL <20%, all names in Mode 1.

## Rule 10: Excluded Instruments
- **SPX box spreads:** excluded from ALL calculations (NLV, EL, concentration, sector, correlation).
- **Legacy picks (SLS, GTLB):** excluded from Wheel procedures, sector, correlation. Included in NLV for Rule 1.
- **Negligible holdings (IBKR fractional, TRAW.CVR):** excluded from everything.

## Rule 11: Portfolio Circuit Breaker (v9.0)
- Gross beta-weighted equity notional may not exceed **1.50x** of household NLV.
- `leverage = sum(qty * beta * spot) / household_NLV` (beta = trailing 6-month vs SPY)
- If breached: block `/scan` (new CSPs). Existing positions managed normally. Mode 1 CC harvest continues.
- Release: leverage must drop below **1.40x** (10% hysteresis buffer).
- Rationale: tail-event protection. Correlation goes to 1, beta dominates. Prevents IBKR forced liquidation at VIX 40+.

## Prohibited
- Mode 1 CCs within 3% of current price
- Mode 2 CC Delta >0.30 or CSP Delta >0.25
- CSP within 7 days of earnings
- Biotech, Pharma, Chinese equities, meme stocks, airlines in the Wheel
- Sub-30% annualized on Mode 2 trades
- Bypass CIO for Dynamic Exits
