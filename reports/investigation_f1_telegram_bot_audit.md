# Investigation F.1 — telegram_bot LLM loop + NL staging + Yash-only handlers

## Executive summary

3 HIGH / 3 MED / 1 LOW found. The LLM tool-loop and Yash-only handlers are
largely sound (tx_immediate sweep landed correctly, is_authorized on all
state-changing commands). The HIGH findings are concentrated in the inline
keyboard callback layer: all `cc:*` buttons are orphaned (no registered
handler), and `parse_and_stage_order` (exposed in `_TOOL_DISPATCH`) stages
with `status='pending'` not `'staged'`, so the normal `/approve` flow never
surfaces those tickets. The NL-staging path (`parse_and_stage_order` PATH 1)
has no Gate-1/Gate-2 rule-engine evaluation before DB write; gates fire only
at TRANSMIT time in `_place_single_order`.

---

## HIGH findings

### F1-H-1 — All `cc:*` inline-keyboard callbacks are orphaned (no registered handler)

**File:line** `telegram_bot.py:9574–9620, 8627–8629, 22097–22104`

**Snippet**
```python
# _build_cc_ladder_views (line 9574–9620):
InlineKeyboardButton("Select {label}C",
    callback_data=f"cc:select:{expiry_index}:{strike_offset}:{row_offset}")
InlineKeyboardButton("Show Next 5 Strikes ⬇️",
    callback_data=f"cc:page:{expiry_index}:{strike_offset + 5}")
InlineKeyboardButton("Switch to {date} Expiry",
    callback_data=f"cc:exp:{other_index}:0")
# _cc_confirm_keyboard (line 8627):
InlineKeyboardButton("CONFIRM", callback_data=f"cc:confirm:{token}")
InlineKeyboardButton("CANCEL",  callback_data=f"cc:cancel:{token}")

# Registered handlers (line 22097–22104) — NO ^cc: pattern:
app.add_handler(CallbackQueryHandler(handle_orders_callback, pattern=r"^orders:"))
app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern=r"^approve:"))
app.add_handler(CallbackQueryHandler(handle_dex_callback,    pattern=r"^dex:"))
app.add_handler(CallbackQueryHandler(handle_liq_callback,    pattern=r"^liq:"))
app.add_handler(CallbackQueryHandler(handle_csp_approval_callback,
                                     pattern=r"^csp_(?:approve|skip|submit):"))
```

**Why it's a bug** The covered-call ladder dashboard sends five button types
(`cc:select`, `cc:page`, `cc:exp`, `cc:confirm`, `cc:cancel`) but no
`CallbackQueryHandler(pattern=r"^cc:")` is registered. Tapping any button
silently does nothing (telegram-bot-python drops unmatched callbacks). The
`/cc` command renders a dead interactive dashboard. Additionally,
`_cc_confirm_keyboard` is defined at line 8623 but is never called anywhere in
the file — the confirmation gate for CC staging exists on paper only.

