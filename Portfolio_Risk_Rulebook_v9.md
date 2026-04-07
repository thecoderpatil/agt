# Portfolio Risk Management Rulebook
**AGT Equities — Pure Heitkoetter Wheel / Act 60 Chapter 2**
**Version 9.0**

**Governing principle:** The objective is the **maximum income for the minimum risk taken** — not the maximum income possible. This portfolio operates under Act 60 Chapter 2 (Puerto Rico) and is managed on the assumption that **the account holder maintains bona fide Puerto Rico residency and that all relevant income is treated as Puerto Rico-source for applicable tax purposes.** Under this framework, options premium and capital gains are tax-exempt. Since gains are tax-exempt, there is no incentive to take excess risk to compensate for tax drag. Losses have limited-to-zero tax-recovery value under the Act 60 structure. The asymmetry is strong: be aggressive about collecting premium on sound structures, conservative about taking on incremental risk. Tax treatment is conditional and may vary by sourcing, residency status, holding period, and transaction type; tax counsel review is required for any decision justified primarily by tax status.

---

## Rule Precedence

When rules conflict, apply this hierarchy:

1. **Avoid forced broker liquidation** — Rule 6 margin compliance and IBKR maintenance requirements override all other rules, including Rule 5.
2. **Maintain portfolio deployment governor** — Rule 2 EL compliance.
3. **Concentration, sector, and correlation controls** — Rules 1, 3, and 4.
4. **Operating procedures** — Rule 7, then Rule 8.

Rationale: IBKR will auto-liquidate positions if margin compliance is breached. No philosophical rule survives a forced liquidation at the worst possible price.

---

## Definitions

**Household Net Liquidation Value (NLV):** The combined net liquidation value across all accounts (Individual + Vikram IND + Roth IRA) as reported by IBKR. Excludes SPX box spread notional (Rule 10).

**Excess Liquidity (EL):** IBKR **Current Excess Liquidity** for the securities segment: **EL = Equity with Loan Value − Maintenance Margin.** If Current EL goes negative, IBKR may begin liquidating positions. The "Current" metric (not Look Ahead or Post-Expiry) is the governing measurement for all Rulebook thresholds.

**Cost Basis:** Initial purchase price per share at the time of assignment or market purchase.

**Adjusted Cost Basis:** Cost Basis − (Accumulated Premium Collected ÷ Shares Owned). This is the effective breakeven after premium harvesting.

**Tested:** A short option where the underlying price has moved to within 2% of the strike, or through it.

**OTM Distance:** (Strike − Current Price) ÷ Current Price for calls; (Current Price − Strike) ÷ Current Price for puts. Expressed as a percentage.

---

## Rule 1: Single-Name Concentration

**Base limit:** No single position may exceed 20% of household net liquidation value at time of entry.

**Drawdown exception:** If a position exceeds 20% solely because the stock declined (not because new shares were added), the limit extends to 30%, provided the stock has fallen at least 30% from cost basis.

**Drift protocol (decline):** If a position drifts above 20% due to price decline:

- **Freeze** — no additional shares may be purchased, no CSPs may be sold on that name.
- **No forced selling** — underwater positions are never liquidated solely to meet this rule.
- **Continue writing covered calls** per the standard operating procedure (Rule 7).
- If the stock rallies to or above cost basis, assignment is welcomed (gain is tax-exempt under Act 60).

**Drift protocol (appreciation):** If a position exceeds 20% due to price appreciation or portfolio contraction (other positions shrinking), the same Freeze protocol applies — no new shares, no CSPs on that name. If the position is in Mode 2, welcome assignment to naturally reduce concentration. Do not force-sell to cure appreciation drift.

---

## Rule 2: Minimum Excess Liquidity by VIX (Deployment Governor)

A minimum percentage of total portfolio excess liquidity must be **retained as a cash cushion** at all times. Higher VIX = lower reserve requirement = more capital available for deployment into premium selling when IV is richest.

| VIX Level | Min EL to Retain | Max Deployable |
|-----------|-----------------|----------------|
| Below 20  | 80%             | 20%            |
| 20-25     | 70%             | 30%            |
| 25-30     | 60%             | 40%            |
| 30-35     | 50%             | 50%            |
| 35-40     | 40%             | 60%            |
| 40-50     | 30%             | 70%            |
| 50+       | 25%             | 75%            |

**Scope:** Applies to total portfolio deployment across all accounts. If current EL is below the required minimum, no new positions may be added until EL recovers through premium accumulation, margin relief, or position assignment at/above cost basis.

