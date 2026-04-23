# Opus Bug Hunt — Round 2 (Sprint 4 Investigation F synthesis)

**Dispatched:** 2026-04-24 dawn. **Synthesized:** 2026-04-24 (Opus 4.7, Coder).

Three Sonnet sub-agents ran in parallel against surfaces Sprint 2 Investigation E
explicitly did NOT deep-audit. Source reports (read-only audits):

- `reports/investigation_f1_telegram_bot_audit.md` — LLM tool-loop + natural-language
  order-staging + Yash-only command handlers
- `reports/investigation_f2_agt_deck_audit.md` — agt_deck FastAPI Cure Console
- `reports/investigation_f3_scanner_asyncio_audit.md` — pxo_scanner + vrp_veto
  + dashboard_renderer + screener + scripts + asyncio.to_thread race-boundary
  trace across 60 sites

## Aggregate count

**9 HIGH / 9 MED / 6 LOW** across 3 surfaces. That compares to Sprint 2 round 1's
5 HIGH / 7 MED / 4 LOW on different surfaces — round 2 is a higher-yield audit
because the surfaces were uncovered (especially `agt_deck/` which had zero prior
coverage).

**Codebase-clean signal:** the screener pipeline (`agt_equities/screener/`),
deploy scripts (`scripts/deploy/*.ps1`), and the 60-site asyncio.to_thread trace
across non-telegram_bot files came back CLEAN. The findings concentrate in
`telegram_bot.py` callback-wiring and a handful of legacy non-`tx_immediate` DB
writes.

## Executive summary by surface

### F.1 — telegram_bot LLM loop + NL staging + Yash-only handlers

**3 HIGH**
- **F1-H-1 — CC dashboard inline keyboard is entirely non-interactive.** All
  `cc:*` callbacks (`cc:select`, `cc:page`, `cc:exp`, `cc:confirm`, `cc:cancel`)
  are rendered by `/cc` but no `CallbackQueryHandler(pattern=r"^cc:")` is
  registered. `_cc_confirm_keyboard` is dead code. Users tap buttons and nothing
  happens.
- **F1-H-2 — Tickets staged via `parse_and_stage_order` (PATH 1 + LLM tool) are
  invisible to `/approve`.** `parse_and_stage_order` writes `status='pending'`;
  `cmd_approve`, `handle_approve_callback`, and `_auto_execute_staged` all filter
  `status='staged'`. Tickets staged via the natural-language path accumulate
  unreachable.
- **F1-H-3 — "Order Details" button uses `callback_data="orders_detail"`
  (no colon) against registered `r"^orders:"` pattern.** Orphaned.

**3 MED**
- **F1-M-1** — MAX_ROUNDS=15 tool-use exhausted silently; no user-facing message.
- **F1-M-2** — `parse_and_stage_order` skips Gate-1/Gate-2 at staging (gates fire
  at TRANSMIT only). Staging success message gives false-safe signal.
- **F1-M-3** — `update_live_order` (LLM-callable) accepts any `account_id`
  without `ACTIVE_ACCOUNTS` check.

**1 LOW**
- **F1-L-1** — `_check_and_track_tokens(0,0)` pre-flight writes a zero-token DB
  row on every message, inflating `api_calls` in `/budget`.

### F.2 — agt_deck FastAPI Cure Console

**2 HIGH**
- **F2-H-1 — `stage_stock_sale_via_smart_friction` (rule_engine.py:709-726) uses
  bare `conn.commit()` not `tx_immediate`.** WAL silent-rollback risk — same class
  as the E-H-3/E-H-4 / E-M-5 patterns. Outer handler's try/except rollback is
  bypassed because the inner commit flushes early. <10 LOC fix.
- **F2-H-2 — Auth token in URL query param logged by uvicorn access log.**
  `main.py:71,1103` binds `0.0.0.0`; the token travels on `GET /?t=<secret>`
  which uvicorn logs at info level. NSSM captures stdout to disk. Move to
  `Authorization` header or suppress the access log.

**2 MED**
- **F2-M-1** — SSE error event streams `str(exc)` to browser (internal DB paths,
  table names). `main.py:463`.
- **F2-M-2** — Global `_vix_cache`/`_spot_cache` with no lock; blocking yfinance
  calls on the event loop. Single-process so GIL protects today, but latent.

**2 LOW**
- **F2-L-1** — No CSRF token on POST endpoints (token-in-URL acts as implicit
  CSRF secret today).
- **F2-L-2** — `.deck_token` file written without restrictive ACL on Windows.

### F.3 — scanner + screener + scripts + asyncio race trace

**4 HIGH**
- **F3-H-1 — `vrp_veto.py:76, 137` — bare `with conn:` on `vrp_analytics.db`.**
  Both `init_vrp_db()` and `write_vrp_results()` are DEFERRED-tx. Both are
  offloaded via `asyncio.to_thread` from telegram_bot.py, so under WAL contention
  a silent rollback is achievable. <10 LOC fix (mirror MR 3 Sprint 3 pattern).
- **F3-H-2 — `scripts/circuit_breaker.py:19` `os.chdir()` at module import,
  fired from `asyncio.to_thread` worker.** MR !209 wrapped the circuit_breaker
  call in `to_thread`; the target module's import-time `os.chdir()` then runs
  on the worker thread but affects the process-global CWD (shared with every
  thread). This is a correctness violation — the net directory happens to be
  the repo root in practice, so there's no observable breakage today, but any
  concurrent code that depends on CWD is racing the circuit_breaker import.
  **Design question:** why is `os.chdir()` at module scope? Can it be removed
  entirely, or at least guarded with `if __name__ == "__main__"`?