**Proposed fix sketch** Register a `handle_cc_callback` handler with the
`^cc:` pattern at application startup. The comment block at line 9424–9429
documents that this handler was planned ("cc:page, cc:exp, cc:select,
cc:confirm, cc:cancel") but never implemented. ~40–80 LOC for the handler
plus 1 LOC for `app.add_handler(...)`.

---

### F1-H-2 — `parse_and_stage_order` writes `status='pending'` but `/approve` queue only shows `status='staged'` rows

**File:line** `telegram_bot.py:2875, 11167–11178, 11697`

**Snippet**
```python
# parse_and_stage_order (line 2875):
ticket = {
    ...
    "status":   "pending",   # <-- NOT 'staged'
    "transmit": False,
    ...
}
await asyncio.to_thread(append_pending_tickets, tickets)

# cmd_approve (line 11167):
rows = conn.execute(
    "SELECT id, payload, created_at FROM pending_orders "
    "WHERE status = 'staged' ORDER BY id"   # <-- filters for 'staged' only
).fetchall()

# handle_approve_callback single approval CAS (line 11697):
"UPDATE pending_orders SET status = 'processing' "
"WHERE id = ? AND status = 'staged'"       # <-- 'pending' rows never claimed
```

**Why it's a bug** PATH 1 (`ACCOUNT:` prefix messages) stages tickets as
`status='pending'`, but `cmd_approve` and `handle_approve_callback` only
operate on `status='staged'` rows. Tickets written by PATH 1 or by the LLM
via the `parse_and_stage_order` tool are silently invisible to `/approve`,
`APPROVE ALL`, and individual approve buttons. They accumulate in the DB
unreachable by the normal operator flow. The user sees the success message
but the order can never be transmitted through the standard path.

**Proposed fix sketch** Change `parse_and_stage_order` to write
`"status": "staged"` instead of `"pending"`, or change the `/approve` query
to include `status IN ('staged', 'pending')`. The former is a 1-LOC change
with no other callers affected (PATH 1 is the only writer using `'pending'`
for these tickets). Confirm `append_pending_tickets` default of `'pending'`
doesn't conflict with the CSP/CC pipelines that write their own status.

---

### F1-H-3 — `orders_detail` inline button is also orphaned (callback pattern mismatch)

**File:line** `telegram_bot.py:8797, 22097`

**Snippet**
```python
# _DASHBOARD_BUTTONS (line 8797):
InlineKeyboardButton("Order Details", callback_data="orders_detail")  # no colon

# Registered handler (line 22097):
app.add_handler(CallbackQueryHandler(handle_orders_callback, pattern=r"^orders:"))
# "orders_detail" does NOT match r"^orders:" (missing colon)
```

**Why it's a bug** The "Order Details" button renders in the Working Orders
dashboard but its `callback_data="orders_detail"` does not match the
registered pattern `r"^orders:"`. Tapping the button silently fails. The
handler at `handle_orders_callback` only splits on `:` and checks for
`refresh`, `match_mid`, `cancel_all` — the string `"orders_detail"` produces
action `"orders_detail"` (no colon split changes it) and falls through all
branches silently.

**Proposed fix sketch** Change `callback_data` to `"orders:detail"` (2 chars)
and add a `if action == "detail":` branch in `handle_orders_callback` (~5-15
LOC) to render the cached detail view from `dashboard_cache[chat_id]["views"]["orders_detail"]`.

---

## MED findings

### F1-M-1 — MAX_ROUNDS exhaust silently swallows the user's query with no user-visible message

**File:line** `telegram_bot.py:10631, 10801–10807`

**Snippet**
```python
for round_num in range(MAX_ROUNDS):   # MAX_ROUNDS = 15
    ...
    if response.stop_reason != "tool_use":
        ...
        break
    ...
    messages.append({"role": "user", "content": tool_results})

else:
    logger.warning("Hit MAX_ROUNDS (%d) in tool loop", MAX_ROUNDS)

try: await status.delete()   # spinner deleted — user sees nothing
except Exception: pass
```

**Why it's a bug** When the model uses all 15 tool-use rounds without
reaching a `stop_reason != "tool_use"` (e.g., infinite tool-call loop from a
confused model state), the `for...else` branch only logs a warning. The
status spinner is deleted but no text reply is sent to the user. From the
operator's perspective the bot just silently dropped the query. With 15 rounds
of Opus, this also consumes ~15× the token budget per query.

**Proposed fix sketch** Add `await update.message.reply_text("⚠️ Hit tool-use
limit — partial work done. Try rephrasing or /clear context.")` in the `else`
branch (~2 LOC). Consider also reducing `MAX_ROUNDS` for the Haiku model.

---

### F1-M-2 — `parse_and_stage_order` (PATH 1 and LLM tool) skips all Gate-1/Gate-2 rule-engine evaluation at staging time

**File:line** `telegram_bot.py:2807–2897`

**Snippet**
```python
async def parse_and_stage_order(text: str) -> str:
    payload = _parse_screener_payload(text)   # only validates format/account
    for i, leg in enumerate(payload["legs"], 1):
        ticket = { ..., "status": "pending", "transmit": False, ... }
        tickets.append(ticket)
    await asyncio.to_thread(append_pending_tickets, tickets)
    return "✅ Order Staged..."
    # No Gate-1 (concentration, NLV headroom, Rulebook rules) evaluation here
```

**Why it's a bug** Tickets written by PATH 1 bypass all Rulebook gate logic.
Gate-1 checks (concentration limits, delta-adjusted exposure, NLV headroom,
3-strike lock) only run at TRANSMIT time inside `_place_single_order`. An
operator reviewing `/approve` sees a staged ticket with no indication that it
violates any rule, and may transmit it before realizing gates will block it at
execution. This is particularly relevant for the LLM tool path where the model
calls `parse_and_stage_order` without any preflight knowledge. Not immediately
exploitable since `transmit=False` and approval is still gated, but the
staging step gives a false "safe" signal.

**Proposed fix sketch** Add a lightweight preflight call to the circuit
breaker and concentration check at the top of `parse_and_stage_order`, similar
to the `cmd_daily` pattern at line 14268–14300. ~10–20 LOC. Gate result should
be appended to the success message, not block staging (fail-open acceptable at
staging; fail-closed at transmit).

---

### F1-M-3 — `update_live_order` (exposed in `_TOOL_DISPATCH`) lacks account-ID membership validation

**File:line** `telegram_bot.py:7971–8090, 8121–8125`

**Snippet**
```python
# _TOOL_DISPATCH (line 8121):
"update_live_order": lambda args: update_live_order(
    args["ticker"], args["account_id"], args["new_limit_price"]
),

# update_live_order (line 8015):
for t in trades:
    if (t.orderStatus.status in WORKING
            and t.contract.symbol.upper() == ticker_upper
            and t.order.account == account_id):   # arbitrary account_id accepted
```

**Why it's a bug** The LLM (Haiku, Sonnet, or Opus) can call
`update_live_order` with any string as `account_id`. There is no validation
that the account belongs to `ACTIVE_ACCOUNTS`. If the model hallucinates an
account ID, the order-scan will return a non-match error gracefully — but if
the model somehow constructs a valid account ID for an account that happens to
have a matching order, the reprice ticket is staged for that account without
explicit operator awareness. Compare with `stage_trade_for_execution` which
does check `if account_id not in ACTIVE_ACCOUNTS: return error`. The
asymmetry is a latent risk.

**Proposed fix sketch** Add `if account_id not in ACTIVE_ACCOUNTS: return
json.dumps({"error": "..."})` at the top of `update_live_order`, matching the
pattern at line 6631. ~3 LOC.

---

## LOW findings

### F1-L-1 — `_check_and_track_tokens(0, 0)` budget pre-flight silently writes a zero-row DB record

**File:line** `telegram_bot.py:10585, 1023–1041`

**Snippet**
```python
# Pre-flight budget check (line 10585):
if not _check_and_track_tokens(0, 0):
    raise RuntimeError("Daily token budget reached...")

# _check_and_track_tokens (line 1041):
_tokens_used_today += total   # total = 0 + 0 = 0, adds no count
# ... but then writes to api_usage with (today, 0, 0):
conn.execute("INSERT INTO api_usage ... ON CONFLICT DO UPDATE SET api_calls = api_calls + 1",
             (today, 0, 0))
```

**Why it's a bug** Every call to `_dispatch_to_llm` (i.e., every user
message) triggers a DB write to `api_usage` with 0 tokens but `api_calls + 1`
due to the ON CONFLICT DO UPDATE clause. The `api_calls` counter in the DB
therefore over-counts by 1 per conversation message, inflating the call-count
metric in `/budget` reports. Low severity since only a reporting artifact.