**Measurement:** IBKR Current Excess Liquidity (see Definitions), measured across **margin-eligible accounts only** (Individual + Vikram IND). Roth IRA net liquidation value is excluded because IRA accounts cannot deploy margin or sell naked CSPs. VIX is checked at time of order entry.

**Design rationale:** A contrarian premium-selling rule. When VIX is elevated and premium is richest, more capital is freed for deployment. When VIX is low and premium is thin, capital is conserved.

---

## Rule 3: Sector Concentration

No more than **2 names from the same industry classification bucket** may be held simultaneously across all accounts.

**Universe:** All S&P 500 and NASDAQ-100 constituents.

**Sector classification source:** Yahoo Finance industry field via yfinance (`Ticker.info["industry"]`), cached in the local `ticker_universe` SQLite table. Refreshed monthly via `/sync_universe`. This is **not** a licensed GICS dataset. If an official GICS feed is adopted, this rule will be migrated to GICS Industry Group identifiers.

**Granularity rationale:** Broad sector groupings (11 categories) are too coarse — "Information Technology" would lump software, semiconductors, hardware, and fintech together, making Rule 3 unworkable. The Yahoo Finance industry field (~24+ categories) provides the right level of separation. Examples:

| Industry Classification | Example Names |
|---|---|
| Software - Application | ADBE, CRM, UBER |
| Software - Infrastructure | MSFT, PYPL |
| Semiconductors | QCOM, NVDA, AMD |
| Oil & Gas Integrated | XOM, CVX |
| Oil & Gas E&P | OXY |
| Banks - Diversified | JPM |
| Credit Services | AXP, V, MA |
| Discount Stores | WMT, COST, TGT |
| Restaurants | MCD |
| Healthcare Plans | UNH |
| Drug Manufacturers - General | JNJ |

NOTE: The "Example Names" column above is illustrative, not exhaustive. The actual classification is determined dynamically by the `ticker_universe` table. Run `/sync_universe` to refresh.

**Classification disputes:** If yfinance returns a classification that seems incorrect (e.g., a fintech company classified as "Software"), default to the industry group where the company's revenue is most concentrated. Flag the dispute in the Telegram output.

**Legacy violations:** Existing sector violations (e.g., 3 names in one industry bucket from pre-rulebook entries) are resolved through natural assignment at/above cost basis or Dynamic Exit (Rule 8). No forced selling to cure a legacy violation.

---

## Rule 4: Pairwise Correlation Limit

No two positions may have a **rolling 6-month correlation above 0.6** at time of entry.

**Measurement:** Daily returns over the trailing 6-month window. Recalculated at each new position entry and at the start of each calendar month for existing positions.

**If an existing pair breaches 0.6:** Freeze both names (no additions). Write covered calls on the smaller position and welcome assignment when it approaches cost basis. If the position qualifies for Dynamic Exit (Rule 8), evaluate aggressive exit to resolve the correlation violation faster.

---

## Rule 5: Capital Velocity > Nominal Breakeven

Under the Act 60 framework, options premium income is tax-exempt. Losses have limited-to-zero recovery value — no offset, no carry-forward, no meaningful tax benefit. A realized loss with no compensating yield is purely destructive.

**Core principle:** A loss that is mathematically compensated by the yield on redeployed capital is not a loss — it is a rotation cost.

**Operational rules:**

- Never sell shares on the open market below cost basis for any reason, **except** under the following conditions:
  - **Rule 8 Dynamic Exit:** The trade passes all three gates and receives CIO authorization.
  - **Thesis Deterioration:** The fundamental investment thesis on the holding has turned negative (e.g., structural revenue decline, margin compression, competitive obsolescence, regulatory destruction). Requires CIO consultation and logged rationale before execution.
  - **Forced Liquidation Avoidance:** Rule 6 breach protocol at <10% EL, per Rule Precedence (Rule 6 overrides Rule 5).
  - **Emergency Risk Event:** Imminent delisting, confirmed fraud, or bankruptcy. Requires logged rationale.
- Never write covered calls with the intent of forcing assignment at a loss **unless** the trade passes Rule 8 Dynamic Exit gates and receives CIO authorization.
- If a stock rallies to or above cost basis, assignment is welcomed — the gain is tax-exempt.
- Capital for new positions is sourced exclusively from accumulated premium income or assignment proceeds.

---

## Rule 6: Vikram IND Margin Backstop

**Excess liquidity floor:** The Vikram IND account must maintain IBKR Current Excess Liquidity of at least **20% of net liquidation value** at all times.

**Breach protocol:**

