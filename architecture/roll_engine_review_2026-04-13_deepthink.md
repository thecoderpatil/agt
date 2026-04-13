This is a rigorous architectural review. Operating a deeply underwater, 1.5x leveraged wheel desk under Puerto Rico's Act 60 requires absolute mechanical precision. Because you have a 0% tax rate on gains but **zero ability to write off capital losses**, preventing assignment below your Adjusted Cost Basis (ACB) is a matter of structural survival. A realized loss is unrecoverable capital destruction. 

The architect has correctly identified a severe bleeding mechanism in the current defensive loop, but the proposed "Two-Engine" solution introduces new structural risks and logic landmines—especially regarding the fixes you shipped today and the May 2026 multi-tenant rollout. 

Here is my Lead Quant review of the codebase and the architect's proposal, answering your 9 questions directly.

---

### 1. Diagnosis of the 8 `STATE_3` Bugs
The architect's diagnosis is fiercely accurate on the math, but they misunderstand the nature of Bug #5 and missed a critical logic flaw in their own proposal.
*   **Bug 2 (`debit_paid <= 0: continue`):** This is a catastrophic logic inversion. In options combo pricing, a negative debit is a net credit. By skipping these, the engine is explicitly programmed to **refuse profitable rolls and only execute trades that cost margin**. This explains why the desk's premium generation choked in WARTIME.
*   **Bug 3 (EV Ratio $\ge$ 2.0):** Demanding $2.00 of strike improvement for every $1.00 of debit paid is an arbitrage fantasy in liquid, near-term options. It guarantees the defensive engine will freeze.
*   **The Bug the Architect Missed (The Horizontal Math Flaw):** The architect proposes relaxing the EV filter to `intrinsic_gained > debit_paid`. **This mathematically breaks horizontal rolls.** If you roll out in time at the *same* strike, the `intrinsic_gained` is exactly `$0.00`. If you pay a $0.10 debit for 30 days of time, the formula evaluates as `0.00 > 0.10` (`FALSE`). The engine will instantly reject its own horizontal fallback.
*   **Missed Bug 2 (Ex-Div Blindness):** Walking 14-45 DTE without checking `next_dividend_date`. Rolling an ITM defensive call across an ex-dividend date guarantees early assignment.

### 2. Two-Engine Split vs. Single State Machine
**I strongly reject the two-engine split.** 
Building two entirely separate execution loops (`ENGINE 1` and `ENGINE 2`) running asynchronously over the same portfolio creates race conditions, doubles the IBKR API chain-query payload, and creates threshold dead-zones. (e.g., If a call is at delta 0.33, Engine 1 ignores it because it's >0.30, and Engine 2 ignores it because it's <0.35. The position is orphaned).