**Proposed fix sketch** Guard the DB write inside `_check_and_track_tokens`
with `if total > 0:` before the tx_immediate block, similar to the
`if model and total > 0:` guard already present for the per-model table at
line 1073. ~2 LOC.

---

## Coverage notes

**Read in full:**
- `_dispatch_to_llm` (lines 10567–10820): LLM tool-loop, MAX_ROUNDS, all tool error paths
- `handle_message` (10833–10909): PATH 1 and PATH 2 routing
- `cmd_think`, `cmd_deep` (10923–10979): model escalation, identical auth pattern
- `parse_and_stage_order` (2807–2929): NL staging path, gate enforcement
- `_parse_screener_payload` (2673–2801): account validation, leg parsing
- `stage_ratio_spread` (6207–6553): confirmed NOT in `_TOOL_DISPATCH`
- `stage_trade_for_execution` (6565–6753): confirmed NOT in `_TOOL_DISPATCH`
- `_TOOL_DISPATCH` and `TOOLS` (8099–8490): full tool registry
- All callback handlers: `handle_approve_callback`, `handle_csp_approval_callback`, `handle_orders_callback`, `handle_liq_callback`, `handle_dex_callback`
- All registered `CallbackQueryHandler` entries (22097–22104)
- `cmd_halt`, `cmd_resume`, `cmd_reject`, `cmd_approve`, `cmd_recover_transmitting`, `cmd_scan`, `cmd_cc`, `cmd_vrp`, `cmd_rollcheck`, `cmd_csp_harvest`, `cmd_daily`, `cmd_report`, `cmd_list_rem`, `cmd_approve_rem`, `cmd_reject_rem`, `cmd_cure`
- `_sync_db_write`, `_sync_db_read_one`, `append_pending_tickets`, `_place_single_order`, `_auto_execute_staged`

**Explicitly skipped:**
- `_run_cc_logic`, `roll_scanner`, `scan_csp_harvest_candidates` (called from handlers but live in `agt_equities/`; audit scope was `telegram_bot.py` surface only)
- `_scheduled_cc`, `_scheduled_csp_scan` scheduled jobs (not in target surface)
- The full TRANSMIT/JIT re-validation chain inside `handle_dex_callback` / `_place_single_order` (audited for `_HALTED` and tx_immediate presence; deep rule logic not enumerated)
- Auth on `handle_dex_callback`: confirmed `user_id != AUTHORIZED_USER_ID` check present at line 12089
- Auth on `handle_liq_callback`: confirmed `user.id != AUTHORIZED_USER_ID` check at line 19272
- Auth on `handle_csp_approval_callback`: confirmed `user_id != AUTHORIZED_USER_ID` at line 11946
