# Investigation F.2 — agt_deck FastAPI deep audit

## Executive summary

2 HIGH / 2 MED / 2 LOW

---

## Architecture + surface reviewed

The `agt_deck/` package is a FastAPI application (`main.py`, ~1100 LOC) serving the AGT "Cure Console" and "Command Deck" UI. It mounts Jinja2 templates with autoescape enabled and a single `StaticFiles` mount. Auth is enforced globally via `TokenAuthMiddleware` (query-param `?t=<token>`) on all non-`/static` routes. Read routes use `get_ro_conn()` (URI mode, `PRAGMA query_only=ON`). Write routes (`POST /api/cure/dynamic_exit/{audit_id}/attest` and `POST /api/cure/r5_sell/stage`) use `get_rw_conn()` — which is `get_db_connection()` from `agt_equities/db.py`. All SQL uses parameterized queries. Templates use `autoescape=True` globally and no `|safe` filter anywhere on user input. `queries.py` contains no raw string SQL concatenation.

---

## HIGH findings

### F2-H-1 — Write path uses bare `conn.commit()` not `tx_immediate` (r5_sell stage)

**File:line** `agt_equities/rule_engine.py:709–726` (called from `agt_deck/main.py:989`)

**Snippet**
```python
conn.execute(
    "INSERT INTO bucket3_dynamic_exit_log ...",
    (audit_id, ticker, ...),
)
conn.commit()  # bare DEFERRED commit, not tx_immediate
```

**Why it's a bug**
The `r5_sell_stage` POST handler (`main.py:987`) opens a `get_rw_conn()` and passes it to `stage_stock_sale_via_smart_friction()`. That function uses `conn.execute(...)` followed by a bare `conn.commit()` with no `BEGIN IMMEDIATE`. Under Python sqlite3's default isolation level, the transaction starts as DEFERRED, which races to upgrade to RESERVED on the first write. If the bot's writer holds a RESERVED lock at that instant (e.g., during an EL snapshot write or flex_sync), the upgrade can fail silently, returning a `STAGED` row that was never actually written. The sister function `attest_staged_exit` in `queries.py` already avoids this (it documents that the caller owns the transaction), and `smart_friction_submit` in `main.py` does call `conn.rollback()` / `conn.commit()` from the outer scope — but `stage_stock_sale_via_smart_friction` bypasses that outer control by calling `conn.commit()` internally.

There is also a subtle isolation violation: `main.py:r5_sell_stage` acquires `conn` before calling `stage_stock_sale_via_smart_friction`, but the stage function commits its own transaction unconditionally (line 726). Any exception after commit but before returning to `main.py`'s `finally: conn.close()` leaves the connection post-commit with no way to roll back.

**Proposed fix sketch** (~4 LOC)
Replace the bare `conn.execute(...); conn.commit()` block in `rule_engine.py:stage_stock_sale_via_smart_friction` with `with tx_immediate(conn):` wrapping the `INSERT`. Remove the inner `conn.commit()`. Caller (`main.py:r5_sell_stage`) already handles `conn.rollback()` in its except branch; the tx_immediate context manager then owns commit/rollback cleanly.

---

### F2-H-2 — Auth token exposed in uvicorn access log (token-in-URL pattern)

**File:line** `agt_deck/main.py:71`, `main.py:1103`

**Snippet**
```python
token = request.query_params.get("t", "")
# ...
uvicorn.run(app, host="0.0.0.0", port=8787, log_level="info")
```

**Why it's a bug**
The auth token is a URL query parameter (`?t=<secret>`). Uvicorn at `log_level="info"` writes every request line to stdout in the format `GET /?t=<token> HTTP/1.1 200`. On Windows, NSSM captures stdout to a log file. Any process or user with access to the NSSM log file can read the live token from a single request line. The token is a 32-byte URL-safe secret — high entropy — but once in the access log it is effectively durable. This is a standard "secret in URL" antipattern.

The service also binds `0.0.0.0` (comment: "for Tailscale mobile access"), meaning the port is reachable by any Tailscale peer, not just localhost. This widens the surface for log-scraping or Referer-header token leakage.

**Proposed fix sketch** (~10 LOC)
Move token validation to an `Authorization: Bearer <token>` header or a `Sec-Fetch-Site`-guarded cookie, so the secret does not appear in the uvicorn access log. Short-term: pass `access_log=False` to `uvicorn.run()` and add a manual `logger.info("GET %s %d", request.url.path, response.status_code)` that strips query params. Eliminates log exposure without changing clients (HTMX calls can be updated to send the token in a header).

---

## MED findings

### F2-M-1 — SSE error event leaks internal exception message to browser

**File:line** `agt_deck/main.py:463`

**Snippet**
```python
except Exception as exc:
    logger.warning("SSE update failed: %s", exc)
    yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
```

**Why it's a bug**
Any exception in the 30-second SSE tick — including DB errors with internal path fragments (`agt_desk.db`, table names, query text) or yfinance exceptions with API URLs — is serialized verbatim into the SSE stream. The browser's EventSource listener in `app.js` receives it. If the app is viewed on Tailscale from a mobile device that is sharing screen, or if browser DevTools is open, internal error strings are visible. Not SQL-injectable, but leaks implementation details. Severity is MED rather than HIGH because the token gate must already be passed to reach `/sse`.

**Proposed fix sketch** (~3 LOC)
Replace `str(exc)` in the SSE yield with a static `"internal error — check server logs"` string. The full exception is already captured by `logger.warning`.

