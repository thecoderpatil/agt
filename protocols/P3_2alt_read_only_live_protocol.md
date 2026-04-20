# P3.2-alt — Read-Only Live Protocol

**Supersedes:** P3.2 paper run protocol (paper path deferred indefinitely)
**Precondition:** execution kill-switch shipped and green (commit 7c821ad)
**Goal:** validate all read paths, rule math, Cure Console rendering, and R5 reconciliation against the LIVE IBKR account with zero write capability. Three to five market days of observation before P3.3 (live 1-contract write).

---

## Pre-flight (Day 0, evening before)

**0.1 Kill-switch state verification**
- `AGT_EXECUTION_ENABLED` unset in `.env` (deploy-time default: disabled)
- `execution_state` DB row: `disabled=1`
- `_HALTED` will be False on boot, but env + DB both block — triple-gate holds
- Run: `python -c "from agt_equities.execution_gate import assert_execution_enabled; assert_execution_enabled()"` → expect ExecutionDisabledError

**0.2 Git hygiene**
- `git status` clean
- `git log --oneline -5` — confirm kill-switch commit at HEAD
- Tag current commit: `git tag p3.2alt-start`

**0.3 DB backup**
- `copy agt_desk.db agt_desk.db.p3.2alt.bak`
- Also backup `trade_ledger.db` if separate

**0.4 Live IBKR config**
- `.env`: `IB_HOST=127.0.0.1`, `IB_PORT=4001` (live TWS) or `IB_PORT=7496` (live Gateway)
- Confirm `PAPER_MODE=false`
- Confirm TWS is logged in with LIVE account credentials
- Confirm API → Enable ActiveX and Socket Clients is checked
- Confirm Read-Only API is **unchecked** (we need position/account data, not read-only IBKR mode)
- Note: "read-only" here means OUR kill-switch blocks writes, not IBKR's API setting

**0.5 Alert channel**
- Telegram bot online
- Send test message: `/ping` → expect response
- Confirm `_alert_telegram` destination chat is operator, not a group

---

## Day 1 — Cold boot + smoke

**1.1 Boot**
- `launcher\start_cure.bat` (or `python -m agt_deck.main` foreground for first boot)
- `python telegram_bot.py` in separate window, foreground
- Watch bot boot log: expect "Starting AGT Equities Bridge", schedulers registered, no tracebacks
- Expect kill-switch log line: `execution_gate: AGT_EXECUTION_ENABLED != true, execution BLOCKED`

**1.2 First connect to live IBKR**
- Bot connects to port 4001 on boot
- Expect log: account summary pulled, positions pulled, NAV computed
- **Verify on phone:** open Cure Console, check top strip NAV matches TWS account window exactly (to the cent per account)
- If mismatch > $1: STOP, investigate before proceeding

**1.3 Underwater Positions render check**
- Open `/cure` on phone
- Underwater Positions section should show your real underwater cycles grouped by household
- For each row, verify against TWS manually:
  - Ticker matches
  - Short strike matches
  - DTE matches
  - Unreal % is in the right ballpark (±5% tolerance for timing)
  - CC shield/warning icon matches your mental model of which positions are covered
- If any row is missing or wrong: log to session notes, do NOT fix yet — triage at end of day

**1.4 Rule engine smoke**
- In Telegram: `/rules` (or whatever the rule dump command is)
- Expect Rule 6, Rule 11, Rule 7 outputs with real numbers
- Verify Rule 6 margin math: compare `ExcessLiquidity` in output to TWS account window `Excess Liquidity` field — must match to within $100
- Verify Rule 11 glide path: eyeball the baseline/target/current for 2-3 cycles against your mental model

**1.5 Kill-switch live test**
- Attempt a transmit path: via Telegram, try to trigger a DEX callback or similar write action
- **Expect:** ExecutionDisabledError logged, Telegram alert fired, no IBKR order placed
- **Verify in TWS:** no orders appear in the Order ticket or Trade Log
- If ANY order reaches IBKR: IMMEDIATE HALT, full stop, debrief

**1.6 End of Day 1**
- Screenshot Cure Console on phone
- Note any anomalies in `session_notes/p3.2alt_day1.md`
- Leave bot and deck running overnight (or stop if you prefer — restart is idempotent)

---

## Days 2-5 — Observation loop

**Each morning at market open (~9:30 ET):**

