# F20 — Sub-account Routing — Survey Report

**Date:** 2026-04-08
**Sprint:** F20
**Status:** Survey complete. Awaiting Architect review.

---

## Survey 1.1A — _stage_dynamic_exit_candidate position dict

**Function:** `telegram_bot.py:8257`

**Signature:**
```python
async def _stage_dynamic_exit_candidate(
    ticker: str,
    hh_name: str,
    hh_data: dict,
    position: dict,
    source: str,
) -> dict:
```

**Position dict shape** (assembled at `telegram_bot.py:9079-9098` in `_discover_positions`):
```python
position_rec = {
    "household": hh,
    "ticker": tkr,
    "sector": rec["sector"],
    "total_shares": total_shares,          # AGGREGATED across all accounts
    "avg_cost_ibkr": round(rec["avg_cost_ibkr"], 2),
    "initial_basis": round(initial_basis, 2),
    "total_premium_collected": round(total_prem, 2),
    "adjusted_basis": round(adj_basis, 2),
    "spot_price": spot,
    "market_value": round(total_shares * spot, 2),
    "mode": mode,
    "existing_short_calls": rec["short_calls"],
    "existing_short_puts": rec.get("short_puts", []),
    "covered_contracts": covered_contracts,
    "uncovered_shares": uncov_shares,
    "available_contracts": uncov_shares // 100,
    "accounts_with_shares": rec["accounts_with_shares"],    # <-- KEY FIELD
    "working_per_account": working_per_account,
    "staged_per_account": staged_per_account,
}
```

**Critical finding:** `accounts_with_shares` at line 9096 already carries per-account share breakdown. Built at lines 8882-8895:
```python
rec["accounts_with_shares"] = {}
# ... later, for each STK position:
acct_entry = rec["accounts_with_shares"].setdefault(acct, {
    "account_id": acct,
    "label": ACCOUNT_LABELS.get(acct, acct),
    "shares": 0,
})
acct_entry["shares"] += qty
```

**However:** `_stage_dynamic_exit_candidate` at line 8280 reads `shares = position["total_shares"]` — the AGGREGATED value. It never reads `position["accounts_with_shares"]`. **Account provenance is available but unused at staging time.**

**Callers:**
| Line | Caller | Source |
|------|--------|--------|
| 8549 | `cmd_dynamic_exit` | `manual_inspection` |
| 9813 | `/cc` overweight carveout | `cc_overweight` |
| 10896 | `_scheduled_watchdog` | `scheduled_watchdog` |

All three pass the same `position` dict from `_discover_positions`, which includes `accounts_with_shares`.

---

## Survey 1.1B — placeOrder account parameter resolution

**TRANSMIT path:** `handle_dex_callback` at `telegram_bot.py:6971-6977`

```python
# Resolve account: first margin account in household
hh_accounts = HOUSEHOLD_MAP.get(row["household"], [])
account_id = hh_accounts[0] if hh_accounts else ""
order = _build_adaptive_sell_order(qty, row['limit_price'], account_id)
order.orderRef = audit_id  # Followup #17: cryptographic 1:1 link

trade = ib_conn.placeOrder(contract, order)
```

**This is the bug.** `HOUSEHOLD_MAP["Yash_Household"][0]` = `"U21971297"` (Individual). Always. Regardless of which account the position actually resides in.

**`_build_adaptive_sell_order`** at line 7071:
```python
def _build_adaptive_sell_order(qty, limit_price, account_id) -> ib_async.Order:
    order = ib_async.Order()
    order.action = "SELL"
    order.account = account_id    # <-- This is where the wrong account lands
    ...
```

**Other placeOrder calls:**
| Line | Context | Account resolution |
|------|---------|-------------------|
| 4672 | `_place_single_order` (CSP/CC via /approve) | `target_trade.order` — account set by original order object |
| 6977 | Dynamic exit TRANSMIT | `HOUSEHOLD_MAP[household][0]` — **BUG** |
| 7198 | `_place_single_order` (pending_orders path) | Uses payload `account_id` field |

Only line 6977 is affected. The others use account_id from the order/payload directly.

---

## Survey 1.1C — Household-to-account mapping locations

**`HOUSEHOLD_MAP`** at `telegram_bot.py:80-84`:
```python
HOUSEHOLD_MAP = {
    "Yash_Household": ["U21971297", "U22076329", "U22076184"],
    "Vikram_Household": ["U22388499"],
}
```

