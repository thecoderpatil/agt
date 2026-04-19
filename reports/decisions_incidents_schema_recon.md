# decisions + incidents Schema Recon — MR-C.1 Unblock
**Date:** 2026-04-19  
**Worktree tip at query time:** 835ef4d (worktree stale vs dispatch-stated 747e05c — read-only recon unaffected)  
**DB:** `C:\AGT_Telegram_Bridge\agt_desk.db`

---

## decisions schema

```sql
CREATE TABLE decisions (
    decision_id           TEXT PRIMARY KEY,
    engine                TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    decision_timestamp    TIMESTAMP NOT NULL,
    raw_input_hash        TEXT NOT NULL,
    llm_reasoning_text    TEXT,
    llm_confidence_score  REAL,
    llm_rank              INTEGER,
    operator_action       TEXT NOT NULL,
    action_timestamp      TIMESTAMP NOT NULL,
    strike                REAL,
    expiry                DATE,
    contracts             INTEGER,
    premium_collected     REAL,
    realized_pnl          REAL,
    realized_pnl_timestamp TIMESTAMP,
    counterfactual_pnl    REAL,
    counterfactual_basis  TEXT,
    market_state_embedding BLOB,
    operator_credibility_at_decision REAL,
    prompt_version        TEXT NOT NULL,
    notes                 TEXT
)
```

---

## incidents schema

```sql
CREATE TABLE incidents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_key         TEXT NOT NULL,
    invariant_id         TEXT,
    severity             TEXT NOT NULL,
    scrutiny_tier        TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open',
    detector             TEXT NOT NULL,
    detected_at          TEXT NOT NULL,
    closed_at            TEXT,
    last_action_at       TEXT,
    consecutive_breaches INTEGER NOT NULL DEFAULT 1,
    observed_state       TEXT,
    desired_state        TEXT,
    confidence           REAL,
    mr_iid               INTEGER,
    ddiff_url            TEXT,
    rejection_history    TEXT,
    fault_source         TEXT NOT NULL DEFAULT 'internal',
    severity_tier        INTEGER NOT NULL DEFAULT 1,
    burn_weight          REAL NOT NULL DEFAULT 10
)
```

**Note:** `fault_source`, `severity_tier`, `burn_weight` were added by Dispatch B (MR !139/!141) as ALTER TABLE additions — they appear as trailing columns in the DDL (appended after the original CREATE TABLE body, separated by `, ` continuation).

---

## decisions sample rows (last 5, PII-scrubbed)

```
(empty — 0 rows in table)
```

Table exists and schema is intact, but no decision records have been written yet (engine has not run a full LLM-backed decision cycle in production).

---

## incidents sample rows (last 5, PII-scrubbed)

| id | incident_key | invariant_id | severity | scrutiny_tier | status | detector | detected_at | fault_source | severity_tier | burn_weight |
|----|-------------|-------------|---------|--------------|--------|---------|------------|-------------|--------------|------------|
| 461 | NO_MISSING_DAEMON_HEARTBEAT:agt_bot | NO_MISSING_DAEMON_HEARTBEAT | high | medium | resolved | agt_scheduler.heartbeat | 2026-04-18T10:52:35+00:00 | vendor | 2 | 1.0 |
| 460 | NO_BELOW_BASIS_CC:7bd1da523939 | NO_BELOW_BASIS_CC | high | low | resolved | agt_scheduler.heartbeat | 2026-04-18T01:23:29+00:00 | internal | 1 | 10.0 |
| 459 | NO_LIVE_IN_PAPER:d89d24a01f22 | NO_LIVE_IN_PAPER | critical | architect_only | resolved | agt_scheduler.heartbeat | 2026-04-18T01:23:29+00:00 | internal | 1 | 10.0 |
| 458 | NO_MISSING_DAEMON_HEARTBEAT:3a12489980ef | NO_MISSING_DAEMON_HEARTBEAT | high | medium | resolved | agt_scheduler.heartbeat | 2026-04-17T19:19:42+00:00 | vendor | 2 | 1.0 |
| 457 | NO_MISSING_DAEMON_HEARTBEAT:d64c00965fcf | NO_MISSING_DAEMON_HEARTBEAT | high | medium | resolved | agt_scheduler.heartbeat | 2026-04-17T19:18:43+00:00 | vendor | 2 | 1.0 |

Observed states are JSON blobs (truncated in display). `desired_state`, `confidence`, `mr_iid`, `ddiff_url`, `rejection_history` are NULL for all 5 shown rows.

---

## column counts + indices

### Column counts
| table | columns |
|-------|---------|
| decisions | 22 |
| incidents | 20 |

### decisions indices
```sql
CREATE INDEX idx_decisions_engine_ts ON decisions(engine, decision_timestamp DESC)
CREATE INDEX idx_decisions_ticker_ts ON decisions(ticker, decision_timestamp DESC)
CREATE INDEX idx_decisions_pending_pnl ON decisions(realized_pnl) WHERE realized_pnl IS NULL
```
(No additional implicit indices beyond the PRIMARY KEY on `decision_id`)

