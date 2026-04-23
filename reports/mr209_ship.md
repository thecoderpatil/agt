# MR !209 ship report — telegram asyncio.to_thread wraps (Sprint 3 MR 1)

**Source dispatch:** `reports/overnight_sprint_3_dispatch_20260424.md` MR 1 section.
**Per-MR dispatch fence:** `reports/sprint3_mr1_dispatch.md`.
**Audit source:** `reports/telegram_approval_gate_asyncio_audit.md` (Sprint 2 Investigation B).

## Status
MERGED. Squash `368021695a99be7456ed4d741629db29603cedd8`, merge `fd5c98dd9255ef3e8671c3a3aef2a9792b6f5e9b`.

## Files

| path | +added | -removed | sha256 (first 8) |
|------|-------|----------|------------------|
| telegram_bot.py | 160 | 189 | see-remote |
| tests/test_telegram_async_offload.py | 183 | 0 | (new) |
| .gitlab-ci.yml | 1 | 1 | (register sprint_a) |

**Net:** −29 on telegram_bot.py (verbose `with closing()` → `asyncio.to_thread(_sync_db_write, ...)` collapse). +183 new test file. 0 net on CI yaml.

## Commit
- branch: `feature/telegram-async-offload`
- squash: `f3a03357cc0cd3232be7c67f1c888d2b9955fb99`
- MR: `!209`
- URL: https://gitlab.com/agt-group2/agt-equities-desk/-/merge_requests/209

## Verification
- `ast.parse(telegram_bot.py)`: OK
- Remote byte sizes match local: telegram_bot.py=491121, test=5789, ci=8672
- `asyncio.to_thread` count: 51 total in telegram_bot.py (40 pre-existing + 11 new)
- `asyncio.to_thread(_cb_run)` count: 2 (B-1 both sites)
- `_sync_db_read_one` call sites: 4 (B-3 read, B-4 fetch, B-5 Step 0, B-5 cooldown recheck)
- `_sync_db_write` call sites: 8 (B-3 ×2 + B-4 ×2 + B-5 ×4)
- LOC gate: GATE PASS (shrinking clause declared for telegram_bot.py)
- Local pytest on new file: 5/5 pass

## Scope decisions (reasoning latitude)

1. **Generic helpers over 10 named helpers.** The dispatch suggested per-site named helpers (`_csp_approval_read_row`, `_csp_approval_update_indices`, etc.). I shipped two generic module-level helpers (`_sync_db_read_one`, `_sync_db_write`) because every offload site is a single SQL statement — named helpers would add ~100 LOC of wrappers with no semantic benefit over the generic pair. Callers remain readable because the SQL string itself is the intent.

2. **Minor E-M-5-adjacent fix folded in.** B-3's two write blocks originally used bare `conn.commit()` (DEFERRED tx). My `_sync_db_write` helper uses `tx_immediate`, which means the new offloaded path is also tx-correct. This is a <10 LOC side-benefit consistent with dispatch latitude ("fold into this MR if the fix is <10 LOC"). Documented here; does NOT make MR 3 (E-M-5 sweep) redundant — MR 3 still needs to sweep `incidents_repo.py`, `remediation.py`, `author_critic.py`.

3. **cmd_daily circuit_breaker fold-in.** Same HTTP-heavy `_cb_run()` lives at line 14319 inside `cmd_daily`; same pattern, same fix, 1 LOC. Folded in per latitude.

4. **CAS atomicity preservation.** Steps 6 and 8 of the JIT chain retain atomicity via SQL `WHERE final_status = 'ATTESTED'` (or `'TRANSMITTING'`) + `tx_immediate` inside the worker thread. The await is AFTER conn release — no split-await race window.

5. **Step 2 closure-based offload.** `is_ticker_locked(conn, ticker)` takes a conn parameter (not a SQL string), so the generic helpers don't fit. Used a closure `_sync_check_ticker_locked` captured at call site, offloaded via `asyncio.to_thread`.

## Skipped per audit
- B-6/B-7 LOW findings (cold paths, masked UX — LLM round, /budget typing indicator).
- `ib_conn.placeOrder` / `cancelOrder` / `cancelMktData` — confirmed non-blocking in `ib_async` source per audit §4.

## CI
Expected pipeline: ~+5 passed (new tests), baseline steady at 1184/0/8.

## Notes

- **One minor scope expansion:** the two B-3 write blocks now use `tx_immediate` via the helper. This is an unintentional side-benefit of consolidating through a shared helper; the tx-correctness fix was MR 3's nominal scope for other files. No net risk — switching from DEFERRED to IMMEDIATE only tightens transaction semantics.

- **Mid-ship edit-tool quirk:** Windows CRLF + Unicode escape handling broke two Edit-tool replacements. Resolved via byte-level Python replacement. Two f-strings in Step 2 and Step 6 required LF→`\n` escape normalization post-replace. AST parse clean after fix. No logic-level impact.

## LOCAL_SYNC

```
LOCAL_SYNC:
  fetch/reset:     pending merge
  pip install:     no new deps
  smoke imports:   deferred until post-merge
  deploy.ps1:      PENDING (CRITICAL tier — telegram_bot.py touch, MUST redeploy)
  heartbeats:      pending
```

To run post-merge on the Coder machine:
```powershell
cd C:\AGT_Telegram_Bridge\.worktrees\coder
git fetch origin main
git reset --hard origin/main
C:\AGT_Telegram_Bridge\.venv\Scripts\python.exe -c "import telegram_bot"
powershell -ExecutionPolicy Bypass -File scripts\deploy\deploy.ps1
sqlite3 C:\AGT_Telegram_Bridge\agt_desk.db "SELECT service, last_beat_utc FROM daemon_heartbeat"
```
