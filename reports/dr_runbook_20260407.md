# Disaster Recovery Runbook

Generated: 2026-04-07
Updated: 2026-04-07 (R6 — Litestream deployment verified)

## Backup Architecture

### Active: Litestream → Cloudflare R2
- **Binary:** litestream.exe v0.5.10 (Windows x86_64)
- **Config:** litestream.yml (gitignored, contains R2 credentials)
- **Source:** C:\AGT_Telegram_Bridge\agt_desk.db
- **Target:** R2 bucket `agt-desk-backup` / path `agt-desk-db`
- **Endpoint:** Cloudflare R2 (credentials in .env)
- **Replication interval:** 10 seconds (continuous WAL streaming)
- **Retention:** 30 days point-in-time recovery
- **Verified restore time:** < 5 seconds for full DB (2.8 MB)

### Legacy: Phase 1 baseline
- `agt_desk.db.phase1_baseline_20260407` (pre-Phase-2 state, 921 KB)

## Restore Procedures

### Scenario 1: Bot crash, DB intact
```
boot_desk.bat
/reconcile
```

### Scenario 2: DB corruption, Litestream running
```
1. Stop bot
2. litestream.exe restore -config litestream.yml -o agt_desk_restored.db C:\AGT_Telegram_Bridge\agt_desk.db
3. move agt_desk.db agt_desk.db.corrupt
4. move agt_desk_restored.db agt_desk.db
5. boot_desk.bat
6. /reconcile
```

### Scenario 3: Full machine loss
```
1. Provision new Windows machine
2. Install Python 3.13 + dependencies (pip install -r requirements.txt)
3. Clone/copy repo to C:\AGT_Telegram_Bridge\
4. Create .env with all credentials
5. Download litestream.exe v0.5.10
6. litestream.exe restore -config litestream.yml -o agt_desk.db C:\AGT_Telegram_Bridge\agt_desk.db
7. boot_desk.bat
8. /reconcile
```

### Scenario 4: Restore drill (verification)
```
python restore_drill.py --from-r2
```

## Restore Drill Results (2026-04-07)

| Table | Live | Restored | Match |
|-------|------|----------|-------|
| pending_orders | 205 | 205 | MATCH |
| premium_ledger | 16 | 16 | MATCH |
| ticker_universe | 597 | 597 | MATCH |
| master_log_trades | 1,466 | 1,466 | MATCH |
| master_log_sync | 1 | 1 | MATCH |
| master_log_open_positions | 35 | 35 | MATCH |
| corp_action_quarantine | 0 | 0 | MATCH |

**Result: PASS — all 7 tables match exactly**

Restored file size: 2,834,432 bytes (vs live: 2,760,704 bytes — slightly larger due to Litestream metadata)

## Starting Litestream for permanent operation

```
install_litestream.bat
```

Or manually:
```
litestream.exe replicate -config litestream.yml
```

## Files

| File | Purpose | Gitignored |
|------|---------|------------|
| litestream.yml | R2 replication config with credentials | YES |
| litestream.exe | Binary (38 MB) | YES |
| install_litestream.bat | Startup script | NO |
| restore_drill.py | Backup verification | NO |
| .env | R2 credentials (R2_ACCESS_KEY_ID, etc.) | YES |