### incidents indices
```sql
CREATE UNIQUE INDEX idx_incidents_active_key
    ON incidents(incident_key)
    WHERE status NOT IN ('merged','resolved','rejected_permanently')

CREATE INDEX idx_incidents_status ON incidents(status)
CREATE INDEX idx_incidents_invariant_id ON incidents(invariant_id)
CREATE INDEX idx_incidents_mr_iid ON incidents(mr_iid)
```

### Full column detail — decisions (22 columns)
| cid | name | type | notnull | default | pk |
|-----|------|------|---------|---------|-----|
| 0 | decision_id | TEXT | 0 | None | 1 |
| 1 | engine | TEXT | 1 | None | 0 |
| 2 | ticker | TEXT | 1 | None | 0 |
| 3 | decision_timestamp | TIMESTAMP | 1 | None | 0 |
| 4 | raw_input_hash | TEXT | 1 | None | 0 |
| 5 | llm_reasoning_text | TEXT | 0 | None | 0 |
| 6 | llm_confidence_score | REAL | 0 | None | 0 |
| 7 | llm_rank | INTEGER | 0 | None | 0 |
| 8 | operator_action | TEXT | 1 | None | 0 |
| 9 | action_timestamp | TIMESTAMP | 1 | None | 0 |
| 10 | strike | REAL | 0 | None | 0 |
| 11 | expiry | DATE | 0 | None | 0 |
| 12 | contracts | INTEGER | 0 | None | 0 |
| 13 | premium_collected | REAL | 0 | None | 0 |
| 14 | realized_pnl | REAL | 0 | None | 0 |
| 15 | realized_pnl_timestamp | TIMESTAMP | 0 | None | 0 |
| 16 | counterfactual_pnl | REAL | 0 | None | 0 |
| 17 | counterfactual_basis | TEXT | 0 | None | 0 |
| 18 | market_state_embedding | BLOB | 0 | None | 0 |
| 19 | operator_credibility_at_decision | REAL | 0 | None | 0 |
| 20 | prompt_version | TEXT | 1 | None | 0 |
| 21 | notes | TEXT | 0 | None | 0 |

### Full column detail — incidents (20 columns)
| cid | name | type | notnull | default | pk |
|-----|------|------|---------|---------|-----|
| 0 | id | INTEGER | 0 | None | 1 |
| 1 | incident_key | TEXT | 1 | None | 0 |
| 2 | invariant_id | TEXT | 0 | None | 0 |
| 3 | severity | TEXT | 1 | None | 0 |
| 4 | scrutiny_tier | TEXT | 1 | None | 0 |
| 5 | status | TEXT | 1 | 'open' | 0 |
| 6 | detector | TEXT | 1 | None | 0 |
| 7 | detected_at | TEXT | 1 | None | 0 |
| 8 | closed_at | TEXT | 0 | None | 0 |
| 9 | last_action_at | TEXT | 0 | None | 0 |
| 10 | consecutive_breaches | INTEGER | 1 | 1 | 0 |
| 11 | observed_state | TEXT | 0 | None | 0 |
| 12 | desired_state | TEXT | 0 | None | 0 |
| 13 | confidence | REAL | 0 | None | 0 |
| 14 | mr_iid | INTEGER | 0 | None | 0 |
| 15 | ddiff_url | TEXT | 0 | None | 0 |
| 16 | rejection_history | TEXT | 0 | None | 0 |
| 17 | fault_source | TEXT | 1 | 'internal' | 0 |
| 18 | severity_tier | INTEGER | 1 | 1 | 0 |
| 19 | burn_weight | REAL | 1 | 10 | 0 |

---

## WAL mode check

```
journal_mode: wal
```

WAL active. Confirmed.

---

## Notes

### 1. decisions table is empty (0 rows)
No rows written yet. Table structure is clean and matches dispatch A spec. MR-C.1 paper_baseline.py adapter can INSERT freely — no migration needed, no existing data to worry about.

### 2. incidents dual-ledger columns confirmed present
`fault_source` (TEXT NOT NULL DEFAULT 'internal'), `severity_tier` (INTEGER NOT NULL DEFAULT 1), and `burn_weight` (REAL NOT NULL DEFAULT 10) are all present and NOT NULL with defaults. They were added via ALTER TABLE (visible as trailing comma-appended tokens in the raw DDL string). All 461 existing rows have values (defaults applied at ALTER TABLE time).

### 3. incident_key in sample rows — no account IDs embedded
Incident keys use colon-delimited format: `INVARIANT_ID:hash_suffix`. No raw IBKR account numbers observed in `incident_key` (hash-suffix format). The NO_LIVE_IN_PAPER row (id=459) has "U22076329" in `observed_state` JSON — this is IBKR account ID, not in the key itself.

### 4. incidents.detected_at / closed_at are TEXT, not TIMESTAMP
Unlike `decisions` where timestamps are typed TIMESTAMP, `incidents` stores datetimes as TEXT (ISO 8601 strings with timezone). MR-C.1 adapter must treat these as strings, not native datetime objects.

### 5. decisions.decision_id is TEXT PK (not AUTOINCREMENT)
Caller-supplied UUID/hash — not auto-generated. MR-C.1 must generate and pass `decision_id` explicitly.

### 6. No foreign key relationship between tables
decisions and incidents are independent tables. MR-C.1 can write to either without referential constraint.