1. Immediately cease all new position entries in the account.
2. Identify the most concentrated position in the account.
3. Write covered calls per Rule 7 and apply all premium income to margin debt reduction.
4. If EL drops below 10%, evaluate outright sale of the smallest position with the least loss as a last resort to avoid forced IBKR liquidation. Per Rule Precedence, this overrides Rule 5.

**Monitoring:** Check Vikram IND Current EL daily before market open.

---

## Rule 7: Covered Call / CSP Operating Procedure

Covered calls operate in two distinct modes depending on whether the stock is above or below cost basis.

### Assignment Event Protocol

American-style equity options can be exercised by the holder **at any time** before expiration. Assignment can arrive overnight without warning. The following operational rules apply at all times:

- **Confirm assignment status** each morning before placing any follow-on orders. Do not assume short options are still open.
- **Never sell shares while short calls are open** on the same position unless the call is simultaneously closed. Selling shares with open short calls creates naked call exposure.
- If assigned unexpectedly on a Mode 1 CC below cost basis, log as an unplanned assignment, update the premium ledger, and evaluate re-entry via CSP if the name remains in the Wheel universe. Escalate to CIO if assignment creates a material rule violation.

### Corporate Action Protocol

Upon announcement of a corporate action (merger, spin-off, special dividend, tender offer) affecting an underlying position, **suspend all new CC/CSP activity on that name** and move to manual review until the adjusted contract terms are confirmed by OCC. Existing short options remain managed per standard rolling rules.

### Mode 1: Stock BELOW Cost Basis — Defensive Premium Collection

**Objective:** Collect premium. Avoid assignment. Protect shares for eventual recovery.

**The 30%/130% framework does NOT apply in this mode.**

**Strike selection:**
1. Start at the cost basis strike. Check the bid.
2. If premium >= 5% ROI annualized: sell there.
3. If premium < 5% ROI annualized: move down one strike at a time until premium >= 5% ROI annualized.
4. Minimum premium floor: $0.03/contract. Below that, skip the name for the cycle.
5. Minimum OTM distance from current price: 3%.

**Low-yield flag:** If the Mode 1 CC generates less than 5% annualized, flag as LOW-YIELD CC. For names flagged on standard 14-21 DTE, prioritize the extended-DTE supplement (45-60 DTE).

**Dynamic Exit trigger:** If a position is flagged LOW-YIELD CC for 3+ consecutive cycles, it is automatically flagged for Dynamic Exit evaluation (Rule 8).

**DTE guidance:**
- Strike far below basis: extend DTE to 21-30 days.
- Premium available near basis: use 14 DTE for faster theta decay.

**Earnings awareness:**
- Strike within 10% of current price + earnings in DTE window: wait until after earnings.
- Strike more than 15% above current price: selling through earnings is acceptable.

**Rolling rules (CRITICAL):**
- Tested with 5+ DTE: hold.
- Tested with 3-4 DTE: prepare to roll.
- ITM with <3 DTE: **roll up and out immediately** for any net credit, even $0.01.
- Cannot roll for any credit: roll out in time only (same strike, further expiry).
- **Never allow assignment below cost basis** except via Rule 8 Dynamic Exit with CIO authorization.

**If the stock rallies through the short strike:** Roll up and out repeatedly, collecting small credits. When the strike reaches cost basis, transition to Mode 2.

### Mode 1 Supplement: Extended-DTE Premium Harvest

**Applies when:** Position in Mode 1 for 6+ months AND price is more than 25% below cost basis.

**Rules:**
1. Strike must be at least 10% above current price.
2. Strike must be at least 15% below cost basis.
3. DTE: 45-60 days.
4. In addition to the standard 14-21 DTE CC, not a replacement.
5. Roll trigger: stock rallies to within 8% of the strike — close immediately.
6. Premium threshold: at least $0.30/contract.

### Mode 2: Stock AT or ABOVE Cost Basis — Standard Heitkoetter Wheel

**Objective:** Generate income at target returns. Assignment is welcome — the gain is entirely tax-exempt.

**The 30%/130% framework applies.**

    Annualized Return = (Premium / Strike) x (365 / DTE)

| DTE | 30% Floor (Min Premium) | 130% Ceiling (Max Premium) |
|-----|------------------------|---------------------------|
| 14  | Strike x 1.15%         | Strike x 4.99%            |
| 21  | Strike x 1.73%         | Strike x 7.48%            |
| 30  | Strike x 2.47%         | Strike x 10.68%           |

**Step 1:** Find the nearest OTM strike where annualized return >= 30%.
**Step 2:** If annualized return exceeds 130%, move up one strike.
**Step 3:** If no strike generates 30%+, do not sell. Wait for IV expansion.
**Delta constraint:** No Mode 2 CC may be sold at a strike with Delta exceeding 0.30, regardless of annualized yield. If the only strikes meeting the 30% floor have Delta > 0.30, do not sell. Wait for IV expansion.

