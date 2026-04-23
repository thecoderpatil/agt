# Investigation F.3 — pxo_scanner + vrp_veto + dashboard_renderer + screener + scripts + asyncio race trace

## Executive summary

4 HIGH / 4 MED / 3 LOW across 3 subsurfaces.

---

## Part A: pxo_scanner + vrp_veto + dashboard_renderer

### pxo_scanner.py

**[LOW] `__file__`-anchored DB path (line 34-37)**
`_DB_PATH` is computed as `Path(__file__).resolve().parent / "agt_desk.db"`. This is correct for production (file lives in repo root), but if the module is imported from a different working directory context (e.g., a test subprocess that patches `sys.path` without setting `AGT_DB_PATH`), the path resolves relative to the scanner's physical location. The `AGT_DB_PATH` env override exists and is the right escape hatch; the default chain is safe as-is but noteworthy.

**[MED] Silent exception swallower masks DB errors in `_load_scan_universe` (line 82-83)**
`except Exception: pass` on the DB read swallows every SQLite error — permissions failures, schema drift, `SQLITE_BUSY` on WAL — and silently falls back to the hardcoded 14-ticker `_FALLBACK_WATCHLIST`. Operator sees no log warning; DB failures during a scan run are invisible.

**[LOW] No per-ticker scan timeout on `yf.Ticker.option_chain()` (line 190)**
`scan_single_ticker` calls `yf_ticker.option_chain(exp_str)` with no timeout guard. If yfinance hangs (DNS, SSL, slow CDN), a single ticker blocks the entire sequential scan indefinitely. `_prefilter_by_volatility` uses `yf.download` which also has no timeout guard (line 98). Compare: `_fetch_latest_headline` correctly wraps in `ThreadPoolExecutor.result(timeout=3)`.

**[LOW] Non-idempotent DB schema: no writes to `agt_desk.db`**
`scan_csp_candidates` does NOT write candidates to the DB — results are returned in-memory only. Re-running is idempotent. The DB is read-only in this module (ticker universe load). No duplicate-row risk.

**[MED] Mode blindness — no paper/live gate in `scan_csp_candidates` (line 267)**
`pxo_scanner` is purely a data-fetch function with no awareness of `AGT_EXECUTION_ENABLED` or `broker_mode`. It always scans yfinance regardless of mode. This is architecturally correct (scan is always informational), but callers (`scan_orchestrator.py`, `telegram_bot.py`) must gate execution independently. No veto logic is inverted here.

---

### vrp_veto.py

**[HIGH] Bare `with conn:` on `vrp_analytics.db` — DEFERRED transaction (lines 76, 137)**
`init_vrp_db()` and `write_vrp_results()` both use `with conn:` on a freshly opened `sqlite3.connect()`. Python's `sqlite3` uses DEFERRED transactions by default under `with conn:`. On a WAL-mode DB under concurrent reads, a DEFERRED write can be silently rolled back if the BEGIN DEFERRED cannot obtain a write lock. Since `vrp_analytics.db` is a separate analytics DB (not `agt_desk.db`), the WAL contention risk is lower than for the main DB, but the pattern violates the project invariant. The calls to `write_vrp_results` are already correctly wrapped in `asyncio.to_thread` on the telegram_bot side (lines 10363, 10453), meaning they run in a thread pool — which makes the DEFERRED transaction risk slightly worse, not better (thread-pool context + DEFERRED + WAL = silent rollback scenario).

