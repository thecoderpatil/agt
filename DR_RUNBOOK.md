# AGT Equities — Disaster Recovery Runbook v1

**Generated:** 2026-04-08 (DR-02/DR-04 filled: F23)
**Anchor:** `339c4a2` (Sprint D), updated doc hygiene sweep
**Tests:** 608/608

---

## Drill Checklist

| ID | Scenario | Severity | Detect target | Recover target | Last drilled |
|----|----------|----------|---------------|----------------|-------------|
| DR-01 | SQLite corruption / WAL checkpoint failure | CRITICAL | 5 min | 10 min | NEVER |
| DR-02 | Bot crash with ATTESTED row mid-transmit | CRITICAL | 60 sec | 5 min | NEVER |
| DR-03 | IBKR disconnect mid-stage (pre-transmit) | HIGH | 60 sec | 5 min | NEVER |
| DR-04 | IBKR disconnect post-transmit, pre-ack | CRITICAL | 60 sec | 5 min | NEVER |
| DR-05 | Telegram API outage | MEDIUM | 10 min | 15 min | NEVER |
| DR-06 | CC leg partial fill | HIGH | 60 sec | 30 min | NEVER |
| DR-07 | Duplicate transmit race (CAS catches) | MEDIUM | 0 sec (auto) | 0 min (auto) | NEVER |
| DR-08 | Clock skew / TZ drift | MEDIUM | 15 min | 5 min | NEVER |

---

## DR-01 — SQLite Corruption / WAL Checkpoint Failure

**Severity:** CRITICAL
**Detect window target:** 5 minutes
**Recover window target:** 10 minutes

### Symptom
- Bot commands return errors or silently fail
- Flex sync log shows `Flex sync failed: ...` with SQLite errors
- Litestream replication stops advancing (R2 bucket stale)
- `agt_desk.db-wal` file grows unboundedly (checkpoint not running)

### Detect
Log patterns (grep-ready):
```
grep "Flex sync failed" logs/*.log
grep "OperationalError" logs/*.log
grep "database is locked" logs/*.log
grep "disk I/O error" logs/*.log
```

DB integrity check:
```sql
-- Run against agt_desk.db
PRAGMA integrity_check;
-- Expected: "ok"
-- Any other result = corruption detected

PRAGMA journal_mode;
-- Expected: "wal"
-- "delete" or "off" = WAL mode lost
```

WAL file size check:
```bash
ls -la agt_desk.db-wal
# Expected: < 10 MB during normal operation
# > 50 MB = checkpoint not running, investigate
```

### Contain
1. Stop the bot process: `Ctrl+C` or kill the Python process
2. Stop Litestream replication: kill the `litestream.exe replicate` process
3. Do NOT delete `agt_desk.db-wal` or `agt_desk.db-shm` — they may contain uncommitted data

### Recover
1. **If DB passes integrity_check but WAL is large:**
   - Run manual checkpoint:
     ```python
     import sqlite3
     conn = sqlite3.connect("agt_desk.db")
     conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
     conn.close()
     ```
   - Expected: WAL file shrinks to 0 bytes
   - If checkpoint fails: proceed to step 2

2. **If DB fails integrity_check — restore from Litestream:**
   ```bash
   # Rename corrupt DB
   move agt_desk.db agt_desk.db.corrupt

   # Restore from R2
   litestream.exe restore -config litestream.yml -o agt_desk.db C:\AGT_Telegram_Bridge\agt_desk.db
   ```
   - Expected: restore completes in < 5 seconds
   - If R2 restore fails: use local baseline `agt_desk.db.phase1_baseline_20260407`
   - Rollback: rename `.corrupt` back to `agt_desk.db`

3. **Verify restored DB:**
   ```bash
   python restore_drill.py --from-r2
   ```
   - Expected: all 7 tables match (MATCH status)
   - If delta > 100 rows on any table: STOP, investigate manually

4. **Restart services:**
   ```bash
   boot_desk.bat
   ```
   Then in Telegram: `/reconcile`

### Verify
```sql
SELECT COUNT(*) FROM master_log_sync WHERE status = 'success'
  ORDER BY sync_id DESC LIMIT 1;
-- Expected: 1 row with recent finished_at timestamp

SELECT COUNT(*) FROM pending_orders WHERE status = 'staged';
-- Expected: 0 or small number (no stranded staged orders)
```

Check Litestream is replicating:
```bash
# Verify litestream process is running
tasklist | findstr litestream
```

### Post-mortem fields
- Timestamp of first error
- WAL file size at discovery
- PRAGMA integrity_check output
- Restore source used (R2 vs baseline)
- Data loss window (time between last known-good state and corruption)
- Root cause hypothesis (disk full, power loss, concurrent access)

### Repro Steps (drill)
1. Create a test DB: `python -c "import sqlite3; c = sqlite3.connect('test_dr01.db'); c.execute('CREATE TABLE t(x)'); c.execute('PRAGMA journal_mode=WAL'); c.commit()"`
2. Insert test data: `python -c "import sqlite3; c = sqlite3.connect('test_dr01.db'); c.executemany('INSERT INTO t VALUES (?)', [(i,) for i in range(1000)]); c.commit(); c.close()"`
3. Simulate corruption: `python -c "f = open('test_dr01.db', 'r+b'); f.seek(100); f.write(b'\\x00' * 50); f.close()"`
4. Verify corruption detected: `python -c "import sqlite3; c = sqlite3.connect('test_dr01.db'); print(c.execute('PRAGMA integrity_check').fetchone())"`
5. Expected: output is NOT "ok"
6. Clean up: `del test_dr01.db test_dr01.db-wal test_dr01.db-shm`