**M.1** Open Cure Console on phone. Top-to-bottom triage exactly as you would in production:
- Red Alert banner
- Underwater Positions
- Lifecycle Queue
- Cure Actions
- Recent Orders

**M.2** For each item that WOULD prompt an action in production:
- Write down what you would do (e.g., "roll SPY 520C → 525C")
- Do NOT stage it through the bot. Stage manually in TWS as you would today.
- Note the time and decision in `session_notes/p3.2alt_day{N}.md`

**M.3** After your manual trade fills in TWS:
- Wait for R5 to pick it up (scheduled poller runs every 10s)
- Watch the bot log for the reconciliation line
- Verify:
  - Trade appears in `trade_ledger` with correct fields
  - `active_cycles` state advances correctly
  - Cure Console refreshes to show the new state
  - Underwater section updates
  - Rule 11 glide path recomputes
- **This is the core R5 validation.** Every manual trade you place during these 5 days is a test case for reconciliation.

**M.4** If anything mismatches:
- Screenshot the discrepancy
- Note the exact field + expected vs actual
- Do NOT patch live. Log to session notes, create a followup, fix between sessions.

**Each afternoon ~3:45 ET:**

**A.1** EOD sweep: verify NAV matches TWS close, verify all fills reconciled, verify no orphaned `pending_orders` rows
**A.2** Review session notes, triage anything that broke
**A.3** Decide: does tomorrow proceed, or do we fix something tonight?

---

## Exit criteria (after Day 5 or earlier if confident)

Green to advance to P3.3 only if ALL of these hold:

- [ ] NAV match to TWS within $1 across all accounts, every day, every session
- [ ] Every manual trade placed in TWS reconciled correctly by R5 within 30s
- [ ] `active_cycles` state advanced correctly for every trade
- [ ] Cure Console Underwater Positions matches TWS positions view exactly
- [ ] Rule 6 margin matches IBKR `ExcessLiquidity` within $100
- [ ] Rule 11 glide path recomputes correctly after every fill
- [ ] Zero `placeOrder` calls reached IBKR in 5 days (confirmed via TWS audit log)
- [ ] Kill-switch alerts fired correctly on every attempted transmit
- [ ] Zero bot crashes
- [ ] Zero deck crashes
- [ ] No schema drift, no followup growth > 2 items

If ANY criterion fails: do not advance. Triage, fix, restart the 5-day clock.

---

## Rollback

If something breaks catastrophically during P3.2-alt:

1. `/halt` in Telegram (redundant but defensive)
2. Stop bot (Ctrl+C)
3. Stop deck (Ctrl+C)
4. `git checkout p3.2alt-start` (reset to known-good commit)
5. `copy agt_desk.db.p3.2alt.bak agt_desk.db` (restore DB)
6. Debrief, write REFLECTION, plan fix sprint

---

## What P3.2-alt does NOT cover

- **First write path.** No placeOrder reaches IBKR. That's P3.3's job.
- **JIT chain pulls under real rate limits.** yfinance behavior during high-activity days is untested until you're clicking through Cure Console rapidly. You'll exercise some of this during morning triage but not heavily.
- **DEX callback happy path end-to-end.** DEX will be blocked by kill-switch. You validate everything up to the placeOrder gate, not past it.
- **Margin math under a new position.** You'll see Rule 6 against your current portfolio shape, but not against a shape the bot itself constructed.

P3.3 covers the first three. P3.4+ cover the rest.

---

## Operator notes

- Keep session notes per day. They become the P3.2-alt debrief document.
- Any followup discovered here gets logged in the followup ledger with `p3.2alt` tag.
- If Cure Console or rule engine shows something that doesn't match your mental model of the portfolio, TRUST TWS, not the bot. The bot is being validated.
- If TWS shows something that doesn't match your mental model, that's a different problem — stop and investigate manually before continuing.
- Do not trade larger or differently than you would normally during these 5 days. The point is observation of real, unmodified operator behavior.

---

## To advance to P3.3

After exit criteria met:
1. Write `REFLECTION_p3.2alt.md` — what worked, what broke, what changed
2. Draft `P3.3_live_one_contract_protocol.md` — based on learnings
3. Architect + operator review
4. Flip `AGT_EXECUTION_ENABLED=true` in `.env` (deploy-time enable)
5. UPDATE `execution_state` SET disabled=0 via admin SQL, or add a one-time migration
6. First P3.3 session

---

End of protocol.
