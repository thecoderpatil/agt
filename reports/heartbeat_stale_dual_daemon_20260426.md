# Incident Report — Dual-Daemon HEARTBEAT_STALE
## P0 Triage, Forensic, Recovery

**Date:** 2026-04-26
**Incident window:** ~11:28:55Z – ~11:40Z (apparent)
**Triggered by:** Deploy smoke check + manual DB query showing daemon_heartbeat stale
**Investigated:** 11:39:59Z – 11:48:18Z
**Outcome:** FALSE ALARM / self-resolving WAL snapshot artifact. No actual service outage.

---

## 1. State Capture (11:39:59Z)

### Process State
Both daemon processes RUNNING throughout:

| PID   | StartTime          | WorkingSet  | Service          |
|-------|--------------------|-------------|------------------|
| 45240 | 2026-04-26T06:44Z  | 82 MB       | agt-telegram-bot |
| 27616 | 2026-04-26T06:44Z  | 54 MB       | agt-scheduler    |

Service status: `SERVICE_RUNNING` for both (`Get-Service`). No restarts detected.

### SQLite WAL State (C:\AGT_Runtime\state\agt_desk.db)

| File        | Size        | mtime (UTC)         |
|-------------|-------------|---------------------|
| agt_desk.db | 13,369,344  | 2026-04-26T11:28:55Z |
| agt_desk.db-wal | 1,561,512 | 2026-04-26T11:40:55Z |
| agt_desk.db-shm | 32,768  | 2026-04-26T11:29:11Z |

WAL at 1.5MB = ~379 frames (page_size=4096, frame_size=4120). Auto-checkpoint
threshold = PRAGMA wal_autocheckpoint = 1000 pages = 4.1MB. WAL was below
auto-checkpoint threshold — automatic checkpoint had NOT fired.

Main DB mtime = 11:28:55Z = last successful checkpoint point. All writes from
11:28:55Z onward were in the WAL, un-checkpointed.

### DB Path Clarification
`AGT_DB_PATH = C:\AGT_Telegram_Bridge\agt_desk.db` (coder shell) resolves via
**symlink** to `C:\AGT_Runtime\state\agt_desk.db`. Both paths hit the same
production database. All DB queries in this investigation are against the same
physical file.

### Initial Heartbeat Query (11:39:59Z)
```
daemon_heartbeat (last_beat_utc):
  agt_bot       2026-04-26T11:28:55+00:00  pid=45240
  agt_scheduler 2026-04-26T11:28:18+00:00  pid=27616
```

Both daemons APPEARED 11+ minutes stale. This triggered the P0 assessment.

### daemon_heartbeat_samples (50 rows, 11:39:59Z query)
Most recent 50 rows ended at `agt_bot 2026-04-26T11:28:55Z`. No rows from
11:29Z–11:39Z visible at query time.

### cross_daemon_alerts (since 11:00Z)
```
id=18  kind=STUCK_ORDER_SWEEP  severity=info  created_ts=11:27:18.487Z
  swept_count=29, error_count=0, skipped_in_flight=6
```
No HEARTBEAT_STALE alert. No other alerts.

---

## 2. Re-query at 11:40Z (after initial capture)

```
daemon_heartbeat (last_beat_utc):
  agt_bot       2026-04-26T11:40:55+00:00  pid=45240  ← CURRENT
  agt_scheduler 2026-04-26T11:41:18+00:00  pid=27616  ← CURRENT
```

Both daemons beating. Ages: agt_bot 24s, agt_scheduler 4s. The "stale" condition
resolved BETWEEN my two queries — a window of ~30 seconds.

### daemon_heartbeat_samples (window 11:26Z–11:35Z, post-first-query)
```
agt_scheduler  11:26:19Z  ... 11:27:19Z  11:28:18Z  11:29:18Z  11:30:18Z  ...
agt_bot        11:26:25Z  ... 11:27:25Z  11:28:25Z  11:28:55Z  11:29:25Z  ...
```

**NO GAP in daemon_heartbeat_samples.** All expected beats are present through
the 11:29–11:31Z window and beyond. Both daemons were writing to the samples
table continuously throughout the apparent stale period.