**`ACCOUNT_TO_HOUSEHOLD`** at `telegram_bot.py:85-89` (reverse map):
```python
ACCOUNT_TO_HOUSEHOLD = {
    account_id: household_id
    for household_id, account_ids in HOUSEHOLD_MAP.items()
    for account_id in account_ids
}
```

**`ACCOUNT_LABELS`** at `telegram_bot.py:1775-1779`:
```python
ACCOUNT_LABELS = {
    "U21971297": "Individual",
    "U22076329": "Roth IRA",
    "U22388499": "Vikram",
}
```

**`MARGIN_ACCOUNTS`** at `telegram_bot.py:1786`: `{"U21971297", "U22388499"}` — excludes IRAs.

**Usage of `HOUSEHOLD_MAP[household][0]` (the bug pattern):** Only at line 6972 (TRANSMIT path).

---

## Survey 1.1D — INSERT into bucket3_dynamic_exit_log

**CC path** (`_stage_dynamic_exit_candidate` at lines 8453-8484):
```python
conn.execute(
    "INSERT INTO bucket3_dynamic_exit_log "
    "(audit_id, trade_date, ticker, household, desk_mode, "
    " action_type, household_nlv, underlying_spot_at_render, "
    " gate1_freed_margin, gate1_realized_loss, "
    " gate1_conviction_tier, gate1_conviction_modifier, "
    " gate1_ratio, gate2_target_contracts, "
    " walk_away_pnl_per_share, strike, expiry, "
    " contracts, shares, limit_price, "
    " render_ts, staged_ts, final_status, source) "
    "VALUES (?, date('now'), ?, ?, ?, "
    " 'CC', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'STAGED', ?)",
    (audit_id, ticker, hh_name, desk_mode, ...)
)
```

**No `originating_account_id` in the column list.** Only `household` (= `hh_name`, e.g., `"Yash_Household"`).

**STK_SELL path** (`rule_engine.py:710-722`):
```python
conn.execute(
    "INSERT INTO bucket3_dynamic_exit_log "
    "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
    " household_nlv, underlying_spot_at_render, "
    " gate1_realized_loss, walk_away_pnl_per_share, "
    " shares, limit_price, exception_type, final_status) "
    "VALUES (?, date('now'), ?, ?, ?, 'STK_SELL', ?, ?, ?, ?, ?, ?, ?, 'STAGED')",
    (audit_id, ticker, household, desk_mode, ...)
)
```

**Also no `originating_account_id`.** Also only `household`.

---

## Survey 1.1E — Schema definition + migrations

**CREATE TABLE** at `schema.py:705-752`:
```sql
CREATE TABLE IF NOT EXISTS bucket3_dynamic_exit_log (
    audit_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    household TEXT NOT NULL,
    desk_mode TEXT NOT NULL CHECK (...),
    action_type TEXT NOT NULL CHECK (action_type IN ('CC', 'STK_SELL')),
    household_nlv REAL NOT NULL,
    underlying_spot_at_render REAL NOT NULL,
    gate1_freed_margin REAL,
    gate1_realized_loss REAL,
    gate1_conviction_tier TEXT,
    gate1_conviction_modifier REAL,
    gate1_ratio REAL,
    gate2_target_contracts INTEGER,
    gate2_max_per_cycle INTEGER,
    walk_away_pnl_per_share REAL,
    strike REAL,
    expiry TEXT,
    contracts INTEGER,
    shares INTEGER,
    limit_price REAL,
    campaign_id TEXT,
    operator_thesis TEXT,
    attestation_value_typed TEXT,
    checkbox_state_json TEXT,
    render_ts REAL,
    staged_ts REAL,
    transmitted INTEGER NOT NULL DEFAULT 0,
    transmitted_ts REAL,
    re_validation_count INTEGER NOT NULL DEFAULT 0,
    final_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (final_status IN ('PENDING', 'STAGED', 'ATTESTED',
                                'TRANSMITTING', 'TRANSMITTED',
                                'CANCELLED', 'DRIFT_BLOCKED', 'ABANDONED')),
    source TEXT NOT NULL DEFAULT 'scheduled_watchdog' CHECK (...),
    exception_type TEXT CHECK (...),
    fill_ts REAL,
    fill_price REAL,
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (campaign_id) REFERENCES ...
) WITHOUT ROWID
```

**Existing migrations** (schema.py):
- Line 616: `_migrate_dyn_exit_add_transmitting` (Beta Impl 3)
- Line 620-625: `ALTER TABLE ... ADD COLUMN exception_type TEXT` (Beta Impl 5)
- Lines 628-637: Followup #17 columns: `ib_order_id INTEGER`, `ib_perm_id INTEGER`, `fill_qty INTEGER`, `commission REAL`