### Known gaps
- No automated PRAGMA integrity_check on startup
- No WAL file size monitoring or alerting
- No automated failover to baseline backup if R2 is unavailable
- Propose as Followup #16: automated DR drill scripts

---

## DR-02 — Bot Process Crash with ATTESTED Row Mid-Transmit

**Severity:** CRITICAL
**Detect window target:** 60 seconds (post-restart)
**Recover window target:** 5 minutes

### Symptom
- Bot process crashed, was force-killed, or X-button closed while a dynamic exit row was in `TRANSMITTING` state.
- On restart, `bucket3_dynamic_exit_log` has one or more rows with `final_status = 'TRANSMITTING'`.
- Operator receives startup orphan scan alert: `Startup orphan scan complete`.
- Possible IBKR-side states: order filled, partially filled, still working, cancelled, never transmitted.

### Detect
The Followup #17 orphan scan runs automatically in `post_init` (telegram_bot.py `_scan_orphaned_transmitting_rows`) and consolidates findings into a single Telegram alert.

Log patterns:
```
grep "orphan_scan: found" logs/*.log
grep "orphan_scan:.*NOT FOUND at IBKR" logs/*.log
grep "orphan_scan:.*-> TRANSMITTED" logs/*.log
grep "orphan_scan:.*-> ABANDONED" logs/*.log
```

Alert categories:
- **Auto-resolved -- filled-via-openTrades** -- local row flipped to `TRANSMITTED`, no operator action needed.
- **Auto-resolved -- filled-via-executions** -- same.
- **Auto-resolved -- dead-at-ibkr (Cancelled/ApiCancelled/Inactive)** -- local row flipped to `ABANDONED`, no operator action needed.
- **NEEDS OPERATOR REVIEW -- live-unfilled** -- IBKR still has a working order, operator must decide whether to let it fill or cancel in TWS.
- **NEEDS OPERATOR REVIEW -- partial-fill** -- operator must decide per-leg.
- **NEEDS OPERATOR REVIEW -- not-found-at-ib** -- cross-midnight case OR order never reached IBKR. Manual verification in TWS required. **D5 binding: never auto-abandon on not-found.**
- **NEEDS OPERATOR REVIEW -- unknown-status** -- defensive category, should not occur in practice.

### Contain
1. Do NOT restart the bot again. Orphan scan already ran.
2. Do NOT manually edit `bucket3_dynamic_exit_log` rows.
3. Open TWS (or Gateway web portal) to verify the actual order state for each `NEEDS OPERATOR REVIEW` row.

### Recover

**For `live-unfilled` rows:**
1. In TWS, locate the working order by `orderRef` (= `audit_id` prefix).
2. Decide: let it fill at the existing limit, or cancel in TWS.
3. If you let it fill: wait for fill, then run `/recover_transmitting <audit_id> filled <ib_order_id>` (look up ib_order_id in TWS order history).
4. If you cancel in TWS: run `/recover_transmitting <audit_id> abandoned`.

**For `partial-fill` rows:**
1. In TWS, verify total filled quantity vs row's intended quantity.
2. If the remaining quantity is now filled or cancelled, run `/recover_transmitting <audit_id> filled <ib_order_id>`.
3. If still partially working, decide whether to cancel the remainder in TWS first, then recover.
4. **Known gap:** `/recover_transmitting` does not currently record partial-fill quantities separately; the row is marked TRANSMITTED at the audit-trail level regardless. Post-paper enhancement (track in followup queue if this occurs in practice).

**For `not-found-at-ib` rows (cross-midnight case):**
1. This is the **Gateway since-midnight limitation** -- `executions()` on Gateway only returns same-day fills. A row transmitted yesterday that crashed cannot be auto-resolved by the scan.
2. In TWS, open the **order history** (not just working orders) for the relevant account.
3. Filter by date range covering when the row was transmitted.
4. Search for the `orderRef` field matching the `audit_id`.
5. If found and filled: `/recover_transmitting <audit_id> filled <ib_order_id>`.
6. If found and cancelled/expired: `/recover_transmitting <audit_id> abandoned`.
7. If not found in TWS order history: the order never reached IBKR (crash happened before or during `placeOrder`). Safe to run `/recover_transmitting <audit_id> abandoned`.

**For `not-found-at-ib` rows (same-day case):**
- Same procedure as cross-midnight, but you can also check TWS working orders directly since the order is guaranteed to be either same-day filled, same-day cancelled, or never-transmitted.

### Verify
```sql
-- Confirm no TRANSMITTING rows remain after recovery
SELECT COUNT(*) FROM bucket3_dynamic_exit_log WHERE final_status = 'TRANSMITTING';
-- Expected: 0

-- Confirm recovery_audit_log entry exists for each manually recovered row
SELECT audit_id, operator_user_id, recovery_action, pre_status, post_status, created_at
FROM recovery_audit_log
ORDER BY created_at DESC LIMIT 10;
```

