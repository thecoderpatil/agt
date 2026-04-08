# F23 — Graceful Shutdown + 1101/1102 Differentiation — Survey Report

**Date:** 2026-04-08
**Sprint:** F23
**Status:** Survey complete. Awaiting Architect review.

---

## Survey 1.1A — ApplicationBuilder + main() + callbacks

**`main()` location:** `telegram_bot.py:11236`
**`ApplicationBuilder` chain:** `telegram_bot.py:11244-11248`

```python
app = (
    ApplicationBuilder()
    .token(TELEGRAM_BOT_TOKEN)
    .post_init(post_init)
    .build()
)
```

**Callbacks registered:**
- `.post_init(post_init)` — YES, at line 11247
- `.post_shutdown(...)` — **NO, not registered**
- `.post_stop(...)` — **NO, not registered**

**`post_init` function:** `telegram_bot.py:11111-11131`

```python
async def post_init(app) -> None:
    try:
        ib_conn = await ensure_ib_connected()
    except Exception as exc:
        logger.error("Could not connect on startup: %s", exc)
        logger.error("Use /reconnect once Gateway/TWS is ready.")
        return

    # Followup #17: populate open order + execution caches for orphan scan
    try:
        await ib_conn.reqAllOpenOrdersAsync()
        from ib_async.objects import ExecutionFilter
        await ib_conn.reqExecutionsAsync(ExecutionFilter())
    except Exception as exc:
        logger.warning("post_init: reqAllOpenOrders/reqExecutions failed: %s", exc)

    # Followup #17: scan for orphaned TRANSMITTING rows
    try:
        await _scan_orphaned_transmitting_rows(ib_conn, app.bot)
    except Exception as exc:
        logger.error("Orphan scan failed: %s — bot continues without scan", exc)
```

**`app.run_polling()`** at line 11347 — this is the blocking entry point. PTB handles SIGINT internally and calls `Application.stop()` → `Application.shutdown()` → `post_shutdown` callbacks (if registered).

---

## Survey 1.1B — sqlite3.connect() calls

**Total:** 2 occurrences in `telegram_bot.py`.

| Line | Function | Category |
|------|----------|----------|
| 233 | `_get_db_connection()` | Per-call factory. Returns a new connection. All callers use `with closing(_get_db_connection()) as conn:` |
| 6506 | `_increment_revalidation_count()` | Isolated connection. Opened, committed, closed within the same function (try/finally with `iso_conn.close()`) |

**Conclusion:** No module-level persistent SQLite connections. All connections are per-call and closed by the caller (`closing()` or explicit `.close()`). **No explicit SQLite close needed at shutdown.** Step 3 in `_graceful_shutdown` becomes a no-op comment.

---

## Survey 1.1C — Background tasks / jobs

### asyncio.Task

| Global | Line | Description |
|--------|------|-------------|
| `_reconnect_task` | 1119 | Created by `_schedule_reconnect()` at line 1178 via `asyncio.create_task(_auto_reconnect())`. Needs cancellation at shutdown. |

### PTB JobQueue (registered at lines 11293-11343)

| Name | Type | Schedule | Line |
|------|------|----------|------|
| `cc_daily` | `run_daily` | 9:45 AM ET, Mon-Fri | 11295 |
| `watchdog_daily` | `run_daily` | 3:30 PM ET, Mon-Fri | 11302 |
| `universe_monthly` | `run_monthly` | 1st at 6:00 AM ET | 11309 |
| `conviction_weekly` | `run_daily` | 8:00 PM ET, Sunday | 11316 |
| `flex_sync_eod` | `run_daily` | 5:00 PM ET, Mon-Fri | 11323 |
| `attested_poller` | `run_repeating` | every 10s | 11330 |
| `attested_sweeper` | `run_repeating` | every 60s | 11337 |