**No `originating_account_id` column exists.** Confirmed by DB query (column does not exist).

---

## Survey 1.2F — Account ID availability at staging time

**Data flow:**

```
ib.reqPositionsAsync()
  → [Position(account='U21971297', contract=Stock(UBER), position=300, avgCost=45.2),
     Position(account='U22076329', contract=Stock(UBER), position=200, avgCost=43.1)]
  → _discover_positions() groups by household+ticker
  → raw[key]["accounts_with_shares"] = {
        "U21971297": {"account_id": "U21971297", "label": "Individual", "shares": 300},
        "U22076329": {"account_id": "U22076329", "label": "Roth IRA", "shares": 200},
    }
  → position_rec["accounts_with_shares"] = rec["accounts_with_shares"]
  → position_rec["total_shares"] = 500  (aggregated)
  → _stage_dynamic_exit_candidate(ticker, hh_name, hh_data, position_rec, source)
```

**Answer: YES, `accounts_with_shares` is available at staging time.** The per-account breakdown is in `position["accounts_with_shares"]`, but `_stage_dynamic_exit_candidate` only reads `position["total_shares"]` (the aggregate).

**For the STK_SELL path** (`rule_engine.py:stage_stock_sale_via_smart_friction`): The function receives `household` and `shares` as scalars from the Cure Console form. **No position dict is passed. No `accounts_with_shares` available.** The Cure Console form at `POST /api/cure/r5_sell/stage` passes: `ticker, household, shares, limit_price, adjusted_cost_basis, exception_type, desk_mode, household_nlv, spot`. **No account_id field.**

This means F20 needs to add account_id to BOTH staging paths:
1. CC path: extract from `position["accounts_with_shares"]` (already available)
2. STK_SELL path: add `account_id` to the Cure Console form data flow

---

## Survey 1.2G — Multi-account position aggregation logic

**Aggregation at lines 8856-8895:**
```python
raw: dict[str, dict[str, dict]] = {}  # household -> ticker -> accumulator
for pos in positions:
    acct = pos.account
    hh = ACCOUNT_TO_HOUSEHOLD[acct]
    key = f"{hh}|{root}"
    if key not in raw:
        raw[key] = {
            "stk_shares": 0,
            "accounts_with_shares": {},  # per-account breakdown preserved
        }
    rec = raw[key]
    if c.secType == "STK" and pos.position > 0:
        qty = int(pos.position)
        rec["stk_shares"] += qty          # AGGREGATE across accounts
        acct_entry = rec["accounts_with_shares"].setdefault(acct, {...})
        acct_entry["shares"] += qty       # PER-ACCOUNT preserved
```

**Key insight:** Positions ARE aggregated across accounts within a household for the `total_shares` value. But per-account breakdown is preserved in `accounts_with_shares`. This means at staging time we can determine which account(s) hold the shares.

**Multi-account excess scenario:**
If Yash holds UBER: 300sh in Individual + 200sh in Roth = 500sh total.
Overweight scope computes `excess_contracts` from `total_shares` (500) vs household NLV.
Say `excess_contracts = 3` (300 excess shares).

The question: which account's shares are "excess"? This depends on which account's shares are encumbered by existing CCs.

**Encumbrance tracking already exists per-account:**
- `short_calls` entries at line 8904 include `"account": acct`
- `working_per_account` at line 8815 tracks per `f"{acct}|{root}"`
- `staged_per_account` at line 8850 tracks per `f"{p.get('account_id', '')}|{p_ticker}"`

So the infrastructure to determine which account has unencumbered (excess) shares already exists. The logic to calculate per-account excess does NOT exist yet — it would need to be built.

---

## Survey 1.3H — Existing row counts by status

```sql
SELECT COUNT(*), final_status FROM bucket3_dynamic_exit_log GROUP BY final_status;
```

| Status | Count |
|--------|-------|
| ABANDONED | 1 |

**Total:** 1 row. Status = ABANDONED (the cleaned-up smoke test row from CLEANUP-1).

**No STAGED, ATTESTED, TRANSMITTED, or TRANSMITTING rows exist.**

---

## Survey 1.3I — Legacy rows needing backfill

**Zero rows in any active state.** The only row is ABANDONED. No backfill needed for live routing — only the ABANDONED row would get a NULL `originating_account_id`, which is harmless (it will never be transmitted).

---

## Option X vs Option Y Analysis