**50% Profit Target Close:** When a Mode 2 CC has decayed to 50% of original premium, evaluate close-and-re-enter. Does not apply to Mode 1 calls.

**Dividend ex-date awareness:** If an ex-dividend date occurs within the DTE window and the short call is ITM, compare the call's remaining extrinsic value to the dividend amount. If extrinsic value < dividend, early assignment risk is elevated — roll or close immediately.

**Rolling rules (Mode 2):**
- Tested with 3+ DTE: hold.
- ITM with <3 DTE: roll up and out for net credit >= $0.20/contract.
- Cannot roll for $0.20 and strike is at/above basis: **let it get called away. Tax-exempt gain.**

**Assignment-and-Re-Enter:** When a Mode 2 position is called away, immediately evaluate re-entry via CSP at the same or nearby strike. The cycle (CC -> assignment -> CSP -> assignment -> CC) is the highest-income outcome under Act 60.

### CSP Operating Procedure (New Names Only)

**Roth IRA restriction:** Naked CSPs are not permitted in the Roth IRA. All CSP activity occurs in the Individual or Vikram IND accounts.

CSPs are sold only on names where assignment is welcome. The 30%/130% framework applies identically.

**Delta constraint:** No CSP may be sold at a strike with Delta exceeding 0.25 (absolute value), regardless of annualized yield. If the only strikes meeting the 30% floor have Delta > 0.25, do not sell. Wait for IV expansion or a different name.

**Earnings buffer:** No CSP may be initiated within 7 calendar days of a scheduled earnings release for the underlying name.

**50% Profit Target Close:** When a CSP has decayed to 50% of original premium, close the position and evaluate redeployment into a fresh 30%+ setup. Enter a GTC buy-to-close order at 50% of collected premium simultaneously with the initial sell-to-open.

**Pre-entry checklist:**
- [ ] EL will remain above VIX-required minimum after the CSP (Rule 2)
- [ ] Position at assignment price would not exceed 20% of household NLV (Rule 1)
- [ ] No sector violation created (Rule 3)
- [ ] No correlation > 0.6 with existing names (Rule 4)
- [ ] Delta ≤ 0.25 (absolute value)
- [ ] No earnings within 7 calendar days

**CSP candidate universe:** All S&P 500 and NASDAQ-100 constituents with liquid weekly options, filtered by the CSP Scorecard (Fundamental, Technical, Portfolio Fit). The CSP Scorecard ranks candidates dynamically based on current IV rank, fundamentals, and Rulebook compliance. There is no static candidate list. Run `/scan` to generate the ranked pipeline.

**Assignment management:** If a CSP is ITM with <5 DTE, either close early (partial loss) or accept assignment and flip to Mode 2 covered calls immediately. Assignment is preferred under Act 60.

### Order Execution (Both Modes)

- Place limit orders at mid minus $0.03-0.05.
- Wait until 9:45 AM ET to enter orders (avoid first-15-minute spread widening).
- If not filled by 10:30 AM ET, move to mid.
- Sell largest-premium positions first to get capital working.

---

## Rule 8: Dynamic Exit Matrix — Aggressive Exit Protocol

The Dynamic Exit Matrix is a **Tactical Override** for Engine 2 (Covered Calls). It governs the deliberate acceptance of assignment below adjusted cost basis to liberate trapped margin for rotation into higher-yielding Heitkoetter Wheel setups.

### Activation Criteria

A position is flagged for Dynamic Exit evaluation when ANY of the following are true:

1. Flagged LOW-YIELD CC (Rule 7 Mode 1) for 3+ consecutive cycles.
2. Creates a Rule 3 (sector) or Rule 4 (correlation) violation unresolvable by natural recovery.
3. Margin consumption blocks deployment into compliant 30%+ Wheel setups (measurable via Rule 2 EL shortfall).

Flagging does NOT authorize the exit. The exit requires passing all three gates.

### Concentration Escalation Schedule

| Position as % of Household NLV | Evaluation Frequency |
|---|---|
| > 40% | Every cycle (every Monday) |
| 25-40% | Every 2 cycles |
| < 25% | Standard: 3 consecutive LOW-YIELD CC cycles |

Escalation triggers Gate 1 evaluation automatically but does NOT bypass any gate. All three gates must pass.

### Exit Scope