In Telegram: `/orders` to confirm no stale working orders remain that shouldn't be there.

### Post-mortem fields
- Crash cause (X-button, OOM, power loss, segfault, kill -9)
- Number of TRANSMITTING rows found at startup
- Auto-resolved count
- Manual-review count (broken down by category)
- For each manually recovered row: audit_id, final disposition, ib_order_id if applicable, time from crash to recovery
- Whether Windows Defender / Search Indexer exclusions were active at time of crash (PRE_PAPER_CHECKLIST items)

### Repro Steps (drill)
1. Stage a dynamic exit row via Cure Console, attest it, transmit it.
2. While the row is still `TRANSMITTING` (before Step 8 completes), kill the bot process with Task Manager -- End Task on `python.exe`.
3. Do NOT let post_shutdown fire. This simulates an uncontrolled crash.
4. Restart via `boot_desk.bat`.
5. Expected: Telegram alert `Startup orphan scan complete` fires within 30s of boot.
6. Verify the row was either auto-resolved (if IBKR filled it) or listed as `NEEDS OPERATOR REVIEW`.
7. If manual review: run `/recover_transmitting` with the correct disposition.
8. Verify `bucket3_dynamic_exit_log` has no `TRANSMITTING` rows and `recovery_audit_log` has the recovery entry.
9. **Cross-midnight variant:** repeat steps 1-2, wait until past midnight ET, restart, verify the row shows as `not-found-at-ib` (because Gateway executions since-midnight returns empty for the prior day), and manually recover via TWS order history lookup.

### Known gaps
- Partial-fill quantity tracking is not granular in `/recover_transmitting`; the row flips to TRANSMITTED regardless of fill completeness. Post-paper followup if observed.
- No automated cross-check between `/recover_transmitting` input and actual TWS state -- trusts operator per D6.
- Cross-midnight Gateway limitation is locked Option B (manual verification required). Defense in depth via Flex Web Services reconciliation is Followup #19, post-paper.

---

## DR-03 — IBKR Disconnect Mid-Stage (Pre-Transmit)

**Severity:** HIGH
**Detect window target:** 60 seconds
**Recover window target:** 5 minutes

### Symptom
- Telegram sends: `IBKR connection failed: ...` (line 6508)
- Telegram sends: `CRITICAL: IB Gateway disconnected. 5 reconnect attempts failed.` (line 1086)
- `/cc` or `/scan` commands fail with connection errors
- Dynamic exit staging fails with `LIVE_BID_FETCH_FAIL`

### Detect
Log patterns:
```
grep "IBKR_CONNECT_FAIL" logs/*.log
grep "LIVE_BID_FETCH_FAIL" logs/*.log
grep "Could not connect" logs/*.log
grep "IB Gateway disconnected" logs/*.log
```

The bot auto-detects via `disconnectedEvent` handler (telegram_bot.py:1129) which triggers `_schedule_reconnect()` — 5 retry attempts at 60-second intervals across ports 4001 (Gateway) and 4002 (TWS).

### Contain
1. Check TWS/Gateway process is running on the host machine
2. If Gateway/TWS is down: restart it via IB desktop application
3. Do NOT restart the bot yet — let the auto-reconnect handler attempt recovery

### Recover
1. **If auto-reconnect succeeds:**
   - Operator receives: `IB Gateway reconnected (attempt N/5).` (line 1072)
   - No further action needed — bot resumes normal operation
   - Expected: subsequent `/cc` commands succeed

2. **If auto-reconnect fails (all 5 attempts exhausted):**
   - Operator receives CRITICAL alert (line 1086)
   - In Telegram, run: `/reconnect`
   - Expected: bot re-establishes connection
   - If `/reconnect` fails: restart Gateway/TWS, wait 30s, then `/reconnect` again

3. **If any STAGED rows expired during disconnect:**
   - The 60s sweeper (telegram_bot.py:10858) auto-transitions STAGED rows to ABANDONED after 15 minutes
   - These rows can be re-staged via Cure Console or `/dynamic_exit` after reconnection
   - No manual intervention needed

### Verify
```
/reconnect
# Expected: "Connected via Gateway — accounts: [U21971297, ...]"

/health
# Expected: no error, portfolio data returns
```

### Post-mortem fields
- Timestamp of disconnect
- Duration of outage
- Reconnect attempt count (1-5 or failed)
- Number of STAGED rows that expired to ABANDONED during outage
- Gateway/TWS process state at time of disconnect
- Network connectivity state

### Repro Steps (drill)
1. Ensure bot is connected: send `/health` and confirm response
2. Kill the IB Gateway process (Task Manager → End Task on `ibgateway.exe`)
3. Wait 60 seconds — observe auto-reconnect attempts in bot logs
4. Expected: `_schedule_reconnect` fires, 5 attempts logged
5. Restart IB Gateway
6. Send `/reconnect` in Telegram
7. Verify: `/health` returns portfolio data
8. Note: ATTESTED rows continue to exist during disconnect — the 10s poller will re-deliver keyboards once connection is restored

### Known gaps
- No alternative alerting channel if Telegram is also down during disconnect
- No monitoring for partial connectivity (connected but data feeds stale)
- No automated health check that verifies data freshness post-reconnect

---

## DR-04 — IBKR Disconnect Post-Transmit, Pre-Ack