**PTB JobQueue lifecycle:** PTB's `Application.stop()` calls `JobQueue.stop()` internally before `post_shutdown` fires. All 7 jobs are managed by PTB. **No manual cancellation needed for JobQueue jobs.**

**Only `_reconnect_task` requires explicit cancellation in `_graceful_shutdown`.**

---

## Survey 1.1D — _auto_reconnect() + ensure_ib_connected() + errorEvent

### `_auto_reconnect()` — `telegram_bot.py:1122-1171`

```python
async def _auto_reconnect():
    logger.warning("IB disconnected — retrying in 60s…")
    await asyncio.sleep(60)
    for attempt in range(1, 6):
        try:
            ib_conn = await ensure_ib_connected()
            logger.info("Auto-reconnected (attempt %d)", attempt)
            # Followup #17 Part C.5: orphan scan on autoreconnect
            try:
                await ib_conn.reqAllOpenOrdersAsync()
                from ib_async.objects import ExecutionFilter
                await ib_conn.reqExecutionsAsync(ExecutionFilter())
                from telegram import Bot
                bot = Bot(token=TELEGRAM_BOT_TOKEN)
                await _scan_orphaned_transmitting_rows(ib_conn, bot)
            except Exception as scan_exc:
                logger.exception("Autoreconnect orphan scan failed: %s", scan_exc)
            try:
                from telegram import Bot
                bot = Bot(token=TELEGRAM_BOT_TOKEN)
                await bot.send_message(
                    chat_id=AUTHORIZED_USER_ID,
                    text=f"✅ IB Gateway reconnected (attempt {attempt}/5).",
                )
            except Exception:
                pass
            return
        except Exception as exc:
            logger.error("Reconnect attempt %d failed: %s", attempt, exc)
            await asyncio.sleep(60)
    logger.error("Gave up reconnecting after 5 attempts — use /reconnect")
    # ... (Telegram CRITICAL alert on total failure, lines 1154-1171)
```

### `ensure_ib_connected()` — `telegram_bot.py:1181-1244`

```python
async def ensure_ib_connected() -> ib_async.IB:
    global ib
    async with _ib_connect_lock:
        if ib is not None and ib.isConnected():
            return ib

        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                pass
            ib = None

        for port, label in[(IB_TWS_PORT, "Gateway"), (IB_TWS_FALLBACK, "TWS")]:
            candidate = ib_async.IB()
            try:
                logger.info("Connecting to %s:%s (%s) clientId=%s …",
                    IB_HOST, port, label, IB_CLIENT_ID)
                candidate.disconnectedEvent += _schedule_reconnect
                await candidate.connectAsync(IB_HOST, port, clientId=IB_CLIENT_ID, timeout=10)
                candidate.reqMarketDataType(4)
                await asyncio.sleep(2)
                candidate.reqPositions()
                await asyncio.sleep(1)
                # Register fill event listeners for ledger auto-update
                try:
                    candidate.execDetailsEvent.clear()
                    candidate.execDetailsEvent += _offload_fill_handler(_on_cc_fill)
                    candidate.execDetailsEvent += _offload_fill_handler(_on_csp_premium_fill)
                    candidate.execDetailsEvent += _offload_fill_handler(_on_option_close)
                    candidate.execDetailsEvent += _offload_fill_handler(_on_shares_sold)
                    candidate.execDetailsEvent += _offload_fill_handler(_on_shares_bought)
                    # R5: Order state machine handlers
                    candidate.orderStatusEvent += _r5_on_order_status
                    candidate.execDetailsEvent += _offload_fill_handler(_r5_on_exec_details)
                    candidate.commissionReportEvent += _r5_on_commission_report
                    logger.info("Fill + R5 order state event listeners registered (8 handlers)")
                except Exception as evt_exc:
                    logger.warning("Failed to register fill events: %s", evt_exc)

                ib = candidate
                logger.info("Connected via %s — accounts: %s", label, ib.managedAccounts())
                return ib
            except Exception as exc:
                logger.warning("%s failed (%s) — trying next…", label, exc)
                try:
                    candidate.disconnect()
                except Exception:
                    pass

        raise ConnectionError(
            f"Could not connect to Gateway (port {IB_TWS_PORT}) "
            f"or TWS (port {IB_TWS_FALLBACK})."
        )
```