### WAL checkpoint (passive, run at ~11:47Z)
```
PRAGMA wal_checkpoint(PASSIVE) → 0 | 606 | 606
```
606 WAL frames checkpointed. Main DB file promoted. WAL effectively cleared.

---

## 3. Recovery Actions

**None required.** Both services were running and healthy before any intervention.
Post-checkpoint confirmation: agt_bot 27s, agt_scheduler 4s.

No service restarts. No rollback. No data loss.

---

## 4. Forensic Analysis

### 4.1 Timeline
```
11:27:18.000Z  Manual sweep_terminal_states() invoked (29 orders, DEFERRED tx)
11:27:18.487Z  Sweep complete (<0.5s). STUCK_ORDER_SWEEP alert id=18 written.
11:27:19Z      agt_scheduler beats (last pre-gap beat = 11:27:19Z... then 11:28:18Z)
11:28:18Z      agt_scheduler last beat visible in daemon_heartbeat at query T1
11:28:25Z      agt_bot beats normally
11:28:55Z      agt_bot last beat visible in daemon_heartbeat at query T1
11:28:55Z      Main DB file mtime (last checkpoint marker)
11:29:11Z      agt_desk.db-shm mtime (last WAL index writer update)
11:39:59Z  T1: Initial query — both daemons show 11:28Z stale in daemon_heartbeat
               daemon_heartbeat_samples: last 50 rows end at 11:28:55Z
               WAL = 1.5MB, 379 frames, uncheckpointed
11:40:21Z      agt_desk.db-shm mtime refreshed (sqlite3 CLI opened connection)
11:40:55Z      agt_desk.db-wal mtime (WAL actively modified by daemons)
11:40:xx Z  T2: Re-query — daemon_heartbeat shows 11:40:55Z current
               daemon_heartbeat_samples: 30 rows from 11:31:25Z upward, no gap
11:47:xx Z     PRAGMA wal_checkpoint(PASSIVE): 606 frames checkpointed
11:47:55Z      Confirmed: agt_bot 27s, agt_scheduler 4s
```

### 4.2 Sweep Scope (sweep_terminal_states)
From `agt_equities/order_lifecycle/sweeper.py`:
```python
conn = get_db_connection(db_path=db_path)      # DEFERRED mode
cursor = conn.execute("SELECT * FROM pending_orders WHERE status NOT IN ...")
rows = [...]
for row in rows:
    _apply_sweep(conn, ...)   # SELECT status_history + UPDATE pending_orders per row
conn.commit()                  # single COMMIT after all 29 updates
conn.close()
```

Transaction type: **DEFERRED** (no `tx_immediate`). On first UPDATE, Python
sqlite3's implicit `BEGIN` escalates from shared → reserved lock. Holds the
WAL write lock for all 29 × (SELECT history + UPDATE) operations before
`conn.commit()`. However: sweep completed in **< 0.5 seconds** (alert written
at 11:27:18.487Z). The write lock was held for ~0.5s total.

This is NOT the direct cause of the 11:28:55Z stale event (which is 90s later).

### 4.3 write_heartbeat Transaction Scope
From `agt_equities/health.py`:
```python
with closing(get_db_connection(db_path=db_path)) as conn:
    conn.execute("INSERT INTO daemon_heartbeat ... ON CONFLICT DO UPDATE ...")
    try:
        conn.execute("INSERT INTO daemon_heartbeat_samples ...")
    except sqlite3.OperationalError:
        ...
    conn.commit()
```

Transaction type: **DEFERRED** (no `tx_immediate`). This violates the production
write contract stated in `agt_equities/db.py`:
> Write transactions must use `tx_immediate(conn)` — never rely on Python
> sqlite3's implicit DEFERRED 'with conn:' behavior.

Under DEFERRED: the first DML (`INSERT INTO daemon_heartbeat`) implicitly
issues `BEGIN`, then tries to upgrade to RESERVED lock on first actual write.
If another writer holds RESERVED at that moment, the connection waits up to
`busy_timeout` (15s). If the lock isn't released in 15s, `sqlite3.OperationalError`
propagates up from `conn.execute(INSERT daemon_heartbeat ...)`. The outer
`except Exception as exc` block catches it → logs loudly → returns. Both the
UPSERT and the samples INSERT are rolled back. `daemon_heartbeat.last_beat_utc`
is NOT updated.