---

### F2-M-2 — Global `_vix_cache` and `_spot_cache` have no asyncio lock (concurrent mutation)

**File:line** `agt_deck/main.py:81–98`, `103–136`

**Snippet**
```python
_vix_cache: dict = {"value": None, "fetched_at": 0}
_spot_cache: dict = {}

def get_vix() -> float | None:
    ...
    _vix_cache["value"] = float(val)
    _vix_cache["fetched_at"] = now
```

**Why it's a bug**
FastAPI with uvicorn runs in a single asyncio event loop but the route handlers call `get_vix()` and `get_spots()` synchronously (not in a threadpool executor), which means they execute in the event loop thread. In that setting, CPython's GIL prevents true data races on dict mutation. However, both `get_vix()` and `get_spots()` call `yf.download()` / `ticker.fast_info` which are blocking network I/O. If uvicorn is configured with multiple workers or if a future refactor moves these to `run_in_executor`, two concurrent calls can both see `cache miss`, both fetch, and one silently overwrites the other's result with stale data. More immediately: the read-check-write on `_vix_cache` is non-atomic — a concurrent request can observe an inconsistent intermediate state during the multi-key update (`"value"` written, `"fetched_at"` not yet). Severity MED because it does not affect DB integrity, only UI display accuracy.

**Proposed fix sketch** (~6 LOC)
Wrap both cache functions with `asyncio.Lock()` (module-level `_vix_lock` and `_spot_lock`) and `await` them from an async wrapper, or move fetches to a background APScheduler job that writes cache from a single owner thread, making all FastAPI reads lock-free.

---

## LOW findings

### F2-L-1 — CSRF: no CSRF token on state-changing POST endpoints

**File:line** `agt_deck/main.py:737` (`smart_friction_submit`), `main.py:922` (`r5_sell_stage`)

**Why it's a bug**
Both POST endpoints mutate `bucket3_dynamic_exit_log`. There is no CSRF token in the forms or HTMX headers — protection relies entirely on the query-param `?t=<token>`. Under the internal-network threat model this is acceptable: an attacker who can forge a cross-origin POST would also need to know the `?t=` token, which is not accessible cross-origin due to SOP. However, if the user ever browses a malicious internal page while authenticated, that page could `fetch()` the POST endpoint using relative URL if the token is predictable or stored in localStorage. Rating LOW because the token in the URL acts as an implicit CSRF secret (it must be re-included in every HTMX form action, which the templates do correctly via `?t={{ token }}`).

**Proposed fix sketch** (~5 LOC)
Add `Referrer-Policy: same-origin` and `X-Frame-Options: SAMEORIGIN` response headers via middleware. Optionally add `starlette-csrf` middleware as a defense-in-depth layer.

---

### F2-L-2 — `.deck_token` file written world-readable to project root

**File:line** `agt_deck/main.py:1092–1097`

**Snippet**
```python
_token_path = Path(__file__).resolve().parent.parent / ".deck_token"
fd, tmp = tempfile.mkstemp(dir=str(_token_path.parent), prefix=".deck_token_")
with os.fdopen(fd, "w") as f:
    f.write(DECK_TOKEN)
os.replace(tmp, str(_token_path))
```

**Why it's a bug**
`tempfile.mkstemp` creates the file with mode `0600` on POSIX, but on Windows the default ACL inherits from the parent directory. If `C:\AGT_Telegram_Bridge\` has loose ACLs (e.g., `Users: Read`), the token file is readable by any local user. The comment says this is "for launcher discovery" — if the launcher only needs it, a named pipe or environment variable would avoid persistent disk exposure. Rating LOW because LocalSystem is the NSSM service account and the file is in a project directory that should be restricted anyway; this is a defense-in-depth gap rather than an active vulnerability.

**Proposed fix sketch** (~3 LOC)
On Windows, call `win32security` to set a restrictive DACL on the temp file before `os.replace`, or use `icacls` via subprocess to lock the file to the service account only. Alternatively, remove the file write and pass the token via a named pipe or the NSSM environment block.

---

## Coverage notes

**Read in full:** `agt_deck/main.py`, `agt_deck/db.py`, `agt_deck/queries.py`, `agt_deck/formatters.py`, `agt_deck/risk.py`, `agt_deck/desk_state_writer.py`, `agt_equities/db.py`, `agt_deck/templates/cure_smart_friction.html`, `agt_deck/templates/cure_attest_success.html`, `agt_deck/templates/command_deck.html` (first 80 lines).

**Spot-checked:** `agt_equities/rule_engine.py:650–739` (the `stage_stock_sale_via_smart_friction` path). Grep confirmed: no `|safe` filter anywhere in templates; all SQL uses `?` parameters; no `render_template_string`-style calls; no `open()` with user-controlled path; no `os.system()` / `subprocess` calls.

**Not read:** `agt_deck/templates/base.html`, `cure_console.html`, `cure_partial.html`, `cure_health_strip.html`, `cure_lifecycle.html`, `agt_deck/static/app.js`, `agt_deck/static/app.css`. The templates omitted are Jinja2 render targets with `autoescape=True` — no `|safe` found in a full grep — so XSS risk from those is low. `app.js` is worth a future pass to check for `innerHTML` assignments from SSE event data (the SSE `topstrip` event delivers formatted strings that go through `formatters.py` — no raw DB values).
