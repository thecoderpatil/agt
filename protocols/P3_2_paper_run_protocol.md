> **SUPERSEDED** — 2026-04-09. Paper run path deferred indefinitely due to IBKR paper provisioning blocker and the decision to pivot to read-only-live validation. See `P3_2alt_read_only_live_protocol.md`. This file is retained for historical record only.
>

# P3.2 — Paper Run Protocol

**Purpose:** End-to-end money path validation on IBKR paper account. Manual Dynamic Exit, Yash household, 1 contract SPY covered call.

**Prerequisites:**
- Sprint C (state_builder SSOT) complete
- Sprint D (rule_engine hardcode purge) complete
- Cure Console polish complete
- Paper account provisioned by IBKR
- All 3 DEX pre-flight blockers resolved

---

## Pre-flight surveys (fire before Step 2)

1. **DEX staging UI path survey** — Coder verifies current entry point in Cure, F20 routing against paper DU* accounts, Smart Friction modal wiring. ~1h, report-only. Any blocker found → STOP P3.2, fix first.
2. **Operator confirms SPY quotes streaming in TWS paper** (Step 0.5).

---

## Step 0 — Environment setup

1. Set `.env`:
   ```
   AGT_PAPER_MODE=1
   AGT_PAPER_ACCOUNTS=DU<yash_paper>:Yash_Household,DU<vikram_paper>:Vikram_Household
   AGT_TRUST_TIER=T0
   ```
   Verify `AGT_TRUST_TIER=T0` (10s cooldown — safest tier for first run).

2. Verify config reads correctly:
   ```bash
   python -c "from agt_equities.config import PAPER_MODE, HOUSEHOLD_MAP, MARGIN_ACCOUNTS; print(f'PAPER={PAPER_MODE}, HH={HOUSEHOLD_MAP}, MARGIN={MARGIN_ACCOUNTS}')"
   ```
   Expected: `PAPER=True`, household map contains DU* accounts, MARGIN_ACCOUNTS = all DU* accounts.

3. Start IBKR Gateway on port 4002 (paper primary). Fallback: 7497 if 4002 unreachable.

4. Verify chosen ticker (SPY recommended — guaranteed paper data + in wheel universe) quotes streaming in TWS paper before bot startup. If data subscription pending, wait up to 24h per IBKR policy.

5. Start bot: `boot_desk.bat`

6. Telegram: `/status` — verify `[PAPER]` prefix on response.

7. Telegram: `/halt` — cancel all scheduled jq jobs (CC scan, watchdog, etc.) to isolate P3.2 from background activity. Bot stays alive, IB connection preserved.

---

## Step 1 — Sanity checks

1. Open Cure Console at `http://127.0.0.1:8787/cure?t=<token>`.
2. Verify blue `PAPER TRADING` banner visible at top.
3. Verify Health Strip loads (may show zero EL — expected with fresh paper account).
4. Verify Lifecycle Queue section renders (empty is expected).
5. Telegram: `/orders` — should return empty or `[PAPER] No recent orders`.

---

## Step 1.5 — Gate block verification (optional but recommended)

1. Stage a dummy BTC with notional > Yash household freed cash (e.g., oversize limit price).
2. Attempt TRANSMIT.
3. Expect: `_pre_trade_gates` blocks with clear reason string in Telegram edit.
4. Expected log: `_pre_trade_gates BLOCK: site=dex reason=<notional_exceeds_freed>`
5. CANCEL the dummy row before proceeding to Step 2.

**Purpose:** Proves the safety net fires before committing the real run. Skip only if time-constrained.

---

## Step 2 — Manual staging (1 SPY CC)

1. In Cure Console, locate SPY in the household positions or use the staging entry point.
2. Stage a **1-contract SPY covered call** via the Cure Console UI:
   - Action type: CC (covered call)
   - Ticker: SPY
   - Household: Yash_Household
   - Contracts: 1
   - Strike + expiry: nearest weekly, OTM by at least 1-2 strikes.
3. Verify Telegram alert fires within the configured `_staged_alert_buffer` coalescing window (default 60s — verify `STAGED_COALESCE_WINDOW` at telegram_bot.py:116 before run).
4. Verify `bucket3_dynamic_exit_log` row created:
   ```sql
   SELECT audit_id, ticker, household, contracts, final_status, originating_account_id
   FROM bucket3_dynamic_exit_log
   WHERE final_status = 'STAGED'
   ORDER BY staged_ts DESC LIMIT 1;
   ```
   Expected: 1 row, SPY, Yash_Household, 1 contract, `originating_account_id` = DU* paper account.

---

## Step 3 — Attestation (Smart Friction)

1. In Cure Console Lifecycle Queue, click the STAGED row to open Smart Friction modal.
2. Complete attestation:
   - Standard flow: check all checkboxes, enter operator thesis.
   - Click ATTEST.