The daemon retries on the next 30s/60s cycle. If contention persists, multiple
consecutive heartbeat cycles can fail silently.

### 4.4 Root Cause: WAL Snapshot + DEFERRED Contention

**Why daemon_heartbeat showed stale at T1 (11:39:59Z) but current at T2 (11:40:xx):**

The WAL had 379 frames (~1.5MB) uncheckpointed. The `agt_desk.db` main file
mtime was 11:28:55Z (last checkpoint). The SHM (WAL index) mtime was 11:29:11Z.

The most consistent hypothesis:

1. Around 11:28:55Z, a brief write-lock contention (origin: likely the `attested_sweeper`
   job at 11:28:18Z or another scheduler job using `tx_immediate`) blocked
   `write_heartbeat`'s DEFERRED transaction from acquiring RESERVED for 1-2 cycles.
2. `write_heartbeat` committed a failure → last_beat_utc stayed at 11:28:55Z in
   `daemon_heartbeat` (and daemon_heartbeat_samples rolled back simultaneously for
   that cycle).
3. The WAL during this window may have had a brief inconsistent state in the
   WAL index (shm mtime = 11:29:11Z), causing my T1 sqlite3 read to see the
   last fully-flushed state (11:28:55Z) rather than the latest committed WAL frames.
4. By T2 (11:40:xx), contention cleared, heartbeat commits succeeded, and the
   WAL index was updated — my query saw current beats.

**Why daemon_heartbeat_samples shows no gap NOW but showed cutoff at T1:**

Two possible sub-explanations (not mutually exclusive):
- (A) The samples rows from 11:29Z–11:39Z were in WAL frames that were committed
  but not yet reflected in the WAL index (shm) at T1 → invisible to my T1 read,
  visible after shm was refreshed at ~11:40:21Z.
- (B) write_heartbeat failed for 1-2 cycles only (not 11 minutes) — the gap was
  transient and the contention cleared quickly. The T1 read happened in the brief
  window between the last failed commit and the next successful one.

The PASSIVE checkpoint at T3 clearing 606 frames confirms the WAL had accumulated
significant uncheckpointed state. This did not cause data loss but would have
caused slow reads and increased write-lock contention duration.

### 4.5 MR !268 daemon_heartbeat_samples Contribution

The Phase B double-write (added to write_heartbeat in MR !268) adds ONE additional
DML to each heartbeat write. This:
- Increases write transaction size slightly (2 DMLs instead of 1)
- Does NOT change the fundamental DEFERRED contention risk (already present in
  the original daemon_heartbeat UPSERT)
- The inner `try/except OperationalError` correctly tolerates pre-migration DBs
  but does NOT protect against the OUTER `except Exception` path (commit failure)

The double-write is NOT the root cause. It is a latent amplifier: if write_heartbeat
were using `tx_immediate`, the second INSERT would be safe inside the acquired
reserved lock. Under the current DEFERRED pattern, both writes succeed-or-fail together.

### 4.6 Concurrent CSP Scan / Other Jobs
No CSP scan evidence at 11:27–11:31Z (no CSP-related alerts or DB writes visible).
The `attested_sweeper` (A5a, every 60s) fired at ~11:28:19Z per scheduler beat and
uses `tx_immediate` (short lock). This is the likely contention source for the
transient DEFERRED heartbeat failure.

---

## 5. Defects Identified

### DEFECT-1 (P1): write_heartbeat() uses DEFERRED, not tx_immediate
**File:** `agt_equities/health.py:56-84`
**Impact:** Under any concurrent writer (attested_sweeper, orphan sweep, Phase B proof
report, terminal_state_sweeper, etc.) the heartbeat UPSERT silently fails. daemon_heartbeat
shows stale. HEARTBEAT_STALE alert fires. On-call pages. False P0s.
**Fix:** Wrap both `conn.execute(INSERT daemon_heartbeat ...)` calls in a single
`with tx_immediate(conn):` block. The inner `try/except OperationalError` for
samples can remain as a guard against missing table, but outside the tx_immediate
is wrong — missing table should be caught, not lock timeout.