### `_schedule_reconnect()` — `telegram_bot.py:1174-1178`

```python
def _schedule_reconnect() -> None:
    global _reconnect_task
    if _reconnect_task is not None and not _reconnect_task.done():
        return
    _reconnect_task = asyncio.create_task(_auto_reconnect())
```

### Key confirmations:
- **`errorEvent` is NOT registered anywhere.** Grep returned zero matches across the entire codebase.
- **`_schedule_reconnect` is the ONLY `disconnectedEvent` handler** (line 1204).
- **Fill handler re-registration** at lines 1217-1227: `execDetailsEvent.clear()` then 5 `execDetailsEvent +=`, plus `orderStatusEvent +=` and `commissionReportEvent +=` (8 handlers total).
- **No `errorEvent.clear()` or `errorEvent +=` anywhere.**

---

## Survey 1.2E — Existing 1100/1101/1102 handling

**Grep for `1100`, `1101`, `1102` in telegram_bot.py:** Zero matches.

**Conclusion:** No handling of these error codes exists anywhere in the codebase. Confirmed.

---

## Survey 1.2F — ib_async version + API surface

**Installed version:** `ib_async 2.1.0` (confirmed via `import ib_async; print(ib_async.__version__)`).

**No version pin file found.** No `requirements.txt` in root (only `requirements-dev.txt` with `hypothesis>=6.150,<7`). No `pyproject.toml` in root. The version is managed by the venv directly.

**API surface verified on ib_async 2.1.0 instance:**

| API | Exists | Notes |
|-----|--------|-------|
| `ib.errorEvent` | YES | `eventkit.event.Event` on instance (dynamic) |
| `ib.disconnectedEvent` | YES | Already in use |
| `ib.disconnectAsync()` | **NO** | Does not exist |
| `ib.disconnect()` | YES | Synchronous |
| `ib.reqOpenOrdersAsync()` | YES | |
| `ib.reqAllOpenOrdersAsync()` | YES | |
| `ib.reqExecutionsAsync()` | YES | |

**errorEvent callback signature** (from ib_async source `IB._onError`):
```python
def _onError(self, reqId, errorCode, errorString, contract):
```
So external handlers receive: `(reqId, errorCode, errorString, contract)`.

**IMPORTANT:** ib_async 2.1.0 already has an internal 1102 handler in `_onError`:
```python
if errorCode == 1102:
    asyncio.ensure_future(self.reqAccountSummaryAsync())
```
Our handler will fire **in addition to** this internal handler. No conflict — ib_async resubscribes to account summary, we handle alerts + orphan scan. The `errorEvent` fires after `_onError`, so ib_async's internal handling runs first.

**Disconnect path:** Since `disconnectAsync()` does not exist, `_graceful_shutdown` must use `ib.disconnect()` directly (synchronous). Since it's synchronous and brief (just closes the socket), wrapping in `asyncio.to_thread()` is appropriate but arguably unnecessary. Will use `ib.disconnect()` directly since it's non-blocking in practice (socket close).

---

## Survey 1.3G — openTrades / executions call sites

| Line | Expression | Context |
|------|-----------|---------|
| 3528 | `ib_conn.openTrades()` | `/orders` command — display only |
| 3763 | `ib_conn.openTrades()` | `/status_orders` command — display only |
| 4468 | `ib_conn.openTrades()` | `/fills` command — display only |
| 4565 | `ib_conn.openTrades()` | `/reconcile` command — display only |
| 5278 | `ib_conn.openTrades()` | Ledger display helper |
| 11009 | `ib_conn.openTrades()` | **Orphan scan** — reconciliation (DB writes) |
| 11010 | `ib_conn.executions()` | **Orphan scan** — reconciliation (DB writes) |