Dynamic Exits for Rule 1 violations target only the overweight portion:

    target_shares    = floor((household_nlv x 0.15) / current_price)
    excess_shares    = current_shares - target_shares
    excess_contracts = floor(excess_shares / 100)

The 15% target (not 20%) provides a 5-point downside cushion.

Exception: Rule 3/Rule 4 violations are binary — full position may be evaluated.

### Gate 1: Capital Velocity Test (Hard Gate)

    (Freed Margin x Conviction Modifier) > |Net Walk-Away Loss|

| Tier | Modifier | Criteria |
|---|---|---|
| High (0.20) | Harder to exit | Positive EPS + above-median revenue + no downgrade |
| Neutral (0.30) | Default | Does not qualify for High or Low |
| Low (0.40) | Easier to exit | Negative EPS OR revenue decline OR low operating margin (< 5%) |

Conviction is computed weekly from yfinance. CIO may override with logged justification (`/override` command). Overrides expire after 90 days.

- **Freed Margin** = Strike x 100 x Contracts
- **Net Walk-Away Loss** = |Strike + Premium - Adjusted Cost Basis| x 100 x Contracts

The expected 12-month redeployment yield must exceed the realized loss. This gate is a hard mathematical binary. No overrides. No exceptions.

**Pre-residency shares:** If any shares in the position were acquired before the account holder established bona fide Puerto Rico residency, the Net Walk-Away Loss calculation must include the estimated mainland federal capital gains tax liability on those shares' built-in gains. Gate 1 is evaluated on the after-tax loss, not the nominal loss.

### Gate 2: Position Sizing (Hard Gate)

| Walk-Away Loss Severity | Max Contracts per Cycle |
|------------------------|------------------------|
| <= 2% of position market value | 100% (full liquidation) |
| > 2% of position market value | 25-33% of available contracts |

### Gate 3: CIO Consultation (Mandatory)

No Aggressive Exit may be staged without the exact strike and allocation being mathematically verified and authorized by the CIO Oracle via the Telegram integration. The CIO Oracle:

1. Receives the full options ladder and position context.
2. Evaluates Gate 1 and Gate 2 programmatically.
3. Returns APPROVED with the optimal strike and quantity, or REJECTED with the reason.

### Walk-Away Profit Calculation

    Walk-Away P&L per Share = Strike + Call Premium - Adjusted Cost Basis

Where: Adjusted Cost Basis = Initial Purchase Price - (Accumulated Premium Collected / Shares Owned)

- Walk-Away P&L > 0: profitable exit. Full liquidation authorized (functionally a Mode 2 trade).
- Walk-Away P&L < 0: capital liberation trade. Must pass Gate 1 and Gate 2.

### Management Rules

- Place all aggressive exit CCs with `transmit=False` for TWS review.
- If stock rallies above the exit strike before expiry and walk-away P&L would be negative: roll per Mode 1 rules.
- Track all aggressive exits separately in the premium ledger.
- After assignment: immediately evaluate freed capital for CSP deployment on a diversifying, uncorrelated name.

### Capital Priority After Assignment

1. If Rule 6 is breached: apply proceeds to Vikram IND margin debt reduction.
2. If Rule 2 EL is below floor: hold as cash until EL recovers.
3. If EL is compliant: deploy into the highest-IV name on the CSP candidate pipeline that passes Rules 1, 3, and 4.

### CIO Payload Format

The Dynamic Exit CIO payload is auto-generated by the 3:30 PM watchdog or manually via `/dynamic_exit`. It contains:

```
━━ Dynamic Exit: {TICKER} ({Household}) ━━

Position: {shares}sh @ ${spot} = ${value}
Concentration: {pct}% of ${nlv} NLV
Adjusted Basis: ${basis}
Gap: {gap}%

Escalation: {EVERY_CYCLE|EVERY_2_CYCLES|STANDARD}

Conviction: {HIGH|NEUTRAL|LOW} (x{modifier}) [{source}]
  EPS Trend: {POSITIVE|FLAT|NEGATIVE}
  Revenue vs Sector: {ABOVE|AT|BELOW}
  Analyst: {UPGRADE|STABLE|DOWNGRADE}
  Margins: {HIGH_MARGIN|MID_MARGIN|LOW_MARGIN}

Exit Scope: {OVERWEIGHT_ONLY|FULL_POSITION|OVERWEIGHT_ENCUMBERED}
  Target: 15% = {N}sh
  Excess: {N}sh = {N}c
  Available: {N}c (after existing CC encumbrance)
  After exit: {N}sh ({pct}%)

Gate 1 (x{modifier}):
  ✅ $260C @ $1.50 | freed $26,000 x0.40 = $10,400 vs loss $5,200 (2.0x)
  ❌ $255C @ $2.10 | freed $25,500 x0.40 = $10,200 vs loss $7,100 (1.4x)
  Expiry: 2026-05-01 (27d)

CIO: If approved, generate:
  /exit ADBE 260 2026-05-01 1 vikram
```