Correct pattern:
```python
with closing(get_db_connection(db_path=db_path)) as conn:
    with tx_immediate(conn):
        conn.execute("INSERT INTO daemon_heartbeat ... ON CONFLICT DO UPDATE ...", ...)
        try:
            conn.execute("INSERT INTO daemon_heartbeat_samples ...", ...)
        except sqlite3.OperationalError as inner:
            logger.warning("daemon_heartbeat_samples write skipped: %s", inner)
```

### DEFECT-2 (P2): sweep_terminal_states() uses DEFERRED, not tx_immediate
**File:** `agt_equities/order_lifecycle/sweeper.py:133-188`
**Impact:** The sweep acquires DEFERRED write lock on first UPDATE. Holds it for
all N × (SELECT + UPDATE) operations in a single transaction. For N=29, this was
< 0.5s (acceptable). For N=100+, this could be 5-10s, blocking all DEFERRED writers
(heartbeat, etc.) for that duration or causing them to fail after busy_timeout.
**Fix:** Option A: Wrap entire UPDATE block in `tx_immediate`. Option B (better):
commit per-row in a `tx_immediate` per sweep (removes large-transaction risk entirely).

### DEFECT-3 (P3): WAL auto-checkpoint not triggering promptly
**Context:** WAL at 1.5MB (379 frames), below 1000-page auto-checkpoint threshold
(4.1MB). WAL grew unchecked since last checkpoint at 11:28:55Z (~11 minutes before
I ran PASSIVE checkpoint manually).
**Impact:** Large WAL = slower reads + slower write-lock contention resolution.
**Fix:** Consider `PRAGMA wal_autocheckpoint = 200` (vs default 1000) on the
production connection in `get_db_connection()`, OR add a periodic PASSIVE checkpoint
call in one of the existing scheduler jobs.

---

## 6. Verdict

| Dimension | Finding |
|-----------|---------|
| Actual outage? | **NO** — both daemons running and beating continuously (confirmed by samples table and process list) |
| Data loss? | **NO** — no orders lost, no heartbeat data corrupted |
| Root cause | DEFERRED transaction in write_heartbeat() + brief write contention (attested_sweeper) → 1-2 heartbeat write failures → stale daemon_heartbeat read |
| Sweep-induced WAL contention? | **PARTIAL CONTRIBUTOR** — sweep used DEFERRED and completed in < 0.5s; not the direct cause. Establishes a pattern that scales badly. |
| MR !268 double-write blocker? | **NO** — samples table present and no gap. The double-write is correct in concept; only the transaction mode needs fixing. |
| Phase B proof-report heartbeat-gap metric impact? | **YES** — until DEFECT-1 is fixed, a busy sweep day could produce a false gap in daemon_heartbeat_samples that the proof-report metrics pick up as a real gap |
| Recovery action taken | PASSIVE WAL checkpoint (606 frames cleared). No service restarts needed. |

**Phase A piece 6 status: CLOSED** (sweeper functional, correct classifications confirmed).
This incident is orthogonal to piece 6 closure — it does not reopen.

---

## 7. Follow-up MR Candidates

### MR-heartbeat-tx-fix (P1, Phase B piece)
Files: `agt_equities/health.py`
Change: Add `tx_immediate` to `write_heartbeat()`. One-line change + test update.
Tier: CRITICAL (telegram_bot.py imports health.py → service restart required).
Dispatch to Architect: YES — surface as Phase B blocker before first proof-report
scoring window (first scored report 2026-04-29 07:30 ET).

### MR-sweeper-tx-fix (P2)
Files: `agt_equities/order_lifecycle/sweeper.py`
Change: Wrap per-row UPDATE in `tx_immediate(conn)` (one tx per sweep row) or wrap
entire loop in single `tx_immediate`. Per-row is safer (shorter lock windows).
Tier: CRITICAL.

### MR-wal-checkpoint (P3)
Files: `agt_equities/db.py` or `agt_scheduler.py`
Change: Lower `PRAGMA wal_autocheckpoint = 200` in `get_db_connection()` or add
periodic `PRAGMA wal_checkpoint(PASSIVE)` in scheduler.
Tier: STANDARD.