**Severity:** CRITICAL
**Detect window target:** 60 seconds
**Recover window target:** 5 minutes

### Symptom
One of three scenarios:

**Scenario A -- Step 8 DB write failed:**
- `placeOrder` succeeded (IBKR has the order).
- Local `UPDATE bucket3_dynamic_exit_log SET final_status='TRANSMITTED'` failed (DB error, lock timeout, etc.).
- Operator receives: `TRANSMIT RECOVERY REQUIRED\n<ticker> <audit_id>... may be live at IBKR, but local TRANSMITTED write failed.`
- Row remains in `TRANSMITTING` state with possible live order at IBKR.

**Scenario B -- IBKR disconnect during or immediately after placeOrder:**
- Network or Gateway failure between transmit and order acknowledgment.
- `disconnectedEvent` fires -- `_schedule_reconnect` -- `_auto_reconnect`.
- Followup #17 Part C.5: on successful reconnect, orphan scan runs automatically and reconciles `TRANSMITTING` rows.
- Followup #23: if the reconnect event carries IBKR error 1101 (data lost), `_handle_1101_data_lost` also triggers the orphan scan path.

**Scenario C -- bot process crash during transmit (not disconnect):**
- Same as DR-02. Restart -- orphan scan in post_init.

### Detect
Log patterns:
```
grep "TRANSMIT_STEP8_FAILED" logs/*.log
grep "TRANSMIT_STEP8_CAS_LOST" logs/*.log
grep "TRANSMIT RECOVERY REQUIRED" logs/*.log
grep "IBKR 1101: data lost" logs/*.log
grep "_handle_1101" logs/*.log
grep "orphan_scan" logs/*.log
```

Telegram alerts to watch for:
- `TRANSMIT RECOVERY REQUIRED` (Scenario A)
- `IBKR 1101: data lost. Reconciliation in progress.` (Scenario B with 1101)
- `IB Gateway reconnected (attempt N/5).` followed by `Startup orphan scan complete` (Scenario B with auto-reconnect)
- `1101 recovery complete.` (Scenario B with 1101 edge-case branch where reconciliation ran inline)
- `1101 recovery FAILED: ... MANUAL REVIEW REQUIRED.` (worst case -- fail-closed alert fired but automatic recovery did not complete)

### Contain
1. Do NOT manually transmit the same row again -- the order may already be live at IBKR.
2. Do NOT force-restart the bot unless Scenario A requires it (see Recover step 1 below).
3. Open TWS and check the relevant account's working orders + recent order history.

### Recover

**Scenario A (Step 8 DB write failed):**
1. In TWS, search working orders and recent order history by `orderRef = <audit_id>`.
2. If found and filled: `/recover_transmitting <audit_id> filled <ib_order_id>`.
3. If found and still working: decide whether to let it fill or cancel in TWS, then recover accordingly.
4. If found and cancelled: `/recover_transmitting <audit_id> abandoned`.
5. If not found in TWS: the placeOrder likely succeeded at the API level but the order was never acknowledged. Contact IBKR support with timestamps before abandoning. Do NOT recover until TWS confirms state.
6. Verify `recovery_audit_log` has the entry.

**Scenario B -- auto-reconnect succeeded + orphan scan ran:**
1. Read the orphan scan Telegram alert -- it lists what was auto-resolved and what needs manual review.
2. For auto-resolved rows: no action needed, verify in TWS if paranoid.
3. For `NEEDS OPERATOR REVIEW` rows: follow the DR-02 recovery procedure above (per category).

**Scenario B -- 1101 recovery FAILED alert fired:**
1. This means `_handle_1101_data_lost` was triggered without a preceding disconnect event (edge case), tried to re-fetch open orders + executions, and one of those steps failed.
2. The bot is still running and connected, but reconciliation did not complete.
3. Manually run `/reconnect` in Telegram. This cycles the IBKR connection and triggers `_auto_reconnect` -- full orphan scan path.
4. Read the resulting orphan scan alert and follow DR-02 procedure.
5. If `/reconnect` itself fails: restart the bot via Ctrl+C + `boot_desk.bat`. Startup orphan scan in post_init will run.

**Scenario B -- all 5 reconnect attempts failed:**
1. Operator receives: `CRITICAL: IB Gateway disconnected. 5 reconnect attempts failed.`
2. Check Gateway/TWS process on the host; restart the IB desktop application if needed.
3. In Telegram: `/reconnect`.
4. On successful reconnect, `_auto_reconnect` orphan scan will fire automatically.
5. If still failing: restart the bot (Ctrl+C + `boot_desk.bat`). post_init orphan scan handles reconciliation on the fresh connection.

**Scenario C (crash):** See DR-02.

### Verify
```sql
-- Confirm no stale TRANSMITTING rows
SELECT audit_id, ticker, final_status, last_updated
FROM bucket3_dynamic_exit_log
WHERE final_status = 'TRANSMITTING';
-- Expected: 0 rows

-- Confirm recovery audit trail for any manually recovered rows
SELECT audit_id, operator_user_id, recovery_action, pre_status, post_status,
       ib_order_id_provided, created_at
FROM recovery_audit_log
WHERE created_at > datetime('now', '-1 day')
ORDER BY created_at DESC;
```

In Telegram:
```
/orders
# Verify no unexpected working orders remain at IBKR

/health
# Verify connection is healthy and accounts list is complete
```