**Spec asks:** If excess shares span multiple accounts, should we stage one row per account (Option X) or one row with a JSON allocation column (Option Y)?

**Recommendation: Option X (separate rows per account).**

**Evidence:**
1. The entire TRANSMIT path (`handle_dex_callback`) operates on a single `bucket3_dynamic_exit_log` row. One row = one `placeOrder` call. No splitting logic exists and adding it would be high-risk.
2. The orphan scan, `/recover_transmitting`, R5 fill handlers, sweeper, and CAS guards all assume 1 row = 1 order. Option Y would break this invariant.
3. The current per-account encumbrance tracking (`working_per_account`, `staged_per_account`, `accounts_with_shares`) provides the data needed to compute per-account excess at staging time.
4. Option X requires more staging logic but zero TRANSMIT-path changes. Option Y requires both staging AND TRANSMIT-path changes.

**Caveat:** Option X requires `_stage_dynamic_exit_candidate` to loop over accounts when excess spans multiple accounts. Each iteration stages a separate row with its own `audit_id` and `originating_account_id`. The Gate 1 evaluation per row uses the per-account excess (not total excess). This is more staging-time complexity but atomically correct at TRANSMIT.

**Edge case:** If excess_contracts = 3 and Individual has 2 uncovered lots while Roth has 1 uncovered lot, Option X stages 2 rows: one for 2c from Individual, one for 1c from Roth. Each row has its own `originating_account_id`.

**Simplification for v1 (Architect call):** If multi-account excess is rare (it requires the same ticker held in multiple accounts within a household AND overweight AND partially encumbered), we could defer multi-account splitting to a followup and stage the entire excess from the account with the most unencumbered shares. This gets the common case right (single-account positions) while flagging the edge case for manual review.

---

## Deviations from Spec

### D1: Two staging paths, not one
The spec assumes `_stage_dynamic_exit_candidate` is the only staging function. In reality there are TWO:
- **CC path:** `_stage_dynamic_exit_candidate()` in `telegram_bot.py:8257` — has `position["accounts_with_shares"]` available
- **STK_SELL path:** `stage_stock_sale_via_smart_friction()` in `rule_engine.py:653` — receives scalars from Cure Console form, NO position dict

Both paths write to `bucket3_dynamic_exit_log`. Both need `originating_account_id`. The STK_SELL path requires adding `account_id` to the Cure Console form → POST handler → `stage_stock_sale_via_smart_friction()` parameter list.

### D2: No legacy rows to backfill
The spec anticipates legacy STAGED/ATTESTED/TRANSMITTED rows needing backfill. Production DB has only 1 ABANDONED row. F20-2 (backfill) becomes a no-op — the migration just adds the nullable column.

### D3: HOUSEHOLD_MAP order is the bug vector
`HOUSEHOLD_MAP["Yash_Household"][0]` = `"U21971297"` (Individual). This is a list — the ordering is implicit and fragile. The fix removes dependence on list ordering entirely by using the row's `originating_account_id` instead.

### D4: STK_SELL Cure Console form needs account_id
The Cure Console dynamic exit panel (`cure_dynamic_exit_panel.html`) and Smart Friction modal (`cure_smart_friction.html`) currently render per-position data. They would need to include the originating account_id in a hidden field or select element. This is a frontend change not in the original spec scope.

### D5: `_compute_overweight_scope` uses total shares
`_compute_overweight_scope()` at line 8296 takes `shares` (total across all accounts). It computes excess at the household level, not per-account. For Option X with per-account rows, we'd need to compute per-account excess. This requires either modifying `_compute_overweight_scope` or post-processing its output with the per-account breakdown.

---

## Summary

| Finding | Impact |
|---------|--------|
| `accounts_with_shares` already available at CC staging time | CC path fix is straightforward |
| STK_SELL path has no position dict — scalars only from form | Requires form + handler + function signature change |
| TRANSMIT bug: `HOUSEHOLD_MAP[household][0]` always picks first account | Single fix site at line 6972 |
| Only 1 legacy row (ABANDONED) — no active rows to backfill | F20-2 simplifies to migration-only |
| Option X (per-account rows) is safer than Option Y (JSON allocation) | Preserves 1 row = 1 order invariant |

**Key decisions for Architect:**
1. Option X confirmed? Or v1 simplification (single account with most unencumbered shares)?
2. STK_SELL Cure Console form change in scope or deferred?
3. `_compute_overweight_scope` modification vs post-processing for per-account excess?

---

F20 survey done | tests: 451/451 | STOP | reports/f20_sub_account_routing_survey_20260408.md
