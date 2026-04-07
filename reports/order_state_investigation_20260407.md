# R5: Order State Machine Investigation

Generated: 2026-04-07
Status: REPORT ONLY — awaiting Yash review

## (a) pending_orders: 205 rows

Schema: `id INTEGER PK, payload JSON, status TEXT, created_at TIMESTAMP`

Status distribution:
- `superseded`: 92 (old staged orders overwritten by new ones)
- `rejected`: 64 (Yash rejected via /reject)
- `approved`: 49 (sent to IBKR)

No `FILLED`, `CANCELLED`, or `WORKING` status. Once approved, the order's fate is not tracked back in pending_orders.

## (b) placeOrder call sites (3 locations)

1. **Line 4000** — inside general order execution flow
2. **Line 6075** — inside `/approve` command handler
3. **Line 7564** — inside dynamic exit handler

At line 6082 (after placeOrder returns):
```python
UPDATE pending_orders SET status = 'approved' WHERE id = ?
```

**Bug confirmed:** Status flips to `'approved'` at the moment `placeOrder` is called, not when IBKR acknowledges. If `placeOrder` throws or IBKR rejects the order, the status is already `'approved'`. The failure path (line 6126) sets `status = 'failed'`, but only if the exception is caught in the immediate try/except — race conditions or IBKR-side rejections after acknowledgment are lost.

## (c) bot_order_log: 0 rows

Table exists (created in Phase 0 schema) but is never written to. The `ExecutionBridge` that would populate it is Phase 3 scope (not yet implemented).

## (d) live_blotter: 2 rows, stale

Last update: `2026-04-03T21:11:37` (4 days ago). Both rows: PYPL, status `PreSubmitted`.

The live_blotter is updated via event handlers but only for the most recent session. On bot restart, stale entries persist. No cleanup mechanism beyond manual `/cleanup_blotter`.

## (e) ib_async event handlers: 5 wired

```python
# Line 1075-1080 (inside ensure_ib_connected):
candidate.execDetailsEvent.clear()
candidate.execDetailsEvent += _offload_fill_handler(_on_cc_fill)
candidate.execDetailsEvent += _offload_fill_handler(_on_csp_premium_fill)
candidate.execDetailsEvent += _offload_fill_handler(_on_option_close)
candidate.execDetailsEvent += _offload_fill_handler(_on_shares_sold)
candidate.execDetailsEvent += _offload_fill_handler(_on_shares_bought)
```

**Handlers registered:**
- `execDetailsEvent`: 5 handlers for different fill types (CC, CSP premium, option close, shares sold, shares bought)
- `disconnectedEvent`: reconnect handler
- `orderStatusEvent`: **NOT wired** — order status changes (Working, Cancelled, Filled) are not tracked
- `commissionReportEvent`: **NOT wired** — commission data after fills is not captured

**Gap:** Fill handlers update `premium_ledger` (legacy) but do NOT update `pending_orders` status or `bot_order_log`. The order lifecycle ends at `'approved'` in pending_orders — there's no feedback loop from IBKR's order status events.

## (f) Status reads on pending_orders (12 sites)

| Line | Query | Purpose |
|------|-------|---------|
| 5683 | `SELECT WHERE status = 'staged'` | List staged orders for review |
| 5798 | `UPDATE SET status = 'rejected' WHERE status = 'staged'` | Reject all staged |
| 5811 | `SELECT WHERE status = 'staged' ORDER BY id` | Find next to approve |
| 5822 | `UPDATE SET status = 'processing'` | Mark as being processed |
| 5833 | `SELECT WHERE status = 'processing'` | Resume processing |
| 5902 | `UPDATE SET status = 'processing'` | Same pattern |
| 6012 | `UPDATE SET status = 'duplicate_skipped'` | Dedup check |
| 6040 | `UPDATE SET status = 'rejected_naked'` | Naked position check |
| 6082 | `UPDATE SET status = 'approved'` | After placeOrder |
| 6126 | `UPDATE SET status = 'failed'` | On placeOrder exception |
| 6147 | `UPDATE SET status = 'rejected'` | Batch reject |
| 697 | `INSERT status = 'staged'` | Initial staging |