**Impact of 1101 data loss:** Lines 3528, 3763, 4468, 4565, 5278 are display-only — they'll return empty collections after 1101 until repopulated. No corruption risk. Line 11009-11010 (orphan scan) depends on `reqAllOpenOrdersAsync()` + `reqExecutionsAsync()` being called first to repopulate — which is exactly what `_handle_1101_data_lost` does.

No calls to `ib.trades()` or `ib.fills()` found in the codebase.

---

## Survey 1.3H — Orphan scan from Followup #17

**Entry point:** `_scan_orphaned_transmitting_rows(ib_conn, app_bot)` at `telegram_bot.py:10984`

**Signature:** `async def _scan_orphaned_transmitting_rows(ib_conn, app_bot):`
- `ib_conn`: ib_async.IB instance (must have populated `openTrades()` and `executions()`)
- `app_bot`: telegram Bot instance (for sending alerts)

**Safe to re-invoke from 1101 handler: YES.**

Evidence:
1. `_auto_reconnect()` already re-invokes it on reconnect (line 1136), proving the pattern is established.
2. All DB writes are CAS-guarded: `WHERE final_status = 'TRANSMITTING'` — prevents double-write.
3. Column ownership: writes ONLY `final_status` + `last_updated` (D4 binding).
4. Read path: fresh SELECT of TRANSMITTING rows each call — no stale state carried.
5. Resolution policy (D3) is inherently idempotent: filled → TRANSMITTED, dead → ABANDONED, live → leave, not found → leave.
6. No side effects beyond DB writes + Telegram alert.

**For `_handle_1101_data_lost`:**
- Must call `reqAllOpenOrdersAsync()` + `reqExecutionsAsync(ExecutionFilter())` BEFORE calling `_scan_orphaned_transmitting_rows()` to repopulate ib_async's internal collections.
- Use `from ib_async.objects import ExecutionFilter` (existing codebase pattern, not `from ib_async import ExecutionFilter`).
- Bot instance: `Bot(token=TELEGRAM_BOT_TOKEN)` (same pattern as `_auto_reconnect` at line 1134).

---

## Worked Example Validation

### Scenario A (1102 — data maintained)
- Handler receives `errorCode=1102`.
- Logs `INFO: IBKR 1102: connectivity restored, data maintained`.
- Sends Telegram info alert: green circle emoji + "data maintained, no action needed."
- Takes NO other action. No orphan scan. No order re-fetch.
- ib_async's internal `_onError` separately resubscribes to account summary.
- **Testable:** Mock `_handle_1101_data_lost`, call `_on_ib_error(..., 1102, ...)`, assert it was NOT called.

### Scenario B (1101 — data lost)
- Handler receives `errorCode=1101`.
- Logs `CRITICAL: IBKR 1101: connectivity restored, DATA LOST`.
- `_handle_1101_data_lost()` fires:
  1. Sends CRITICAL Telegram alert.
  2. Calls `ib.reqOpenOrdersAsync()` to repopulate open orders.
  3. Calls `ib.reqExecutionsAsync(ExecutionFilter())` to repopulate executions.
  4. Calls `_scan_orphaned_transmitting_rows(ib, bot)` to reconcile.
  5. Sends result alert.
- **Testable:** Mock all 4 dependencies, assert called in order.

### Scenario C (Ctrl+C graceful shutdown)
- PTB's SIGINT handler fires.
- `Application.stop()` runs → `JobQueue.stop()` (PTB internal) → all 7 scheduled jobs stopped.
- `Application.shutdown()` runs → `post_shutdown` callbacks fire.
- `_graceful_shutdown()` runs:
  1. Cancels `_reconnect_task` if pending.
  2. Calls `ib.disconnect()` with 5s timeout.
  3. SQLite: no-op (all per-call connections).
  4. Logs: `post_shutdown: complete — all resources released`.