**4 MED**
- **F3-M-1** — `pxo_scanner._load_scan_universe` silent `except Exception: pass`.
  DB errors (permissions, busy, schema drift) fall through to the hardcoded
  watchlist with zero visibility.
- **F3-M-2** — `vrp_veto._VRP_DB_PATH` + `pxo_scanner._DB_PATH` are
  `__file__`-anchored. Same class as E-M-4 but for the VRP analytics DB. After
  atomic-rotation deploy, history fragments across paths.
- **F3-M-3** — (from asyncio trace) `write_vrp_results` at
  `telegram_bot.py:10363, 10453` — DEFERRED-tx in callee. Covered by F3-H-1's
  fix.
- **F3-M-4** — (implicit in F3-H-2) `circuit_breaker.os.chdir()` impacts every
  thread, not just the worker.

**3 LOW**
- **F3-L-1** — No timeout on `yf.option_chain()` in `scan_single_ticker`. A hung
  yfinance call blocks the full sequential scan. (Contrast: `_fetch_latest_headline`
  uses a 3-second ThreadPoolExecutor timeout.)
- **F3-L-2** (implicit) — screener pipeline was CLEAN.
- **F3-L-3** (implicit) — deploy scripts were CLEAN.

## Follow-on MR recommendations

**Trivial HIGH-severity fixes shippable as dedicated small MRs tomorrow:**

| Finding | Proposed MR | Size | Risk | Blocker? |
|--------|-------------|------|------|----------|
| F2-H-1 | MR D: rule_engine.py smart-friction staging → tx_immediate | ~8 LOC | Low (pattern exists) | No |
| F3-H-1 | MR E: vrp_veto.py tx_immediate sweep | ~10 LOC | Low | No |
| F1-H-1 | MR F: register `^cc:` CallbackQueryHandler | ~3 LOC but needs design | Medium — dead code may be intentionally parked | **Yes, needs Architect ruling** |
| F1-H-2 | MR G: unify `pending` vs `staged` ticket status | ~5 LOC | Medium — pending semantics may be load-bearing | **Yes, needs Architect ruling** |
| F1-H-3 | MR H: fix `callback_data="orders_detail"` → `"orders:detail"` | ~2 LOC | Low | No |
| F2-H-2 | MR I: move agt_deck auth token from URL query to header | ~20 LOC | Medium — client-side breaking change | **Yes, Yash opens console from Tailscale; coordinate** |
| F3-H-2 | MR J: remove `os.chdir()` from `circuit_breaker.py` module scope | ~5 LOC | Medium — CWD-relative reads may be load-bearing | **Yes, needs Architect ruling** |

Sprint 5 recommended ship order (if all HIGHs cleared by Architect):
  D → E → H (no-brainer tx + trivial fixes)
  F → G → I → J (queued behind Architect ruling)

## Architect-ruling queue (wake-time triage)

1. **F1-H-1 (cc dashboard)** — is the CC inline keyboard intentionally parked?
   If yes, delete the dead rendering code. If no, ship MR F.
2. **F1-H-2 (pending vs staged ticket status)** — if `pending` is intentional for
   the NL path, the filter logic in `cmd_approve`/`_auto_execute_staged` must
   expand to include `pending`. If not, the NL path should write `staged`.
3. **F3-H-2 (circuit_breaker `os.chdir`)** — what's the load-bearing reason? Can
   it be removed or guarded? MR 1 Sprint 3's asyncio wrap surfaced this race.
4. **F2-H-2 (auth token in URL)** — coordinate with Yash on the client-side
   change. Current URL pattern is compatible with Tailscale bookmarks but the
   token leak into NSSM stdout logs is real.

## Coverage notes

**Audited this round:**
- `telegram_bot.py` LLM tool-loop + NL staging + all Yash-only handlers (F.1)
- `agt_deck/` full FastAPI surface including routes, middleware, DB access (F.2)
- `pxo_scanner.py`, `vrp_veto.py`, `dashboard_renderer.py` (F.3 part A)
- `agt_equities/screener/` package (F.3 part B)
- `scripts/` spot-audit focused on deploy + migration + circuit_breaker (F.3 part B)
- asyncio.to_thread race-boundary trace across 60 call sites repo-wide (F.3 part C)

**Explicitly skipped:**
- Throwaway one-shot scripts (`scripts/commit_*`, `scripts/patch_*`, `scripts/merge_*`)
- `.staged/` copies (these are commit-staging artifacts, not code)
- `agt_equities/walker.py` (pure function, prohibited to touch per CLAUDE.md)
- `agt_equities/flex_sync.py` (prohibited)
- `telegram_bot.py` already-audited surfaces from Sprint 2 round 1 (silent
  swallowers in `handle_approve_callback` etc.)

## No URGENT this round

None of the findings is a live-capital placement-side regression. F3-H-2
(`circuit_breaker.os.chdir()`) was arguably introduced by MR !209 Sprint 3
wrapping circuit_breaker in `to_thread`, but the net CWD is the repo root in
practice, so there's no current breakage. F1-H-2 (`pending` vs `staged`) is
semantic — NL-staged tickets never fire, which is operator-surprise but not
live-capital leak.

MR A + MR B's code introduces no new HIGH findings (verified via spot-audit of
new module bodies during draft).