### Post-mortem fields
- Scenario (A / B with auto-recover / B with 1101 / B with failed recover / C)
- Time of disconnect or Step 8 failure
- Time of first recovery alert
- Time of final row disposition
- Number of rows affected
- Whether manual `/recover_transmitting` was needed
- Whether TWS order history lookup was needed
- Gateway/TWS process state during the incident
- IBKR error codes observed (1100/1101/1102)

### Repro Steps (drill)

**Scenario A drill (Step 8 DB failure):**
1. Stage, attest, and begin transmitting a dynamic exit row.
2. In a separate shell, hold an exclusive lock on `agt_desk.db` during Step 8 (use `sqlite3 agt_desk.db 'BEGIN IMMEDIATE'` and hold).
3. Expected: placeOrder succeeds at IBKR, Step 8 UPDATE fails with lock timeout, `TRANSMIT RECOVERY REQUIRED` alert fires.
4. Release the lock.
5. Follow Scenario A recovery procedure above.
6. Verify final state.

**Scenario B drill (disconnect + 1102):**
1. Stage, attest, transmit a row.
2. After confirming TRANSMITTED, disconnect the network interface briefly (< 30s).
3. Expected: `disconnectedEvent` fires, reconnect succeeds on first attempt, no orphan scan findings (row already TRANSMITTED).
4. Verify no false positives in logs.

**Scenario B drill (disconnect + 1101):**
1. Same as above but disconnect for > 60s to force IBKR to drop session state.
2. Expected: on reconnect, IBKR emits error 1101.
3. `_handle_1101_data_lost` should alert, defer to `_auto_reconnect` path (since `disconnectedEvent` already fired), and the orphan scan should run.
4. Verify Telegram receives both the 1101 critical alert AND the orphan scan completion alert.

**Scenario B drill (1101 without disconnect -- edge case):**
1. This is hard to simulate without a custom mock harness. Defer to unit test coverage (F23-3 tests 9 and 10) for this branch.