The CIO Oracle receives this payload and either:
1. **APPROVES** — generates the `/exit` command shown, which the user pastes into Telegram
2. **REJECTS** — provides quantitative reason (e.g., "Gate 1 ratio 1.4x is marginal, wait for higher IV cycle")
3. **OVERRIDES CONVICTION** — directs the user to run `/override TICKER TIER reason` before re-evaluating

The CIO may NOT modify Gate 1 math, change the 15% buffer target, or bypass the overweight scope. The CIO's authority is:
- Approve/reject the exit at a specific strike
- Override the conviction tier with logged justification
- Request re-evaluation at a different DTE window

### Dynamic Exit Workflow

**Automated path (3:30 PM watchdog):**

1. Watchdog detects position above 20% of household NLV
2. Checks escalation tier (>40% = every Monday, 25-40% = every 2 weeks)
3. If evaluation is due, auto-generates the full CIO payload
4. Sends payload to Telegram
5. User pastes to CIO Oracle for Gate 3 consultation
6. CIO approves → generates `/exit TICKER STRIKE EXPIRY QTY HOUSEHOLD`
7. User pastes `/exit` command into Telegram
8. Bot validates: ticker, strike, contracts ≤ excess, available contracts > 0
9. Order placed with transmit=True + GTC
10. Premium ledger auto-updates on fill

**Interaction with /cc (Defensive CCs):**

The daily 9:45 AM `/cc` scan is aware of Dynamic Exit reservations. For any position flagged for Dynamic Exit (above 20% household NLV):
- Defensive CCs are written ONLY on `target_shares` (the shares being kept)
- Excess shares are reserved — no defensive CCs
- The `/cc` output shows: "ADBE: Xc reserved for Dynamic Exit"

Once the Dynamic Exit CC fills and shares are called away:
- Next `/cc` run sees fewer total shares
- The position may drop below 20%, removing the Dynamic Exit flag
- All remaining shares return to normal Defensive or Harvest CC treatment

**If existing short calls block the exit:**

When excess shares are already encumbered by defensive CCs, the payload shows OVERWEIGHT_ENCUMBERED with instructions to let the existing CC expire first. No `/exit` command is generated until contracts become available.

---

## Rule 9: Red Alert Protocol

Red Alert is activated when the portfolio is in a non-compliant state across multiple rules simultaneously.

### Activation Criteria (any 2 of the following):

- 3+ positions exceed the 20% concentration limit (Rule 1).
- All-book EL is below the VIX-required minimum (Rule 2).
- Vikram IND EL is below the 20% floor (Rule 6).
- No position can generate 30% annualized at a strike at/above cost basis (all names in Mode 1).

### Operating Procedure

**Covered calls:** All names in Mode 1 (Rule 7). Write at the highest strike generating >= $0.10 premium. Use extended-DTE supplement where criteria are met. Roll aggressively to avoid assignment below cost basis (unless Rule 8 authorizes).

**Dynamic Exit evaluation (MANDATORY):** Evaluate ALL Mode 1 positions for Dynamic Exit eligibility. Run every position through Gate 1. If any pass, escalate to CIO consultation immediately.

**Premium priority:**
1. Vikram IND margin debt reduction (if Rule 6 breached).
2. Cash accumulation toward EL floor compliance (Rule 2).
3. CSP deployment on diversifying names (if EL allows).
4. General income.

### Recovery Triggers

| Stock Price vs Cost Basis | Action |
|---------------------------|--------|
| More than 15% below basis | Mode 1. Extended DTE if criteria met. Evaluate Dynamic Exit. |
| Within 10-15% of basis | Transition zone. Write at basis strike if premium >= $0.10. |
| Within 5% of basis | Write at/above basis. Actively welcome assignment. |
| At or above basis | Mode 2: 30%/130% framework. Welcome assignment. Tax-exempt gain. |

### Deactivation Criteria (ALL must be true):

- All-book EL meets or exceeds the VIX-required minimum (Rule 2).
- Vikram IND EL is above the 20% floor (Rule 6).
- No more than 2 positions exceed the 20% concentration limit (Rule 1).
- At least 2 positions can generate 30% annualized at strikes at/above cost basis (Mode 2).

---

## Rule 10: Excluded Instruments

### SPX Box Spreads (Margin Financing)