**The Architecture Fix:** Build a **Single Unified Roll Evaluator**. The chain-walking and pricing math must be DRY (Don't Repeat Yourself). You simply pass a different **Constraints Matrix** into the evaluator based on the position's urgency state:
*   *Routine State:* `max_debit = 0.00`, `min_dte = 14`, `max_delta = 0.30`
*   *Defensive State:* `max_debit = intrinsic_gained * 0.95`, `min_dte = 7`, `max_delta = 0.50`

### 3. Trigger Thresholds (The Hidden Collision Risk)
**The proposed Fast Roll trigger (`delta in [0.15, 0.30]`) is a ticking time bomb.** 
Look at the Problem 1 fix you shipped today: `strike_floor = max(0, spot * 1.03)`. You are now writing CCs 3% Out-of-The-Money (OTM). Depending on IV and DTE, a 3% OTM call frequently has an inception delta of **0.20 to 0.35**. 
If you deploy Engine 1 as proposed, the moment your `/cc` engine fills a call, Engine 1 will wake up on the next cycle, see a Delta of 0.28, and **instantly attempt to roll the call you just wrote.** You will churn the portfolio into oblivion.

*   **The Fix:** Fast Roll cannot trigger purely on static Delta. It must trigger on profit realization or delta *expansion*. Trigger: `current_mark <= (original_premium * 0.50)` OR `current_delta >= (inception_delta + 0.10)`.

### 4. Credit vs. Debit Routing
**Unify the math; do not separate the pipelines.**
A roll is simply a calendar or diagonal spread. Do not hardcode separate logic paths. Calculate a single Expected Value/Net Capital Impact score for every node on the options chain. 
Let the Constraints Matrix handle it. If it's a Fast Roll, the engine simply applies `max_acceptable_debit = 0.00`. If it's a Defensive Roll, `max_acceptable_debit = horizontal_allowance OR (intrinsic_gained * 0.95)`.

### 5. Cooldown Mechanism (Time-locks vs. State-locks)
**Veto the 24h/4h time-based cooldowns immediately.**
Time-locks are a lethal anti-pattern in automated volatility trading. If Engine 2 stages an order that doesn't fill, goes to sleep for 4 hours, and then a macro headline hits causing a 4% market surge, your bot will passively watch the position blow through the strike and into assignment while "on cooldown."

*   **The Fix:** Use **State-Based Cooldowns**. 
    Check the staging queue: `if has_pending_roll_order(conId): continue`. 
    Cache the spot price of the last calculation. Only suppress the loop if the market hasn't moved: `if abs(current_spot - last_eval_spot) / last_eval_spot < 0.015: skip_scan`. React to price movement, not the clock.

### 6. BAG Order vs. Sequential BTC+STO Fallback
**Under absolutely NO circumstances should you use sequential legging.**
The architect views BAG-only as a bug; it is actually a vital safety feature. If you attempt a sequential roll on a portfolio with $123K margin debt:
1. The bot executes the Buy-To-Close (BTC) leg first, realizing a cash debit.
2. The portfolio is now synthetically unhedged (naked long stock at 1.5x leverage).
3. If excess liquidity drops below IBKR's real-time maintenance threshold, **IBKR will hard-reject your Sell-To-Open (STO) leg**. 
4. You are left holding unhedged equity, having just drained cash. 

**Strictly enforce IBKR BAG (Combo) orders.** They guarantee atomic margin netting. If a combo doesn't fill, the fallback is algorithmically walking the limit price to cross the bid/ask spread, never breaking the legs.

### 7. Alerting (Silent Failures)
Writing a Defensive Roll failure to a daily digest in WARTIME is a dereliction of duty. If the engine scans 45 DTE forward and cannot find a viable roll for an ITM call, you are facing imminent, non-recoverable capital destruction. 
*   **The Fix:** This must trigger a separate `CRITICAL_PAGER` function that pushes to a dedicated Telegram channel with `disable_notification=False`. It must audibly wake the operator to force manual intervention (e.g., shorting shares to delta-hedge or rolling 180+ DTE).

### 8. Testing Approach
**Mocks will give you 100% false confidence here.** 
Options combo routing is incredibly idiosyncratic. Mock endpoints will gladly accept a mock JSON combo order priced at `$0.12`, but IBKR's live risk engine will instantly reject it because options on that specific ticker require `$0.05` tick increments on the Complex Order Book (COB). 
*   **The Fix:** You must test this against the IBKR Paper Trading port (7497). Write a script to synthetically reconstruct the exact $123K margin load and underwater basis. Force the spot price feeds to trigger the logic. Verify the exact `BAG` payload formats correctly in the paper matching engine.

### 9. Multi-Tenant Scalability (The Strategic Blind Spot)
The architect notes: *"Whatever ships needs to scale to a multi-tenant household model."*
Currently, `_scan_and_stage_defensive_rolls` lives in `telegram_bot.py` and reads global WARTIME states. **This will crash your system in May.** 
If you onboard a second client, `adjusted_basis` and `margin_debt` cannot be queried globally. Client A might be at 1.5x leverage (WARTIME) with an ADBE basis of $400, while Client B is flush with cash (PEACETIME) with a basis of $520. 
*   **The Architecture Fix:** Before writing the new engine, decouple the Roll Logic entirely from the UI/Bot layer. Create `agt_equities/roll_engine.py`. The engine must accept a `PortfolioContext` object containing `account_id`, `wartime_flag`, `available_margin`, and `acb_dict`. Every constraint must be evaluated *per account*.

### Final Executive Directive
Approve the conceptual separation of Proactive vs. Defensive routing, but demand a single unified state machine. Force the architect to patch the Fast Roll trigger immediately so it doesn't instantly roll the 3% OTM calls you fixed today. Kill the time-based cooldowns, absolutely ban sequential legging, fix the horizontal EV math bug, and allocate 10 hours for proper IBKR Paper testing and context isolation.