### Known gaps
- Scenario A cannot distinguish "placeOrder reached IBKR" from "placeOrder failed at network layer" without checking TWS. No automated differentiation.
- 1101 without a preceding disconnect event is an edge case covered by unit tests only, not by an end-to-end drill.
- Cross-midnight disconnect-during-transmit inherits the Gateway since-midnight limitation from DR-02.
- Flex Web Services reconciliation (Followup #19) would close the cross-midnight gap but is post-paper.

---

## DR-05 — Telegram API Outage (Bot Cannot Send/Receive)

**Severity:** MEDIUM
**Detect window target:** 10 minutes
**Recover window target:** 15 minutes

### Symptom
- Operator stops receiving messages from the bot
- No keyboard prompts for ATTESTED rows
- Mode transition alerts not received
- Bot process still running but Telegram sends fail silently

### Detect
Log patterns:
```
grep "attested_poller: failed to dispatch" logs/*.log
grep "attested_poller error" logs/*.log
grep "Mode transition push failed" logs/*.log
```

The poller (telegram_bot.py:10681) runs every 10s. Failed send attempts are logged per-row (line 10745) with `try/except` isolation — one failed row does not block others. The `_dispatched_audits` set does NOT add failed rows, so they retry on next tick.

ATTESTED rows continue ticking toward their 10-minute TTL during the outage. If Telegram is down for > 10 minutes, ATTESTED rows expire to ABANDONED via the sweeper.

### Contain
1. Check Telegram API status: https://downdetector.com/status/telegram/
2. Check bot process is still running: `tasklist | findstr python`
3. Do NOT restart the bot — it will re-establish Telegram polling automatically
4. If ATTESTED rows are at risk of TTL expiry: note their audit_ids from the log for potential re-staging

### Recover
1. **Wait for Telegram to recover.** The python-telegram-bot library handles reconnection automatically via its polling loop.
2. **After Telegram recovers:**
   - The poller re-delivers keyboards for any ATTESTED rows still alive
   - Failed rows that were not added to `_dispatched_audits` retry automatically
3. **If ATTESTED rows expired during outage:**
   - Re-stage via Cure Console or `/dynamic_exit` command
   - The sweeper transition (ATTESTED → ABANDONED) is logged: `ATTESTED_TTL_EXPIRED: audit_id=...` (rule_engine.py:1182)
4. **If bot process itself died during outage:**
   - Restart via `boot_desk.bat`
   - On restart, `_dispatched_audits` is empty — all surviving ATTESTED rows re-deliver keyboards

### Verify
Send a test message to the bot:
```
/mode
```

Check sweeper is running:
```
grep "attested_sweeper" logs/*.log | tail -5
# Expected: recent entries (within last 60 seconds)
```

### Post-mortem fields
- Telegram outage start/end timestamps
- Number of ATTESTED rows that expired to ABANDONED during outage
- Number of poller dispatch failures logged
- Whether any mode transitions were missed
- Whether operator received the alert via alternative channel (none currently)

### Repro Steps (drill)
1. Ensure bot has at least one ATTESTED row (stage via Cure Console, then attest)
2. Block Telegram API access: add a firewall rule blocking `api.telegram.org`
3. Wait 2 minutes — observe poller failures in bot log
4. Expected: `attested_poller: failed to dispatch audit_id=...` entries accumulate
5. Remove firewall rule
6. Wait 10 seconds — observe keyboard re-delivery attempt
7. Clean up: cancel the test ATTESTED row via Telegram CANCEL button
8. Note: this drill requires a live ATTESTED row, which means a live IBKR staging cycle

### Known gaps
- No alternative alerting channel (email, SMS, desktop notification) during Telegram outage
- No `/status` health endpoint that reports Telegram API connectivity
- ATTESTED TTL (10 min) may be too short for extended outages — operator may need to re-stage multiple rows
- No exponential backoff on Telegram send failures in the poller (retries every 10s regardless)

---

## DR-06 — CC Leg Partial Fill

**Severity:** HIGH
**Detect window target:** 60 seconds
**Recover window target:** 30 minutes

### Symptom
- IBKR TWS shows a CC order with status "PartiallyFilled"
- Bot logs show `PARTIALLY_FILLED` status for the order
- Only some contracts filled; remaining quantity still working
- Premium credited is less than expected (proportional to filled quantity)

### Detect
Log patterns:
```
grep "PARTIALLY_FILLED" logs/*.log
grep "remaining=" logs/*.log
grep "_on_cc_fill" logs/*.log
```

DB query to find partial fills:
```sql
SELECT id, payload, status, ib_order_id, fill_price, fill_qty
FROM pending_orders
WHERE status = 'partially_filled'
ORDER BY created_at DESC;
```

The R5 handler at telegram_bot.py:1868 detects partial fills:
```python
if new_status == OrderStatus.FILLED and remaining and float(remaining) > 0:
    new_status = OrderStatus.PARTIALLY_FILLED
```

### Contain
1. Check IBKR TWS → Trades tab → confirm order status and remaining quantity
2. Do NOT cancel the order unless instructed — remaining contracts may fill
3. Note the IB order ID and perm ID for reconciliation

### Recover
**Worked example — ADBE CC partial fill:**

Scenario: Operator stages 2x ADBE $440C 2026-05-16 CC at $4.50 limit. Order sent to IBKR.
1 contract fills at $4.50. 1 contract remains working.

1. **Monitor remaining fill:**
   - IBKR reports: `filled=1.0, remaining=1.0, status=PartiallyFilled`
   - Bot logs: `CC premium: Yash_Household ADBE +$450.00 (1 contracts @ $4.50)`
   - Premium ledger credited: $450.00 (1 × $4.50 × 100)
   - Expected total if fully filled: $900.00

2. **If remaining quantity fills normally:**
   - Second `execDetailsEvent` fires → `_on_cc_fill` credits another $450.00
   - `pending_orders.status` transitions from `partially_filled` to `filled`
   - No operator action needed

3. **If remaining quantity does NOT fill by EOD:**
   - DAY orders expire at market close (all orders use `tif="DAY"`)
   - IBKR cancels the remaining quantity
   - `orderStatusEvent` fires with status `Cancelled`
   - Premium ledger has $450.00 (only the filled portion)
   - Operator decides: re-stage the remaining 1 contract tomorrow, or accept partial

4. **Verify premium ledger accuracy:**
   ```sql
   SELECT household_id, ticker, total_premium_collected
   FROM premium_ledger
   WHERE household_id = 'Yash_Household' AND ticker = 'ADBE';
   -- Expected: total_premium_collected increased by $450.00
   ```

5. **Reconcile at EOD via Flex sync:**
   - `flex_sync.py` runs at 5:00 PM ET and mirrors IBKR Flex data to `master_log_trades`
   - `/reconcile` cross-checks Walker realized P&L against IBKR

### Verify
```sql
-- Check fill log for dedup
SELECT exec_id, ticker, action, quantity, price, premium_delta
FROM fill_log
WHERE ticker = 'ADBE' AND action = 'SELL_CALL'
ORDER BY rowid DESC LIMIT 5;
-- Expected: one row per execution, no duplicates (INSERT OR IGNORE dedup)

-- Check orphan events (fills that couldn't match to pending_orders)
SELECT * FROM orphan_order_events
WHERE status LIKE '%Fill%' OR event_type = 'execDetails'
ORDER BY received_at DESC LIMIT 10;
```

### Post-mortem fields
- IB order ID and perm ID
- Total contracts staged vs filled vs remaining
- Fill timestamps for each leg
- Premium credited vs expected
- Whether remaining quantity filled, expired, or was manually cancelled
- Flex sync reconciliation result

### Repro Steps (drill)
1. This scenario cannot be fully simulated without a live IBKR order
2. Manual walk-through: insert a mock `pending_orders` row with `status='partially_filled'`
3. Verify the R5 handler logic by tracing the code path at telegram_bot.py:1867-1869
4. Verify `_apply_fill_atomically` dedup by calling it twice with the same `exec_id`
5. Expected: second call returns `False` (duplicate suppressed via INSERT OR IGNORE)

### Known gaps
- No explicit "spread completeness" check — system processes individual legs independently
- `bucket3_dynamic_exit_log` has `fill_ts` and `fill_price` but no `fill_qty` or `remaining_qty` columns (Followup #17 adds `fill_qty`)
- No alert for "fill expected but not received within N minutes post-transmit"
- `TRANSMITTED` status in `bucket3_dynamic_exit_log` is set immediately after `placeOrder()` returns, NOT after fill confirmation

---

## DR-07 — Duplicate Transmit Race (CAS Catches — Runbook Post-Event)

**Severity:** MEDIUM
**Detect window target:** 0 seconds (automatic — CAS prevents double-execution)
**Recover window target:** 0 minutes (automatic — no recovery needed)

### Symptom
- Operator taps TRANSMIT button twice quickly on the same ATTESTED row
- First tap succeeds: row transitions ATTESTED → TRANSMITTING → TRANSMITTED
- Second tap: operator sees `Race: row already claimed by another process.` message
- Log shows `TRANSMIT_RACE_LOST: audit_id=... expected_status=ATTESTED`

### Detect
Log patterns:
```
grep "TRANSMIT_RACE_LOST" logs/*.log
grep "CANCEL_RACE_LOST" logs/*.log
```

These are INFO/WARNING level — they indicate the CAS guard worked correctly. No action required unless frequency is unusually high (> 5 per day suggests UI responsiveness issue).

### Contain
No containment needed. The CAS guard at telegram_bot.py:6600-6610 atomically prevents double-execution:
```sql
UPDATE bucket3_dynamic_exit_log
SET final_status = 'TRANSMITTING', last_updated = CURRENT_TIMESTAMP
WHERE audit_id = ? AND final_status = 'ATTESTED'
-- Second caller sees rowcount=0 and exits early
```

### Recover
No recovery needed. This is a normal operational event, not an error. The system handled it correctly.

**Worked example — ADBE double-tap race:**

Scenario: ADBE $440C 2026-05-16 CC is ATTESTED at $4.50 limit. Operator double-taps TRANSMIT.

Timeline:
```
T+0.000s: Tap 1 arrives. Handler reads row: final_status='ATTESTED'
T+0.001s: Tap 1 CAS UPDATE: SET final_status='TRANSMITTING' WHERE audit_id='a1b2c3d4-...' AND final_status='ATTESTED'
           → rowcount=1. Lock acquired.
T+0.050s: Tap 2 arrives. Handler reads row: final_status='TRANSMITTING' (already changed)
T+0.051s: Tap 2 CAS UPDATE: SET final_status='TRANSMITTING' WHERE audit_id='a1b2c3d4-...' AND final_status='ATTESTED'
           → rowcount=0. TRANSMIT_RACE_LOST logged.
T+0.052s: Tap 2 sends: "Race: row already claimed by another process."
T+0.100s: Tap 1 calls placeOrder() → order sent to IBKR
T+0.200s: Tap 1 CAS UPDATE: SET final_status='TRANSMITTED' WHERE final_status='TRANSMITTING'
           → rowcount=1. Order confirmed.
```

Result: exactly 1 order placed. Zero duplicates. CAS prevents the race at the database level.

### Verify
```sql
-- Verify only one order was placed for the audit_id
SELECT audit_id, final_status, transmitted, transmitted_ts
FROM bucket3_dynamic_exit_log
WHERE audit_id = '<the audit_id>';
-- Expected: final_status='TRANSMITTED', transmitted=1

-- Check for duplicate orders at IBKR (manual TWS check)
-- Open TWS → Trades → filter by ticker and time
-- Expected: exactly 1 order matching the audit
```

### Post-mortem fields
- audit_id of the raced row
- Timestamps of both taps (from log)
- Which tap won (first or second)
- Whether the losing tap sent any confusing message to the operator
- IB order ID from the winning tap

### Repro Steps (drill)
1. Create an in-memory SQLite DB with `bucket3_dynamic_exit_log` schema
2. Insert an ATTESTED row with known audit_id
3. Execute the CAS UPDATE twice against the same row:
   ```python
   r1 = conn.execute("UPDATE ... SET final_status='TRANSMITTING' WHERE audit_id=? AND final_status='ATTESTED'", (aid,))
   assert r1.rowcount == 1  # First tap wins
   r2 = conn.execute("UPDATE ... SET final_status='TRANSMITTING' WHERE audit_id=? AND final_status='ATTESTED'", (aid,))
   assert r2.rowcount == 0  # Second tap sees TRANSMIT_RACE_LOST
   ```
4. This is already tested in `test_phase3a5c2_beta_impl3.py::TestTransmitIdempotency`

### Known gaps
- No post-event audit trail beyond the log line (no structured record of which process won the race)
- CANCEL vs TRANSMIT race is also CAS-guarded (same pattern) but the operator may be confused by the "Cancel race" message if they intended to cancel but transmit won
- No rate-limiting on button taps — rapid tapping generates multiple log entries

---

## DR-08 — Clock Skew / TZ Drift Affecting GTC Order Lifecycle

**Severity:** MEDIUM
**Detect window target:** 15 minutes
**Recover window target:** 5 minutes

### Symptom
- STAGED rows expire too early or too late (15-min TTL miscalculated)
- ATTESTED rows expire unexpectedly (10-min TTL drift)
- Market hours check (9:30-9:45 ET delayed-data window) triggers at wrong time
- In-memory cache ages compute incorrectly (e.g., conviction cache expires prematurely)

### Detect
**System clock check:**
```bash
# Compare system time to NTP
w32tm /query /status
# Or:
python -c "import time; print('System epoch:', time.time()); import datetime; print('System time:', datetime.datetime.now()); print('UTC:', datetime.datetime.now(datetime.timezone.utc))"
```

**TTL computation check:**
```sql
-- Check STAGED rows with suspicious age
SELECT audit_id, ticker, staged_ts, 
       (strftime('%s','now') - staged_ts) / 60.0 AS age_minutes
FROM bucket3_dynamic_exit_log
WHERE final_status = 'STAGED'
ORDER BY staged_ts;
-- Expected: age_minutes < 15 for all STAGED rows
-- If age_minutes > 15 and row is still STAGED: sweeper clock issue

-- Check ATTESTED rows
SELECT audit_id, ticker, last_updated,
       (julianday('now') - julianday(last_updated)) * 24 * 60 AS age_minutes
FROM bucket3_dynamic_exit_log
WHERE final_status = 'ATTESTED';
-- Expected: age_minutes < 10 for all ATTESTED rows
```

The system uses two different time sources:
- `staged_ts`: Python `time.time()` (epoch seconds) — used for STAGED TTL
- `last_updated`: SQLite `CURRENT_TIMESTAMP` (UTC) — used for ATTESTED TTL

Both are vulnerable to system clock drift but in different ways.

### Contain
1. Do NOT change the system clock while the bot is running
2. If clock was recently changed (DST manual adjustment, NTP sync jump): restart the bot to reset in-memory caches

### Recover
1. **Sync system clock:**
   ```bash
   w32tm /resync
   # Or: net time /set (requires admin)
   ```

2. **Restart bot to clear in-memory caches:**
   ```bash
   boot_desk.bat
   ```
   On restart, all in-memory caches (conviction, spot price, chain data) are reset. `_dispatched_audits` set is emptied. Fresh `time.time()` calls use the corrected clock.

3. **If STAGED/ATTESTED rows have incorrect ages:**
   - The sweeper runs every 60 seconds and uses `time.time()` for STAGED and `datetime('now')` for ATTESTED
   - After clock correction, the sweeper self-corrects on the next tick
   - Rows that should have expired will expire; rows that shouldn't will survive

### Verify
```bash
# Verify clock is synced
python -c "import time; print('Epoch:', time.time())"
# Compare with https://time.is/ — should be within 1 second

# Verify sweeper is running with correct time
grep "attested_sweeper" logs/*.log | tail -3
```

### Post-mortem fields
- System clock offset at time of detection (seconds behind/ahead)
- Cause of drift (manual change, NTP failure, VM suspend/resume, DST transition)
- Number of rows affected (expired too early or stayed too long)
- Duration of clock skew

### Repro Steps (drill)
1. Note current system time
2. Create a test Python script that computes STAGED TTL:
   ```python
   import time
   staged_ts = time.time()
   # Simulate 16 minutes passing
   fake_now = staged_ts + (16 * 60)
   age_seconds = fake_now - staged_ts
   assert age_seconds > 900, "Should exceed 15-min TTL"
   print(f"Age: {age_seconds/60:.1f} minutes — sweeper would expire this row")
   ```
3. Verify the sweeper correctly compares `time.time()` against `staged_ts`
4. Note: do NOT change the actual system clock during this drill

### Known gaps
- No NTP sync check or system clock validation on startup
- Mixed naive/aware datetimes: `_datetime.now()` (naive) used in ~25 locations for caches; `_datetime.now(ET)` (aware) for market hours. No systematic enforcement
- No monitoring for clock jumps (e.g., VM suspend/resume causing time.time() to jump)
- `staged_ts` uses Python `time.time()` but `last_updated` uses SQLite `CURRENT_TIMESTAMP` — different clock sources could diverge if SQLite and Python disagree
- All dynamic exit orders are DAY (not GTC), so clock skew affecting GTC lifecycle is not applicable — the primary risk is TTL miscalculation

---

## Known Gaps Master List

| ID | Gap | Source | Severity | Proposed fix |
|----|-----|--------|----------|-------------|
| G1 | No PRAGMA integrity_check on startup | DR-01 | MEDIUM | Add to init_db() |
| G2 | No WAL file size monitoring | DR-01 | LOW | Scheduled health check |
| G3 | No automated failover to baseline backup | DR-01 | MEDIUM | Startup script enhancement |
| G4 | No alternative alerting channel | DR-03, DR-05 | HIGH | Email/SMS fallback |
| G5 | No data freshness check post-reconnect | DR-03 | LOW | Health check enhancement |
| G6 | No spread completeness check for partial fills | DR-06 | MEDIUM | Followup #17 scope |
| G7 | TRANSMITTED != filled (set before fill confirmation) | DR-06 | HIGH | Followup #17 R5 handler patch |
| G8 | No fill timeout alert | DR-06 | MEDIUM | Post-paper |
| G9 | No structured race audit trail | DR-07 | LOW | recovery_audit_log (Followup #17) |
| G10 | No NTP sync check on startup | DR-08 | LOW | Pre-paper checklist item |
| G11 | Mixed naive/aware datetimes | DR-08 | LOW | Post-paper cleanup |
| G12 | ~~DR-02 and DR-04 depend on Followup #17~~ | DR-02, DR-04 | ~~CRITICAL~~ CLOSED | Followup #17 shipped, DR-02/DR-04 filled (F23) |
| G13 | Automated DR drill scripts | All | MEDIUM | Followup #16 (proposed) |

---

*End of DR Runbook v1.*