SPX box spreads are used exclusively as a margin financing tool in the Individual and Vikram IND accounts to reduce margin interest cost. They are **not** income-generating positions and are **excluded from all Rulebook calculations**, including:

- Net liquidation value for concentration percentages (Rule 1)
- Excess liquidity thresholds (Rules 2 and 6)
- Sector concentration (Rule 3)
- Correlation measurements (Rule 4)

Box spreads are managed independently and are not subject to covered call or CSP procedures.

### Legacy / Personal Picks

Positions entered as speculative personal picks outside the Heitkoetter Wheel framework (e.g., SLS, GTLB) are:

- **Excluded** from all Wheel operating procedures (Rule 7 modes, premium ledger tracking).
- **Excluded** from sector concentration counts (Rule 3) and correlation calculations (Rule 4).
- **Included** in net liquidation value for the purposes of Rule 1 concentration math (they consume capital).
- **Not subject** to Dynamic Exit (Rule 8) — the CIO has no authority over personal picks.
- Managed at the holder's sole discretion.

### Negligible / Non-Tradable Holdings

Fractional share positions (e.g., IBKR fractional), contingent value rights (e.g., TRAW.CVR), and similar non-tradable or negligible holdings are excluded from all Rulebook calculations and monitoring. If a negligible holding becomes a Wheel candidate (e.g., a CSP is sold on IBKR to initiate a position), it enters the Rulebook at that point and all rules apply from entry forward.

---

## Compliance Checklist — Monday Morning

- [ ] VIX level → determine minimum EL requirement (Rule 2)
- [ ] Household NLV → recalculate each position as % of total
- [ ] Per-account Current EL → compare to VIX-required floor (Rule 2) and Vikram IND 20% backstop (Rule 6)
- [ ] Red Alert status → check activation/deactivation criteria (Rule 9)
- [ ] For each name: Mode 1 or Mode 2?
- [ ] Mode 1 names: highest strike with >= $0.10 premium, >= 3% OTM
- [ ] Mode 1 names > 25% below basis and > 6 months: evaluate extended-DTE supplement
- [ ] Mode 1 names: check earnings calendar
- [ ] Mode 1 names flagged LOW-YIELD CC for 3+ cycles: flag for Dynamic Exit (Rule 8)
- [ ] Mode 2 names: strike where 30% <= annualized return <= 130%, Delta ≤ 0.30
- [ ] Mode 2 CCs at 50%+ profit: flag for mid-week close evaluation
- [ ] Dynamic Exit candidates: run Gate 1. If pass → CIO consultation
- [ ] Mode 2 CCs ITM with ex-div approaching: compare extrinsic value to dividend; roll if extrinsic < dividend
- [ ] Correlation breaches → recalculate (Rule 4)
- [ ] Sector count → confirm ≤ 2 names per industry classification bucket (Rule 3)
- [ ] No new entry would push any name above 20% of household NLV (Rule 1)
- [ ] Corporate actions pending on any underlying → suspend new CC/CSP, manual review

## Compliance Checklist — Mid-Week

- [ ] Mode 2 CCs at 50%+ profit: close-and-re-enter evaluation
- [ ] Mode 1 CCs within 2% of short strike: roll preparation
- [ ] Extended-DTE calls within 8% of short strike: close immediately
- [ ] Assignment events since Monday: confirm status, update premium ledger
- [ ] Dynamic Exit assignments settled: evaluate freed capital for CSP deployment

---

## Appendix A: Two-Engine Model

**Engine 1 — Cash-Secured Puts (Capital Deployment / Entry).** CSPs deploy capital into new wheel positions at favorable cost bases. Every CSP is either premium income (if OTM at expiry) or a new wheel position (if assigned). Engine 1 ignites when EL recovers and capital accumulates from premium + assignments.

**Engine 2 — Covered Calls (Income / Exit).** The primary engine. Every position with shares generates premium via covered calls. Mode 1: pure premium collection, minimal assignment risk. Mode 2: premium plus welcomed assignment (tax-exempt gain + capital redeployment). Freed capital feeds Engine 1.

**Tactical Override — Dynamic Exit Matrix (Rule 8).** A surgical override for Engine 2. When a position becomes a capital trap (low yield, margin drag, compliance blocker), the Dynamic Exit protocol evaluates whether deliberate assignment below adjusted basis is mathematically justified by the redeployment yield. Requires mandatory CIO consultation.

## Appendix B: Prohibited Actions

