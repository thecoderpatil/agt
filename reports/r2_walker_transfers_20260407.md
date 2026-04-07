# R2: Walker Transfer Ingestion — Investigation Report

Generated: 2026-04-07
Status: REPORT ONLY — awaiting Yash review before walker.py edit

## (a) master_log_transfers schema

36 columns including: transaction_id (PK), account_id, date, type, direction, symbol, quantity, asset_category, conid, description, transfer_price, position_amount, pnl_amount, code. Full schema in schema.py.

## (b) Transfer rows in live DB

**35 rows total.** Two types:

| Type | Count | Direction | Description |
|------|-------|-----------|-------------|
| ACATS | 31 | 30 IN, 1 OUT | Fidelity → IBKR transfers (Sep 2025) |
| INTERNAL | 4 | 2 OUT, 1 IN, 1 cash | Cross-account transfers within IBKR |

### The ADBE transfer (smoking gun)

```
U22076184  2025-10-10  INTERNAL  OUT  ADBE 251010P00332500  qty=1  OPT  conid=818664987
```

This is the `ADBE 332.5P 20251010` that was sold to open in U22076184 (Trad IRA) and then internally transferred OUT to another account. There is **no matching IN row** in the transfers table — IBKR may report the receiving side differently, or the IN leg may appear as a trade row in master_log_trades for the receiving account.

### Other INTERNAL transfers

```
U22076184  2025-10-10  INTERNAL  OUT  QCOM 251017P00160000  qty=1  OPT  conid=763106348
U22076184  2025-10-10  INTERNAL  OUT  --                    qty=0  CASH  (cash sweep)
U21971297  2025-10-25  INTERNAL  IN   --                    qty=0  CASH  (cash receipt)
```

Two option positions were transferred out of U22076184 on the same day: ADBE 332.5P and QCOM 160P.

## (c) Walker EventType enum

**No TRANSFER event type exists.** The 16 event types are: CSP_OPEN, CSP_CLOSE, CC_OPEN, CC_CLOSE, LONG_OPT_OPEN, LONG_OPT_CLOSE, STK_BUY_DIRECT, STK_SELL_DIRECT, ASSIGN_STK_LEG, ASSIGN_OPT_LEG, EXPIRE_WORTHLESS, EXERCISE_STK_LEG, EXERCISE_OPT_LEG, CORP_ACTION, CARRYIN_STK, CARRYIN_OPT.

## (d) trade_repo._load_trade_events()

Reads ONLY from `master_log_trades`. Does NOT query `master_log_transfers`. Transfer events are completely invisible to the Walker.

## (e) Event types for cross-account movement

| IBKR Type | In Live DB | Count |
|-----------|-----------|-------|
| ACATS | Yes | 31 rows (Fidelity → IBKR, Sep 2025) |
| INTERNAL | Yes | 4 rows (cross-account within IBKR) |
| Journal Entry | No | — |
| Position Transfer | No | — |

## (f) Proposed Walker event type additions

```python
class EventType(Enum):
    ...
    TRANSFER_IN  = 'transfer_in'   # position received from another account
    TRANSFER_OUT = 'transfer_out'  # position sent to another account
```

## (g) trade_repo loader changes

Add `_load_transfer_events()` that reads from `master_log_transfers` and converts rows to `TradeEvent` objects:

- `source = 'FLEX_TRANSFER'`
- `transaction_type = 'Transfer'`
- ACATS IN → `CARRYIN_STK` or `CARRYIN_OPT` (already handled — this is what inception_carryin.csv rows model)
- INTERNAL OUT → new `TRANSFER_OUT` event
- INTERNAL IN → new `TRANSFER_IN` event

## (h) Cycle event handling

**Intra-household transfer (INTERNAL, both accounts in same household):**

The transfer is a non-economic move. For the Walker:
- `TRANSFER_OUT`: decrement the sending account's position (similar to stock sell for STK, or option close for OPT) with NO P&L impact
- `TRANSFER_IN`: increment the receiving account's position (similar to carry-in) with NO P&L impact
- Net effect on the household cycle: zero — shares/options move from one account to another within the same cycle

Since the Walker groups by `(household, ticker)`, both accounts are in the same cycle. The transfer moves position between per-account sub-state but doesn't change the household total.

**Cross-household transfer:** None exist in the live DB. Flag for Yash decision if encountered — would require closing a cycle in one household and opening in another.

## (i) Worked example: ADBE 332.5P transfer

**Before fix (current state):**
- U22076184 sold ADBE 332.5P on 20251007 (ExchTrade, STO, net_cash=$109.95)
- U22076184 transferred the option OUT on 20251010 (INTERNAL, invisible to Walker)
- U22076329 received the option and it expired on 20251010 (BookTrade Ep, fifo_pnl_realized=$0)
- IBKR FIFO perf summary: attributes $109.95 realized to U22076329 (the receiving account)
- Walker: sees the STO in U22076184 (premium $109.95) and the Ep in U22076329 (realized $0)
- The STO increments `open_short_puts` in U22076184, but the Ep decrements in U22076329
- Net: open_short_puts may go negative in U22076329 (it tries to close a put it never opened)

**After fix:**
- Walker ingests the TRANSFER_OUT from U22076184 on 20251010: decrements U22076184's `open_short_puts` by 1 (the put left this account)
- Walker ingests the TRANSFER_IN to U22076329 on 20251010: increments U22076329's `open_short_puts` by 1 (the put arrived)
- The Ep on 20251010 in U22076329 now has a matching open put to close — osp correctly goes to 0
- The $109.95 premium from the STO in U22076184 stays in the cycle's `premium_total` (correct — the premium was earned, the option just moved accounts)
- Cross-check A: Walker's realized for ADBE Yash would now be $3,837.75 (matching IBKR), because the transfer properly routes the option's lifecycle across accounts

**Expected reconciliation impact:**
- A: ADBE Yash $3,727.80 → $3,837.75 (delta eliminated, 49/49)
- C: U22076329 $109.95 residual → $0 (3/4)

## Implementation estimate

- Walker: +2 event types, +transfer handling in `_apply_event` (~30 lines)
- trade_repo: +`_load_transfer_events()` (~40 lines)
- Tests: +2 (intra-household transfer, ACATS transfer via transfers table)
- Total: ~80 lines

## Production DB: READ-ONLY (no changes during investigation)
