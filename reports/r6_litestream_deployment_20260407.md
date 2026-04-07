# R6: Litestream Deployment Report

Generated: 2026-04-07

## Deployed

- **Litestream v0.5.10** (Windows x86_64, 38 MB)
- **R2 bucket:** agt-desk-backup (Cloudflare)
- **Replication interval:** 10 seconds
- **Retention:** 30 days

## Verification

### Replication confirmed
```
level=INFO msg="initialized db" path=C:\AGT_Telegram_Bridge\agt_desk.db
level=INFO msg="replicating to" type=s3 bucket=agt-desk-backup
level=INFO msg="ltx file uploaded" size=1082068
level=INFO msg="snapshot complete" txid=0000000000000002 size=1082069
level=INFO msg="replica sync" txid.replica=0000000000000002 txid.db=0000000000000002
```

Initial snapshot (1.08 MB) uploaded successfully. Subsequent syncs confirmed.

### Restore drill: PASS

All 7 key tables match between live DB and R2 restore:
- pending_orders: 205/205
- master_log_trades: 1466/1466
- All others: exact match

Restore time: < 5 seconds for 2.8 MB database.

## Credentials

R2 credentials stored in `.env` as:
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_ENDPOINT`
- `R2_BUCKET`

Litestream.yml reads credentials directly (gitignored). No credentials in any tracked file.

## .gitignore coverage

- `litestream.yml` — contains R2 credentials
- `litestream.exe` — 38 MB binary
- `.env` — all secrets

## Next steps

1. Yash runs `install_litestream.bat` to start permanent replication
2. Run restore drill weekly: `python restore_drill.py --from-r2`
3. Monitor R2 bucket size in Cloudflare dashboard

## Live DB: NOT modified during deployment (Litestream is read-only)