3. Verify row transitions to ATTESTED:
   ```sql
   SELECT audit_id, final_status, operator_thesis
   FROM bucket3_dynamic_exit_log
   WHERE audit_id = '<audit_id from Step 2>'
   ```
4. Verify Lifecycle Queue in Cure Console shows ATTESTED state.

---

## Step 4 — Transmit with cooldown

1. In Cure Console Lifecycle Queue, click TRANSMIT on the ATTESTED row.
2. Trust-tier cooldown fires: T0 = 10s. Bot edits Telegram message with countdown.
3. After cooldown, JIT 9-step chain executes:
   - Pre-trade gates (halt, mode, notional, non-wheel, F20)
   - Option chain fetch
   - Price validation
   - `placeOrder` to IBKR paper
4. Verify row transitions to TRANSMITTING → TRANSMITTED:
   ```sql
   SELECT audit_id, final_status, ib_order_id, transmitted_ts
   FROM bucket3_dynamic_exit_log
   WHERE audit_id = '<audit_id>'
   ```
5. Verify Telegram shows `[PAPER] Order transmitted` with order details.

---

## Step 5 — Fill reconciliation

1. Wait for paper fill (may be instant or take minutes depending on IBKR paper sim latency).
2. Check IBKR order status in TWS or via Telegram `/orders`.
3. Verify `bucket3_dynamic_exit_log` row has fill data:
   ```sql
   SELECT audit_id, final_status, fill_price, fill_qty, fill_ts, commission
   FROM bucket3_dynamic_exit_log
   WHERE audit_id = '<audit_id>'
   ```
4. If fill does not arrive within 5 minutes: check TWS paper order status manually. Paper fills can be delayed.

---

## Step 6 — Audit trail verification

1. Verify full lifecycle in `bucket3_dynamic_exit_log`:
   ```sql
   SELECT audit_id, final_status, staged_ts, transmitted_ts, fill_ts,
          originating_account_id, ib_order_id
   FROM bucket3_dynamic_exit_log
   WHERE audit_id = '<audit_id>'
   ```
   Expected: TRANSMITTED status, all timestamps populated, DU* account ID, non-null ib_order_id.

2. Verify Walker picks up the new position (next flex sync, or manual):
   ```bash
   python -c "from agt_equities import trade_repo; print([c.ticker for c in trade_repo.get_active_cycles()])"
   ```

3. Check Cure Console shows updated position state.

---

## Step 7 — Teardown

1. Close the SPY position in TWS paper if needed (or let it expire).
2. Verify no orphaned TRANSMITTING/ATTESTED rows:
   ```sql
   SELECT COUNT(*) FROM bucket3_dynamic_exit_log
   WHERE final_status IN ('TRANSMITTING', 'ATTESTED');
   ```
   Expected: 0.
3. Stop bot: `Ctrl+C` (clean shutdown with post_shutdown).
4. Restart bot to re-enable scheduled jobs cancelled in Step 0.7 (no `/resume` command — `/halt` requires restart to clear).
5. Optionally: set `AGT_PAPER_MODE=0` in `.env` to return to live mode.

---

## Abort procedure

At any point if something unexpected occurs:

1. **Do NOT panic.** Paper orders have no real financial impact.
2. Telegram: `/halt` — blocks all further trade gates.
3. If a TRANSMITTING row is stuck: use `/recover_transmitting <audit_id> abandoned` after verifying no fill in TWS.
4. If IBKR Gateway disconnects: reconnect in TWS, bot auto-reconnects within 30s.
5. Log the issue, screenshot the state, report to Architect.

---

## Success criteria

All must be TRUE for P3.2 to pass:

- [ ] Bot started in paper mode with `[PAPER]` prefix confirmed
- [ ] Cure Console rendered with blue PAPER banner
- [ ] SPY CC staged successfully (STAGED row in DB with DU* account)
- [ ] Smart Friction attestation completed (ATTESTED row)
- [ ] 10s cooldown observed (T0 trust tier)
- [ ] Pre-trade gates passed (all 5 gates)
- [ ] Order transmitted to IBKR paper (TRANSMITTED row with ib_order_id)
- [ ] Fill received and recorded (fill_price, fill_qty populated)
- [ ] No orphaned TRANSMITTING/ATTESTED rows at teardown
- [ ] Telegram message flow complete (staged alert → transmit confirmation → fill notification)
- [ ] No errors in bot logs during run (`grep ERROR logs/*.log`)

---

## Known limitations (paper mode)

- Paper market data is delayed 15min (reqMarketDataType(4)). Option chain bid/ask may be stale.
- Paper fills are simulated — timing and fill prices may not match live behavior.
- Walker requires flex sync to pick up paper trades (master_log_trades populated by flex_sync.py).
- EL snapshots may show zero until IBKR paper account has positions.
- Nickel/dime rounding active per Sprint 1C: OPT prices rounded to nickel (≤$3) or dime (>$3).

---

*Protocol version: 1.0 | Anchor: `ad8dc6f` | Tests: 608/608*