## (g) Proposed status enum

```
STAGED → SENT → ACKED → WORKING → FILLED
                                 → PARTIALLY_FILLED
                                 → REJECTED
                                 → CANCELLED
                                 → EXPIRED
```

Transitions:
- `STAGED`: created by /cc, /scan, or manual staging
- `SENT`: `placeOrder()` called (replaces current `'approved'`)
- `ACKED`: `orderStatusEvent` with status `Submitted` or `PreSubmitted`
- `WORKING`: `orderStatusEvent` with status `Submitted` (order is live on exchange)
- `FILLED`: `execDetailsEvent` fires
- `PARTIALLY_FILLED`: `execDetailsEvent` with partial qty
- `REJECTED`: `orderStatusEvent` with status `Inactive` or IBKR error
- `CANCELLED`: `orderStatusEvent` with status `Cancelled`
- `EXPIRED`: `orderStatusEvent` with status `Inactive` after expiry

## (h) Event-driven transitions

| ib_async event | Trigger | New status |
|----------------|---------|------------|
| `placeOrder()` returns | Immediate | SENT |
| `orderStatusEvent(status='PreSubmitted')` | IBKR routing | ACKED |
| `orderStatusEvent(status='Submitted')` | Exchange working | WORKING |
| `execDetailsEvent` | Fill (full or partial) | FILLED or PARTIALLY_FILLED |
| `orderStatusEvent(status='Cancelled')` | User or system cancel | CANCELLED |
| `orderStatusEvent(status='Inactive')` | IBKR rejection | REJECTED |
| No event within T+1 day | Timeout | EXPIRED (mark manually) |

## (i) Schema recommendation: extend pending_orders

**Recommend extending pending_orders** over creating a new table. Reason: all existing code reads/writes `pending_orders` — creating a parallel `order_lifecycle` table would require updating 12+ sites. Extension is less disruptive.

Add columns:
```sql
ALTER TABLE pending_orders ADD COLUMN ib_order_id INTEGER;
ALTER TABLE pending_orders ADD COLUMN ib_perm_id INTEGER;
ALTER TABLE pending_orders ADD COLUMN status_history TEXT;  -- JSON array of {status, timestamp, source}
ALTER TABLE pending_orders ADD COLUMN fill_price REAL;
ALTER TABLE pending_orders ADD COLUMN fill_qty INTEGER;
ALTER TABLE pending_orders ADD COLUMN fill_commission REAL;
ALTER TABLE pending_orders ADD COLUMN fill_time TEXT;
ALTER TABLE pending_orders ADD COLUMN last_ib_status TEXT;
```

`status_history` example:
```json
[
  {"status": "staged", "at": "2026-04-07T09:45:00", "by": "/cc"},
  {"status": "sent", "at": "2026-04-07T09:46:12", "by": "/approve"},
  {"status": "acked", "at": "2026-04-07T09:46:13", "by": "orderStatusEvent"},
  {"status": "filled", "at": "2026-04-07T09:46:15", "by": "execDetailsEvent"}
]
```

## (j) Backfill plan

Current 49 `'approved'` rows: update to `'approved_legacy'` to distinguish from the new enum. These orders' IBKR outcomes are unknown without historical event data.

## (k) T+2 reconciliation check

After each Flex sync:
```sql
SELECT po.id, po.payload, po.ib_order_id
FROM pending_orders po
WHERE po.status = 'sent' AND po.created_at < date('now', '-2 days')
AND NOT EXISTS (
    SELECT 1 FROM master_log_trades t
    WHERE t.ib_order_id = po.ib_order_id
)
```

Any result = orphan order: sent but never matched to a Flex trade. Flag for investigation.

## Production DB: READ-ONLY (no changes during investigation)
