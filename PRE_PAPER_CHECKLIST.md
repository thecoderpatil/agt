# AGT Equities — Pre-Paper Operational Readiness Checklist

**Generated:** 2026-04-08 | **Sprint:** W5.1

---

## TWS / Gateway Settings

| # | Item | How to verify | Expected result | If fails | Auto? |
|---|------|---------------|-----------------|----------|-------|
| 1 | API enabled | TWS → File → Global Configuration → API → Settings → "Enable ActiveX and Socket Clients" checked | Checked | Check the box, restart TWS | Human |
| 2 | Read-Only API unchecked | Same settings page → "Read-Only API" | Unchecked | Uncheck it (required for order placement) | Human |
| 3 | Port configuration | TWS → API Settings → Socket port | 4001 (Gateway) or 7497 (TWS) | Update port to match IB_TWS_PORT in .env | Human |
| 4 | Trusted IPs | TWS → API Settings → Trusted IPs | 127.0.0.1 listed | Add 127.0.0.1 | Human |
| 5 | Master client ID | TWS → API Settings → Master API client ID | Matches IB_CLIENT_ID in .env | Update .env or TWS config | Human |
| 6 | Auto-restart | Gateway → Configure → Settings → Auto Restart → Time | Set to 11:45 PM ET (outside market hours) | Configure auto-restart | Human |
| 7 | Log level | TWS → API Settings → Logging Level | "Error" (production) or "Detail" (debug) | Adjust as needed | Human |

## IBKR Account Permissions

| # | Item | How to verify | Expected | If fails | Auto? |
|---|------|---------------|----------|----------|-------|
| 1 | Options trading | IBKR Account Management → Trading Permissions | Enabled for US Options | Contact IBKR support | Human |
| 2 | Margin enabled | Account Management → Account Type | Margin account for U21971297 | N/A (account type fixed) | Human |
| 3 | Market data | TWS → Account → Market Data Subscriptions | US Securities Snapshot and Futures (L1) active | Subscribe via Account Management | Human |
| 4 | Paper account | IBKR Account Management → Paper Trading | Paper credentials valid | Reset paper credentials | Human |

## Bot Environment

| # | Item | How to verify | Expected | If fails | Auto? |
|---|------|---------------|----------|----------|-------|
| 1 | Python version | `python --version` | 3.13.x | Install correct version | Auto |
| 2 | ib_async version | `python -c "import ib_async; print(ib_async.__version__)"` | 2.1.0 | `pip install ib_async==2.1.0` | Auto |
| 3 | TELEGRAM_BOT_TOKEN | `python -c "from dotenv import load_dotenv; load_dotenv(); import os; print(bool(os.environ.get('TELEGRAM_BOT_TOKEN')))"` | True | Check .env file | Auto |
| 4 | AUTHORIZED_USER_ID | Same pattern, check env var exists and is numeric | Non-empty integer | Check .env | Auto |
| 5 | ANTHROPIC_API_KEY | Same pattern | Non-empty | Check .env | Auto |
| 6 | FINNHUB_API_KEY | Same pattern | Non-empty | Check .env | Auto |
| 7 | IB_TWS_PORT | Same pattern | 4001 or 7497 | Check .env | Auto |
| 8 | DB path | `python -c "from pathlib import Path; p = Path('agt_desk.db'); print(p.exists(), p.stat().st_size)"` | True, > 1MB | Run init_db or restore from backup | Auto |
| 9 | Log directory | `ls logs/` | Directory exists | `mkdir logs` | Auto |
| 10 | Requirements | `pip install -r requirements.txt --dry-run` | All satisfied | `pip install -r requirements.txt` | Auto |

## Backup State

| # | Item | How to verify | Expected | If fails | Auto? |
|---|------|---------------|----------|----------|-------|
| 1 | Litestream running | `tasklist \| findstr litestream` | Process listed | `install_litestream.bat` | Auto |
| 2 | R2 connectivity | Check litestream log for recent sync | Sync within last 10s | Check R2 credentials in .env | Human |
| 3 | Restore drill | `python restore_drill.py --from-r2` | All 7 tables MATCH | Investigate R2 config | Auto |
| 4 | Baseline backup | `ls agt_desk.db.phase1_baseline_*` | At least 1 file | Run manual backup | Auto |

## Alert Channels

| # | Item | How to verify | Expected | If fails | Auto? |
|---|------|---------------|----------|----------|-------|
| 1 | Bot reachable | Send `/mode` to bot in Telegram | Responds with current mode | Check bot token, restart bot | Human |
| 2 | Operator receives alerts | Send `/health` from AUTHORIZED_USER_ID | Response received | Verify AUTHORIZED_USER_ID in .env | Human |
| 3 | Phone notifications | Send test message, confirm phone buzzes | Notification received | Check Telegram notification settings | Human |

## Safety State

| # | Item | How to verify | Expected | If fails | Auto? |
|---|------|---------------|----------|----------|-------|
| 2 | No TRANSMITTING rows | `SELECT COUNT(*) FROM bucket3_dynamic_exit_log WHERE final_status='TRANSMITTING'` | 0 | Manual investigation required | Auto |
| 3 | No ATTESTED rows | `SELECT COUNT(*) FROM bucket3_dynamic_exit_log WHERE final_status='ATTESTED'` | 0 | Sweep or manual ABANDONED | Auto |
| 4 | Smoke test cleanup | `SELECT audit_id FROM bucket3_dynamic_exit_log WHERE audit_id LIKE 'smoke-%'` | 0 rows | DELETE smoke test rows | Auto |
| 5 | Sweeper running | `grep "attested_sweeper" logs/*.log \| tail -1` | Timestamp within last 60s | Restart bot | Auto |
| 6 | Poller running | `grep "attested_poller" logs/*.log \| tail -1` | Timestamp within last 10s (or no ATTESTED rows) | Restart bot | Auto |
| 7 | Tests passing | `python -m pytest tests/ -q` | 608/608 (or current count) | Fix failing tests before go-live | Auto |

---

## Pre-Paper Execution Order

1. Verify TWS/Gateway settings (human, 5 min)
2. Run bot environment checks (auto, 2 min)
3. Run backup state checks (auto, 2 min)
4. Clean up smoke test rows in production DB (auto, 1 min)
5. Start bot: `boot_desk.bat`
6. Verify alert channels (human, 2 min)
7. Verify safety state (auto, 1 min)
8. Run `/reconcile` — confirm clean output
9. **Go/No-Go decision**

---

## Windows Hardening — Signed off 2026-04-08

- [x] Windows Defender exclusion: C:\AGT_Telegram_Bridge
- [x] Windows Search indexing: disabled system-wide (no action needed)
- [x] PRAGMA busy_timeout=5000
- [x] PRAGMA synchronous=FULL (2) — stricter than NORMAL target, accepted
- [x] PRAGMA journal_mode=wal
- [x] Ctrl+C positive test: 3 clean post_shutdown lines (18:49:34)
- [x] X-button negative test: zero post_shutdown lines after 18:50:02 (bypass confirmed)

Operator signature: Yash | Date: 2026-04-08

---

*End of checklist.*