- Bypass CIO consultation for Dynamic Exits.
- Sell Mode 1 CCs at strikes within 3% of current price.
- Sell shares on the open market at a loss without qualifying under a Rule 5 exception (Dynamic Exit, thesis deterioration, forced liquidation avoidance, or emergency risk event).
- Add positions violating Rules 3 or 4.
- Accept sub-30% annualized returns on Mode 2 trades.
- Sell Mode 2 CCs or CSPs with Delta exceeding 0.30 (CCs) or 0.25 (CSPs).
- Initiate CSPs within 7 calendar days of a scheduled earnings release.
- Enter Biotech, Pharmaceuticals, Chinese Equities, Meme Stocks, or Airlines in the Wheel.

## Appendix C: Account Map

| Account | Type | Margin-Eligible | CSP-Eligible | Notes |
|---|---|---|---|---|
| Individual | Personal Brokerage | Yes | Yes | Primary margin account |
| Vikram IND | Brother Brokerage | Yes | Yes | Rule 6 backstop applies |
| Roth IRA | Retirement | No | No | Shares + CCs only |

## Appendix D: Commands

### Staging

| Command | Description |
|---|---|
| `/cc` | Daily 9:45 AM defensive + harvest CC scan |
| `/mode1` | Alias for `/cc` |
| `/scan` | Generate CSP candidate pipeline |
| `/dynamic_exit TICKER [household]` | Generate CIO payload for Dynamic Exit |

### Approval

| Command | Description |
|---|---|
| `/approve` | Review and place staged orders |
| `/reject` | Clear all staged orders |
| `/exit TICKER STRIKE EXPIRY CONTRACTS HOUSEHOLD` | Execute CIO-approved exit |
| `/override TICKER TIER reason` | Override conviction tier (expires 90 days) |

### Monitoring

| Command | Description |
|---|---|
| `/health` | Full portfolio diagnostic |
| `/vrp` | VRP veto report — all holdings (IV vs RV) |
| `/vrp TICKER` | Single-ticker VRP check (need not be a holding) |
| `/rollcheck` | Expiry and roll alerts |
| `/cycles TICKER` | CC cycle history for a ticker |
| `/fills` | Recent fill log |
| `/ledger` | Premium ledger state |
| `/budget` | API token and cost usage |
| `/status_orders` | Staged/approved order counts |

### Orders

| Command | Description |
|---|---|
| `/orders` | Live working orders with modify/cancel dashboard |

### LLM

| Command | Description |
|---|---|
| `/think <question>` | Ask with Sonnet 4.6 + Rulebook context |
| `/deep <question>` | Ask with Opus 4.6 + Rulebook context (default) |
| All freeform messages | Routed to Haiku 4.5 |

### System

| Command | Description |
|---|---|
| `/reconnect` | Reconnect to IB Gateway |
| `/sync_universe` | Refresh industry classification cache |
| `/cleanup_blotter` | Clear stale blotter entries |
| `/start` | Show command menu |
| `/status` | IBKR connection status |
| `/stop` | Shut down the bot |
| `/clear` | Reset conversation history |

---

---

## Rule 11 — Portfolio Circuit Breaker (Gross Beta-Weighted Leverage)

**Added in v9.0 (2026-04-07) per independent audit recommendation.**

Gross beta-weighted equity notional may not exceed **1.50x** of household NLV. Computed per household:

```
leverage = sum(qty × beta × spot) / household_NLV
```

where beta is the trailing 6-month beta vs SPY for each underlying.

**If leverage > 1.50x:**
- All new CSP staging halts on that household (`/scan` blocked)
- Existing positions managed normally per Rule 7 (defensive exit on overweight)
- Mode 1 CC harvest continues (de-risks via assignment/called-away)
- Rule 11 freeze released only when leverage drops below **1.40x** (10% hysteresis buffer prevents oscillation)

**Rationale:** Rule 4 (sector correlation) is fair-weather. In tail events, dispersion vanishes and beta dominates. A leverage cap prevents IBKR maintenance margin expansion (35-40% on tech equities at VIX 40+) from triggering forced auto-liquidation. The 1.50x limit with 40% EL retain floor (Rule 2 v9) ensures the portfolio can absorb a 25% market decline without liquidation risk. The last 40% of EL is the survival bunker — no VIX level unlocks it.

### Rule 2 — Deployment Governor (updated in v9.0)

Max deployment capped at 60% regardless of VIX level:

| VIX | Min Retain | Max Deploy |
|-----|-----------|------------|
| <20 | 80% | 20% |
| 20-25 | 70% | 30% |
| 25-30 | 60% | 40% |
| 30-40 | 50% | 50% |
| 40+ | 40% | 60% |

---

*This document is the governing charter for AGT Equities. Review quarterly or after any material change in portfolio composition, tax status, or market regime.*