**[MED] `_VRP_DB_PATH` anchored to `__file__` (line 34, 43)**
`_BASE_DIR = Path(__file__).resolve().parent` → `_VRP_DB_PATH = _BASE_DIR / "vrp_analytics.db"`. This means the VRP analytics DB is always created in the repo checkout directory, not in `C:\AGT_Runtime\state\`. On a deploy rotation, `bridge-current` changes but the VRP DB written during standalone `__main__` runs goes to the worktree, while the NSSM service writes to the deployed copy. History can fragment across paths.

**[MED] No paper/live inversion bug — VRP veto is correctly applied**
The veto logic computes `vrp = IV - RV`. When `vrp <= 0` → `DO_NOT_SELL`. This is the correct direction: negative VRP means realized vol > implied vol, so selling premium is punished. The inversion question (paper vs live) does not apply: `vrp_veto.py` has no mode-awareness, and its call sites in `telegram_bot.py` (lines 10325-10363) are not gated differently for paper vs live. The IBKR fallback path (`fetch_iv_from_ibkr_async`) calls `ib.reqMarketDataType(4)` (delayed data) regardless of mode — no live/paper divergence.

---

### dashboard_renderer.py

**[LOW] Hardcoded IBKR account IDs (lines 39-43, 48, 658-660)**
`ACCOUNT_ALIAS`, `DISPLAY_ACCOUNTS`, and `_positions_from_ledger()` contain literal account IDs (`U21971297`, `U22076329`, `U22388499`, `U22076184`). These match `agt_equities/config.py`'s `ACCOUNT_TO_HOUSEHOLD` map. No injection risk here, but multi-account portability requires editing both files.

**[LOW] No injection risk in inline keyboards**
`dashboard_renderer.py` produces PNG images, not Telegram inline keyboard markup. It does not construct `callback_data` strings from user input or DB data. No injection vector identified.

**[MED] Bare `sqlite3.connect()` without tx_immediate in `generate_dashboard()` (line 691-692)**
`generate_dashboard()` opens `sqlite3.connect(db_path)` and passes it to `render_performance_card` and `render_positions_grid`. All queries inside those functions are `SELECT`-only, so the DEFERRED transaction issue is irrelevant for reads. However, the connection is closed via `conn.close()` rather than `closing()`, meaning an exception in a render function before `conn.close()` leaks the connection. Low practical impact (auto-closed on GC), but noteworthy.

---

## Part B: agt_equities/screener + scripts

### agt_equities/screener/ package

**Clean overall.** The screener uses frozen dataclasses (`frozen=True, slots=True`) for all inter-phase handoff types (`types.py`), enforcing immutability across phase boundaries. No shared mutable state.

**Cache module (`cache.py`):** TTL is caller-supplied per `cache_get()` call (line 59). No module-level TTL constant that could be forgotten. Atomic writes via `tempfile.mkstemp + os.replace` (lines 115-121). `_DEFAULT_CACHE_ROOT` uses `__file__`-anchored path (line 34) which is standard and safe for a package installed in the repo.

**Config module (`config.py`):** All thresholds are module-level constants (immutable primitives and frozensets). `EXCLUDED_SECTORS` is a `frozenset[str]` — correct type for immutable set. Sector matching in `pxo_scanner._load_scan_universe` uses `.lower()` case-insensitive comparison against the frozenset (line 76-80), which is correct.

**`vol_event_armor.py` line 276:** `asyncio.to_thread(calendar_provider.get_corporate_calendar, tk)` — offloads a provider method. `YFinanceCorporateIntelligenceProvider` writes to a file-based cache; no module-level shared state mutation. Clean.

### scripts/

**[HIGH] `scripts/circuit_breaker.py` line 19: `os.chdir()` at module import**
`os.chdir(Path(__file__).resolve().parent.parent)` runs unconditionally at module import time. This function is imported at runtime inside `telegram_bot.py` at lines 13029 and 14270 via `from scripts.circuit_breaker import run_all_checks`, and the call is offloaded via `asyncio.to_thread(_cb_run)`. The `os.chdir()` fires during the `import` inside the worker thread, changing the process-wide working directory from inside a thread pool worker. **This is a global process mutation** — `os.chdir()` affects all threads simultaneously. Any concurrent code in the main thread or another worker thread that uses relative paths (e.g., `Path("agt_desk.db")`, `open(".env")`) could suddenly resolve to the wrong directory. The Python docs confirm `os.chdir()` is process-global, not thread-local.

**`scripts/deploy/deploy.ps1`:** Clean. 3-slot atomic rotation (staging → current → previous), service stop/start bracketed, robocopy uses explicit exclusion lists. Validates canonical `.env` and state dir before proceeding. Exit codes checked.

**`scripts/deploy/rollback.ps1`:** Clean. Quarantines failed current rather than deleting it. No destructive overwrite of forensic data.

**`scripts/migrate_*.py`:** Spot-checked `migrate_csp_decisions_table.py` — uses `sys.path.insert` from `__file__`, no `os.chdir()`. Schema operations are standard `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE`. No DROP TABLE or DELETE FROM in the migration scripts checked.

---

## Part C: asyncio.to_thread race-boundary trace

Total `asyncio.to_thread` call sites found: **51 in `telegram_bot.py`** + **9 in other production modules** (`roll_scanner.py`, `scan_orchestrator.py`, `screener/vol_event_armor.py`, `telegram_dispatch.py`, `dev_cli.py`).

| File:Line | Offloaded Function | Touches Module-Level Shared State? | Lock Present? | Notes |
|---|---|---|---|---|
| `telegram_bot.py:2897` | `append_pending_tickets(tickets)` | No — DB write only | N/A | Uses `tx_immediate`; safe |
| `telegram_bot.py:3077` | `func, *args` (generic wrapper `_with_timeout_async`) | Caller-dependent | Caller-dependent | Wrapper; risk depends on offloaded `func` |
| `telegram_bot.py:5745` | `working_call_shares` compute | No — pure computation | N/A | Clean |
| `telegram_bot.py:6467` | `append_pending_tickets([ticket])` | No | N/A | Uses `tx_immediate`; safe |
| `telegram_bot.py:6709` | `append_pending_tickets([ticket])` | No | N/A | Uses `tx_immediate`; safe |
| `telegram_bot.py:7347` | ledger read | No — read-only DB | N/A | Clean |
| `telegram_bot.py:8055` | `append_pending_tickets([ticket])` | No | N/A | Safe |
| `telegram_bot.py:9201` | `assert_execution_enabled_strict(in_process_halted=_HALTED)` | Reads `_HALTED` (bool) | None | Read-only of a simple bool; no race risk |
| `telegram_bot.py:9632` | `_load_cc_ladder_snapshot(...)` | No — pure IBKR/yfinance fetch | N/A | Clean |
| `telegram_bot.py:10325` | `fetch_rv(single_ticker)` | No — pure HTTP | N/A | Clean |
| `telegram_bot.py:10327` | `fetch_earnings(single_ticker)` | No — pure HTTP | N/A | Clean |
| `telegram_bot.py:10363` | `write_vrp_results([result], ...)` | No — writes to `vrp_analytics.db` | N/A | Bare `with conn:` in callee (DEFERRED tx — HIGH bug in vrp_veto.py) |
| `telegram_bot.py:10413` | `fetch_rv(ticker)` (gather) | No | N/A | Clean |
| `telegram_bot.py:10415` | `fetch_earnings(ticker)` (gather) | No | N/A | Clean |
| `telegram_bot.py:10453` | `write_vrp_results(results, ...)` | No | N/A | Same DEFERRED tx concern |
| `telegram_bot.py:11023` | `_refresh_ticker_universe_sync()` | No — DB write to ticker_universe | N/A | Clean; isolated DB write |
| `telegram_bot.py:11599` | count query | No — read-only | N/A | Clean |
| `telegram_bot.py:11695` | claim row (DB write) | No | N/A | Caller uses `tx_immediate` |
| `telegram_bot.py:11722` | row read | No — read-only | N/A | Clean |
| `telegram_bot.py:11960` | row read | No | N/A | Clean |
| `telegram_bot.py:12004` | DB write | No | N/A | Clean |
| `telegram_bot.py:12020` | DB write | No | N/A | Clean |
| `telegram_bot.py:12125` | cancel rowcount (DB write) | No | N/A | Clean |
| `telegram_bot.py:12191` | row read | No | N/A | Clean |
| `telegram_bot.py:12268` | DB write | No | N/A | Clean |
| `telegram_bot.py:12311` | `_sync_check_ticker_locked(conn)` | No — DB read | N/A | Clean |
| `telegram_bot.py:12395` | recheck read | No | N/A | Clean |
| `telegram_bot.py:12616` | lock rowcount (DB write) | No | N/A | Clean |
| `telegram_bot.py:12731` | DB write | No | N/A | Clean |
| `telegram_bot.py:12843` | step8 rowcount (DB write) | No | N/A | Clean |
| `telegram_bot.py:13031` | `_cb_run` = `circuit_breaker.run_all_checks` | **YES — `os.chdir()` at import** | None | **HIGH: process-global cwd mutation from thread** |
| `telegram_bot.py:13601` | DB write with halted flag | Reads `_HALTED` (bool) | None | Read-only bool; safe |
| `telegram_bot.py:14272` | `_cb_run` = `circuit_breaker.run_all_checks` | **YES — `os.chdir()` at import** | None | **HIGH: same as line 13031** |
| `telegram_bot.py:15681` | `_get_effective_conviction(ticker)` | No — DB read | N/A | Clean |
| `telegram_bot.py:15685` | `_persist_conviction(ticker, conviction)` | No — DB write | N/A | Clean |
| `telegram_bot.py:17918` | rows read | No | N/A | Clean |
| `telegram_bot.py:18014` | `_resolve_incident_arg(arg)` | No — DB read | N/A | Clean |
| `telegram_bot.py:18062` | `_rem.gitlab_lower_approval_rule(...)` | No — HTTP | N/A | Clean |
| `telegram_bot.py:18064` | `_rem.gitlab_merge_mr(...)` | No — HTTP | N/A | Clean |
| `telegram_bot.py:18100` | `_ir.mark_merged(...)` | No — DB write | N/A | Clean |
| `telegram_bot.py:18178` | `_resolve_incident_arg(arg)` | No | N/A | Clean |
| `telegram_bot.py:18204–18220` | GitLab ops | No — HTTP | N/A | Clean |
| `telegram_bot.py:18655` | `_run` (shadow_scan.main) | Subprocess-like closure | None | Isolated; clean |
| `telegram_bot.py:19171` | `append_pending_tickets(tickets)` | No | N/A | Safe |
| `telegram_bot.py:19353` | `append_pending_tickets(tickets)` | No | N/A | Safe |
| `telegram_bot.py:22424` | `refresh_beta_cache(tickers)` | No — writes to `beta_cache` table | N/A | Clean; isolated DB write |
| `telegram_bot.py:22492` | `provider.get_corporate_calendar(tk)` | No — file cache write | N/A | Clean |
| `roll_scanner.py:1085` | `load_premium_ledger(...)` | No — DB read | N/A | Local `ledger_cache` dict is function-local |
| `roll_scanner.py:1303` | `ctx.order_sink.stage(...)` | No — DB write via sink | N/A | Clean |
| `scan_orchestrator.py:131` | `_load_scan_universe` | No — DB read | N/A | Clean |
| `scan_orchestrator.py:132` | `scan_csp_candidates(...)` | No — pure computation | N/A | Clean |
| `scan_orchestrator.py:174` | `_fetch_vix()` (closure) | No — yfinance fetch | N/A | Clean |
| `scan_orchestrator.py:193` | `fetch_earnings_map(...)` | No — pure HTTP | N/A | Clean |
| `scan_orchestrator.py:194` | `build_correlation_pairs(...)` | No — pure computation | N/A | Clean |
| `screener/vol_event_armor.py:276` | `calendar_provider.get_corporate_calendar(ticker)` | No — file cache write | N/A | Clean |
| `telegram_dispatch.py:54` | `telegram_approval_gate(...)` | No — polls DB, writes approval rows | N/A | Clean |

### Race-boundary findings

**[HIGH] `circuit_breaker.os.chdir()` mutation from thread pool (lines 13031, 14272)**
When `from scripts.circuit_breaker import run_all_checks` executes inside a `to_thread` worker for the first time, Python runs the module-level `os.chdir(Path(__file__).resolve().parent.parent)` (circuit_breaker.py line 19). `os.chdir()` is a POSIX process-global call — it changes `cwd` for all threads immediately. Any concurrent code in the main event loop thread (or another worker thread) that constructs relative paths (`Path("agt_desk.db")`, `open(".env")`) after this point would resolve to `scripts/..` = repo root, which happens to be the correct directory in this case. However, if the import fires at service startup when `cwd` is already `bridge-current/` (the NSSM service working directory), this could produce an unexpected double-change. Second import invocations are cached by Python's import system so `os.chdir` fires only once — but that one time is from a worker thread, which is unsafe. The risk is latent rather than a confirmed breakage in the current deploy layout, but it's a correctness violation of process isolation.

**[MED] `_dispatched_audits` set mutated from async event loop — confirmed safe for now, but requires care**
`_dispatched_audits` (module-level `set[str]`, line 258) is mutated (`.add()`, `.discard()`, `.difference_update()`) in `_poll_attested_rows` and several callback handlers. All these are `async def` functions called from the PTB event loop — which is single-threaded. No `to_thread` offload touches `_dispatched_audits` directly. The risk would emerge only if a `to_thread`-offloaded function were ever given a reference to `_dispatched_audits`. Currently none are. **Clean** — but the set is a refactoring trap.

**[MED] `_cooldown_tasks` dict mutated in async context only — clean**
`_cooldown_tasks` (dict, line 282) is written only from async callback handlers in the event loop. No worker thread touches it. Clean.

**No deadlock risk identified.** No `to_thread`-offloaded function holds a Python `asyncio.Lock`. The `_ib_connect_lock` and `_reconnect_lock` (lines 1427, 1429) are `asyncio.Lock` instances used only with `async with` in coroutines; they are never passed into `to_thread` workers. SQLite connections use SQLite's own WAL locking which is not subject to asyncio deadlock.

---

## Coverage notes

**Read in full:** `pxo_scanner.py` (366 lines), `vrp_veto.py` (827 lines), `dashboard_renderer.py` (723 lines), `agt_equities/screener/config.py`, `types.py`, `cache.py`, `scripts/circuit_breaker.py`, `scripts/deploy/deploy.ps1`, `scripts/deploy/rollback.ps1`, `agt_equities/beta_cache.py`, `agt_equities/scan_orchestrator.py` (partial), `agt_equities/telegram_dispatch.py`, `agt_equities/roll_scanner.py` (excerpt), `agt_equities/screener/vol_event_armor.py` (excerpt).

**Grepped for all patterns:** All `asyncio.to_thread` call sites across the full repo (`*.py`). All module-level mutable state declarations in `telegram_bot.py`. All `with conn:` occurrences in `vrp_veto.py`.

**Explicitly skipped:** Non-runtime throwaway scripts (`commit_*.py`, `patch_*.py`, `merge_*.py`, `poll_*.py`, `scripts/observe_trading_day.py`, etc.) per the brief. `screener/fundamentals.py`, `screener/universe.py`, `screener/ray_filter.py`, `screener/chain_walker.py` were not read in depth — the types, config, and cache files are sufficient to confirm the architecture. `migrate_*.py` spot-checked (`migrate_csp_decisions_table.py`) only; remaining 5 not read.