- **Testable:** Unit test mocking `ib` and `_reconnect_task`.

### Scenario D (X-button — negative test)
- Windows X-button sends CTRL_CLOSE_EVENT.
- Python signal handlers do NOT fire for this event.
- `post_shutdown` does NOT fire.
- **Testable:** Document-only (cannot simulate X-button in pytest). Operational rule: Ctrl+C only. PRE_PAPER_CHECKLIST already documents this.

---

## Deviations from Spec

### D1: `disconnectAsync()` does not exist
**Spec says:** "Use `asyncio.to_thread(ib.disconnect)` rather than `await ib.disconnectAsync()` ONLY IF Survey 1.2F shows the pinned ib_async version doesn't expose `disconnectAsync`."
**Finding:** `disconnectAsync` does not exist on ib_async 2.1.0. `ib.disconnect()` is synchronous.
**Recommendation:** Use `ib.disconnect()` directly (it's a socket close — non-blocking in practice). Wrapping in `asyncio.to_thread()` is safe but may be overkill for a socket teardown. Architect call.

### D2: No canonical `_alert_telegram` helper exists
**Spec says:** "The `_alert_telegram` helper may already exist under another name."
**Finding:** No centralized Telegram alert helper exists. The pattern throughout the codebase is ad-hoc `Bot(token=TELEGRAM_BOT_TOKEN)` construction:
- `_auto_reconnect()` lines 1134, 1140-1141, 1157
- Orphan scan line 11103
**Recommendation:** Per spec F23-2.5, extract a thin helper. But this is a cosmetic refactor touching existing code. Architect decides whether to extract or keep inline.

### D3: ib_async internal 1102 handler
**Spec does not mention** that ib_async 2.1.0 already handles 1102 internally in `_onError` by resubscribing to account summary. Our handler is additive and non-conflicting. Architect should be aware.

### D4: `ExecutionFilter` import path
**Spec uses:** `from ib_async import ExecutionFilter`
**Codebase uses:** `from ib_async.objects import ExecutionFilter` (lines 1132, 11122)
**Recommendation:** Use codebase convention: `from ib_async.objects import ExecutionFilter`.

### D5: `_on_ib_error` signature — `contract` parameter
**Spec's handler signature:** `def _on_ib_error(reqId: int, errorCode: int, errorString: str, contract) -> None:`
**ib_async source:** `def _onError(self, reqId, errorCode, errorString, contract):` — matches.
No deviation, just confirming the 4-parameter signature is correct.

### D6: Orphan scan function name
**Spec placeholder:** `_run_orphan_scan_post_1101()`
**Actual function:** `_scan_orphaned_transmitting_rows(ib_conn, app_bot)` at line 10984.
Will substitute in implementation.

### D7: Test count target
**Spec says:** 10 new tests → 451/451.
**Current:** 441/441. 441 + 10 = 451. Confirmed.

### D8: Scenario D (X-button) test
**Spec says:** "The test exists to prove the X-button bypass."
**Finding:** This cannot be a pytest unit test — it requires process-level simulation. Suggest making this an empirical verification (manual) documented in the implementation report, NOT a pytest test. The 10 test spec does not include a Scenario D test, so this aligns.

---

## Summary

All 8 survey items complete. No blockers found. The codebase is ready for F23 implementation with the deviations noted above.

**Key implementation decisions for Architect:**
1. D1: `ib.disconnect()` direct vs `asyncio.to_thread(ib.disconnect)` wrap?
2. D2: Extract `_alert_telegram` helper or keep inline `Bot(token=...)` pattern?
3. D3: Acknowledge ib_async's internal 1102 handler — our handler is additive.

---

F23 survey done | tests: 441/441 | survey complete | STOP | reports/f23_shutdown_survey_20260408.md
