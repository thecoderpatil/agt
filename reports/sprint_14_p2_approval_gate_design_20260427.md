# Sprint 14 P2 — CSP Approval-Gate Flow + Dedup + Timeout Default (Option C)
**Date:** 2026-04-27  
**Author:** Coder B  
**Status:** Design — awaiting Architect answer on open questions before code  
**MR tier:** CRITICAL (telegram_bot.py + agt_scheduler.py + approval_gate.py + formatter.py)

---

## Operator Decision — Option C

**Yash chose Option C:** build the approval gate properly. Timeout does NOT block Phase B clock.

- `AGT_BROKER_MODE=paper` + `AGT_CSP_TIMEOUT_DEFAULT=auto_approve` → paper auto-approves on timeout. Preserves Phase B clock momentum.
- `AGT_BROKER_MODE=live` + `AGT_CSP_TIMEOUT_DEFAULT=auto_reject` → live auto-rejects on timeout. Safe gate for live capital.
- Same flag (`AGT_CSP_TIMEOUT_DEFAULT`), flipped value at live promotion (`auto_approve` → `auto_reject`).
- Phase B verdict: engine activity = candidates staged > 0. Approval/rejection is a gate, not an activity counter. Zero approvals on a day where candidates were staged = valid "activity day" for Phase B. No special casing needed.

---

## Discipline: Cite-Ground Pass

Every scheduler call, DB column, callback registration, env var, and state transition in this document is cited to a specific file:line in the current codebase or to a prod sqlite_master query result. Claims marked **INFERRED** have not been verified against actual code.

---

## Recon Findings (all cited)

### 1. Scheduler + Auto-Execute Sequencing

**`PAPER_AUTO_EXECUTE` flag** — `telegram_bot.py:982`
```python
PAPER_AUTO_EXECUTE = PAPER_MODE and os.environ.get("PAPER_AUTO_EXECUTE", "1") != "0"
```
Default: on when PAPER_MODE. Kill-switch: `PAPER_AUTO_EXECUTE=0`.

**`needs_csp_approval`** — `agt_equities/approval_policy.py:30`
```python
def needs_csp_approval(ctx: "RunContext") -> bool:
    return ctx.broker_mode == "live" and ctx.engine == "csp"
```
Paper CSP: returns `False`. Live CSP: returns `True`. Non-CSP engines (cc, roll, harvest): returns `False`.

**`csp_scan_daily` registration** — `telegram_bot.py:22763–22773` (PTB JobQueue)
```python
jq.run_daily(callback=_scheduled_csp_scan, time=_time(hour=9, minute=35, tzinfo=ET), days=(1,2,3,4,5))
```

**`csp_digest_send` registration** — `telegram_bot.py:22779–22784` (PTB JobQueue)
```python
jq.run_daily(callback=_scheduled_csp_digest_send, time=_time(hour=9, minute=37, tzinfo=ET), days=(1,2,3,4,5))
```

**Full paper-mode sequence (cited):**

```
09:35 ET  _scheduled_csp_scan (telegram_bot.py:18904)
  ├─ build_run_context(engine="csp") [telegram_bot.py:18940–18950]
  ├─ run_csp_scan(approval_dispatcher=await_csp_approval) [telegram_bot.py:18954–18966]
  │    ├─ needs_csp_approval(ctx) = False  [approval_policy.py:30]
  │    │   (paper mode → False; approval_dispatcher not called)
  │    ├─ approved = inputs.candidates   [scan_orchestrator.py:319]
  │    │   (auto-approve full universe for paper/non-csp)
  │    └─ allocate_candidates(approved, inputs, ctx)  [scan_orchestrator.py:321]
  │         └─ run_csp_allocator() → append_pending_tickets()
  │              INSERT INTO pending_orders (engine='csp', run_id=ctx.run_id, ...)
  │              [telegram_bot.py:821–828]
  ├─ result.approved_count = len(approved) = 10  [scan_orchestrator.py:325]
  │   ← THIS IS THE "Approved: 10" IN THE 09:35 LOG [telegram_bot.py:18988]
  ├─ header: "Approved: {result.approved_count} · Staged: {staged_n}"
  │   [telegram_bot.py:18988]
  └─ PAPER_MODE and PAPER_AUTO_EXECUTE and staged_n:  [telegram_bot.py:19006]
       └─ _auto_execute_staged()  [telegram_bot.py:11405]
            SELECT id FROM pending_orders WHERE status='staged' ORDER BY id
            [telegram_bot.py:11453–11458]
            — NO engine filter, NO run_id filter, NO csp_approval check

09:37 ET  _scheduled_csp_digest_send (telegram_bot.py:19086)
  └─ run_csp_digest_job()  [csp_digest_runner.py:188]
       ├─ load_latest_result()  [csp_allocator.py:1195]
       ├─ build_digest_payload()  [csp_digest_runner.py:65]
       │    for rank, t in enumerate(staged, start=1):  [line 85]
       │    — iterates per-account tickets, NO grouping by ticker
       ├─ generate_commentary()  [csp_digest/llm_commentary.py]
       ├─ render_card_text(payload)  [csp_digest/formatter.py:104]
       └─ build_inline_keyboard(payload, run_id=...)  [csp_digest/formatter.py:144]
            paper mode → return []  [formatter.py:153–154]
            live mode → per-candidate csp_approve/csp_reject rows  [formatter.py:156–166]
```

**Gap A (paper):** `_auto_execute_staged` (telegram_bot.py:11453) reads
`pending_orders WHERE status='staged'` with **no** `engine` filter, **no** `run_id` filter,
and **no** check of `csp_pending_approval` or `csp_ticker_approvals`. Orders execute at 09:35
before the digest is even sent at 09:37. Yash sees execution results, not a pre-execution gate.

**Gap B (live):** `await_csp_approval` blocks the scan thread synchronously. Allocator does not
run until operator resolves or 90-min timeout. Digest at 09:37 may find a stale `csp_allocator_latest`
row if approval has not resolved yet.

**Gap C (both modes):** `build_inline_keyboard` (formatter.py:153–154) returns `[]` in paper mode.
No per-ticker buttons exist today even for observation. Fix D adds paper keyboard (Approve/Reject
for audit trail) while retaining paper auto-execute behavior on timeout.

### 2. CSP_PENDING_APPROVAL State Machine (cited)

**Initial insert** — `agt_equities/csp_digest/approval_gate.py:107`
Status: `'pending'` on create.

**Status transitions and SET sites:**

| Status | Code site | Note |
|--------|-----------|------|
| `pending` | `approval_gate.py:107` (insert) | initial |
| `approved` | `telegram_bot.py:12110–12117` (csp_submit action) | via old handler |
| `partial` | `approval_gate.py:159` and `approval_gate.py:203` | **LATENT BUG** — not in DB CHECK (see Correction 2) |
| `timeout` | `approval_gate.py:203` (sweep_timeouts) | sweep not wired to runtime |
| `rejected` | CHECK constraint only — never SET in code | dead value |
| `error` | CHECK constraint only — never SET in code | dead value |

**Prod schema** (`C:\AGT_Runtime\state\agt_desk.db` sqlite_master verified):
```sql
status TEXT NOT NULL DEFAULT 'pending'
       CHECK(status IN ('pending','approved','rejected','timeout','error'))
```
`'partial'` NOT in CHECK → IntegrityError on prod if triggered (unreachable today — paper
auto-executes before digest; live flow never exercised).

**Existing callback handler** — `telegram_bot.py:12014–12134`  
Registration — `telegram_bot.py:22707`:
```python
app.add_handler(CallbackQueryHandler(
    handle_csp_approval_callback,
    pattern=r"^csp_(?:approve|skip|submit):"
))
```
Parses: `csp_approve:<row_id:int>:<idx:int>` / `csp_skip:<row_id:int>:<idx:int>` / `csp_submit:<row_id:int>`

**`sweep_timeouts`** — `agt_equities/csp_digest/approval_gate.py:181–212`  
EXISTS but called only from tests. Not wired to any scheduler job or PTB job. **Missing piece for timeout-driven default.**

**`_record_digest_fired`** — `csp_digest_runner.py:162–185`  
Inserts into `csp_pending_approval` with `run_id="digest:{trade_date}"` — idempotency sentinel
for "digest already sent today", NOT a per-candidate approval request. New `csp_ticker_approvals`
rows must use the allocator's real `run_id` (from `load_latest_result()`), not this marker.

**`DEFAULT_TIMEOUT_MINUTES`**:
- `csp_digest_runner.py:42` — `DEFAULT_TIMEOUT_MINUTES = 90`
- `agt_equities/csp_digest/approval_gate.py:22` — `DEFAULT_TIMEOUT_MINUTES = 90`

### 3. Formatter + Keyboard (cited)

**`build_inline_keyboard`** — `agt_equities/csp_digest/formatter.py:144–173`

```python
# formatter.py:153–154 — paper mode returns empty list
if payload.mode != "LIVE":
    return []

# formatter.py:156–166 — live: per-candidate rows
for cand in payload.candidates:
    rows.append([
        {"text": f"✅ Approve {cand.ticker}",
         "callback_data": f"csp_approve:{run_id}:{cand.ticker}"},    # ← csp_ prefix
        {"text": f"❌ Reject {cand.ticker}",
         "callback_data": f"csp_reject:{run_id}:{cand.ticker}"},     # ← csp_ prefix
    ])

# formatter.py:167–172 — ALL row
rows.append([
    {"text": "✅ Approve ALL", "callback_data": f"csp_approve_all:{run_id}"},
    {"text": "❌ Reject ALL",  "callback_data": f"csp_reject_all:{run_id}"},
])
```

**Pattern collision (CRITICAL — Correction 1):**  
The existing handler (telegram_bot.py:22707) matches `r"^csp_(?:approve|skip|submit):"`.
This DOES match `csp_approve:{run_id}:{ticker}` (formatter.py:160).
The handler calls `int(parts[1])` expecting integer `row_id` but receives UUID `run_id` → `ValueError`.
`csp_reject:` is NOT matched by the existing handler (not in `approve|skip|submit`) → PTB drops it.

**Resolution: this MR renames formatter keyboard callbacks from `csp_approve:` to `cta_approve:`
(CSP Ticker Approval). Old handler is untouched; new handler registers `^cta_`.** See Fix — Callback.

**`render_card_text`** — `formatter.py:104–141`  
Iterates `ordered = sorted(payload.candidates, key=_key)` (line 119), calls
`_normal_card_body(cand, comm)` (line 122) for each. One card per `DigestCandidate`.
Fix D adds mode-keyed card body selection here.

**`build_digest_payload`** — `csp_digest_runner.py:65–124`  
Line 85: `for rank, t in enumerate(staged, start=1):` — one `DigestCandidate` per account ticket.
ARM with 3 accounts = 3 candidates = 3 button rows in live keyboard.
Fix C inserts grouping-by-ticker before this loop.

**`pending_orders` columns** (confirmed `telegram_bot.py:821–828`):
- `engine` — TEXT ('csp', 'cc', 'roll', 'harvest') — used for gate filter
- `run_id` — TEXT — joins to `csp_ticker_approvals.run_id`
- `broker_mode_at_staging` — TEXT — audit trail
- `payload` — JSON blob — `json_extract(payload, '$.ticker')` usable in SQL

---

## Scope

| Fix | What changes | Files touched | LOC est. |
|-----|-------------|--------------|----------|
| A — Approval-gate sequencing | `_auto_execute_staged` checks per-ticker CSP state via LEFT JOIN | `telegram_bot.py`, migration | +80 / ±30 |
| B — Timeout-driven default | `sweep_timeouts_with_default`; new `csp_timeout_sweeper` job; `AGT_CSP_TIMEOUT_DEFAULT` env var | `csp_digest/approval_gate.py`, `agt_scheduler.py` | +60 |
| C — Ticket dedup by ticker | `build_digest_payload` groups per-account tickets by ticker before DigestCandidate creation | `csp_digest_runner.py` | ±35 |
| D — Broker-mode-keyed digest | Split `_normal_card_body` into paper/live variants; paper gets buttons for audit trail | `csp_digest/formatter.py` | +40 |
| Schema | `csp_ticker_approvals` table + migration | `scripts/migrate_csp_ticker_approvals.py` | +60 |
| Callback | New `handle_csp_ticker_callback` + rename formatter callbacks to `cta_` prefix | `telegram_bot.py`, `formatter.py` | +70 |

**Total:** ~345 LOC additive, ~30 LOC modified. One atomic CRITICAL MR.

**Out of scope:**
- LLM commentary flow — `generate_commentary` path unchanged
- IVR%, VRP%, 52W range, analyst data — deferred to data-provider sprint
- `walker.py`, `flex_sync.py`, `rule_engine.py` — standing prohibitions
- `telegram_approval_gate` (approval_gate.py:236–382) synchronous poller — left in place; live promotion wires the new async gate at this policy point
- `csp_pending_approval` table — not modified; existing row format preserved for old handler backward compat

---

## Fix A — Approval-Gate Sequencing

**Goal:** `_auto_execute_staged` defers execution of CSP orders until the per-ticker approval
state is resolved. Non-CSP engines bypass the gate.

**New table: `csp_ticker_approvals`** (see Migration Runbook for full DDL)

**Flow change — three sites:**

**Site 1 — `run_csp_digest_job` (csp_digest_runner.py:188):**  
After `_record_digest_fired` (line 306), write one `csp_ticker_approvals` row per unique ticker
in `payload.candidates` with `status='pending'`, `timeout_at_utc=now+DEFAULT_TIMEOUT_MINUTES`.

**Site 2 — `handle_csp_ticker_callback` (new, telegram_bot.py):**  
Handles `cta_approve:{run_id}:{ticker}` and `cta_reject:{run_id}:{ticker}` taps. Writes to
`csp_ticker_approvals` (not `csp_pending_approval`). Also handles `cta_approve_all:{run_id}`
and `cta_reject_all:{run_id}` bulk actions. After writing approval, calls `_auto_execute_staged`
with a `ticker_filter` for immediate execution of approved tickers (see Q7).

**Site 3 — `_auto_execute_staged` (telegram_bot.py:11405):**  
Replace the READ phase query (currently line 11453: `SELECT id FROM pending_orders WHERE status='staged'`)
with a LEFT JOIN that includes the gate status:

```sql
-- New READ phase query in _auto_execute_staged
SELECT p.id, p.payload, p.run_id, p.engine,
       COALESCE(cta.status, 'not_gated') AS csp_gate_status
FROM pending_orders p
LEFT JOIN csp_ticker_approvals cta
    ON cta.run_id = p.run_id
   AND cta.ticker = json_extract(p.payload, '$.ticker')
WHERE p.status = 'staged'
  AND p.run_id IS NOT NULL
ORDER BY p.id
```

Gate logic per row:
- `engine != 'csp'` → `csp_gate_status = 'not_gated'` → execute (CC/roll/harvest bypass)
- `engine = 'csp'` and `csp_gate_status IN ('approved', 'timeout_approved')` → execute
- `engine = 'csp'` and `csp_gate_status IN ('rejected', 'timeout_rejected')` → skip; mark `pending_orders.status = 'csp_rejected'` (new terminal state)
- `engine = 'csp'` and `csp_gate_status = 'pending'` → skip (defer); do NOT claim, do NOT revert
- `engine = 'csp'` and `csp_gate_status = 'not_gated'` (no row for this run_id+ticker) → execute (backward compat for pre-gate rows and non-digest CSP runs)

Old rows with `run_id IS NULL` excluded by `WHERE p.run_id IS NOT NULL` — they bypass gate entirely.

**CAS modification:** The existing CAS claim (`UPDATE ... SET status='processing' WHERE id IN (...) AND status='staged'`) only runs for IDs that passed the gate. Deferred IDs never enter the CAS claim.

---

## Fix B — Timeout-Driven Default

**New env var:** `AGT_CSP_TIMEOUT_DEFAULT`  
- `auto_approve` — pending tickers become `timeout_approved` (paper default)  
- `auto_reject` — pending tickers become `timeout_rejected` (live default)  
Read at sweep time from `os.environ.get("AGT_CSP_TIMEOUT_DEFAULT", "auto_reject")`.

**New function in `agt_equities/csp_digest/approval_gate.py`:**

```python
def sweep_timeouts_with_default(
    db_path: str | Path,
    *,
    now_utc: datetime | None = None,
    timeout_default: str = "auto_reject",  # "auto_approve" | "auto_reject"
) -> int:
    """Sweep pending csp_ticker_approvals rows past timeout_at_utc.

    Flips status to timeout_approved or timeout_rejected per timeout_default.
    Returns count of rows swept.
    """
    now = now_utc or datetime.now(timezone.utc)
    new_status = "timeout_approved" if timeout_default == "auto_approve" else "timeout_rejected"
    decided_by = f"timeout_{timeout_default}"
    with get_db_connection(db_path=db_path) as conn:
        with tx_immediate(conn):
            n = conn.execute(
                "UPDATE csp_ticker_approvals SET status=?, decided_at_utc=?, decided_by=? "
                "WHERE status='pending' AND timeout_at_utc < ?",
                (new_status, now.isoformat(), decided_by, now.isoformat()),
            ).rowcount
    return n
```

**New scheduler job in `agt_scheduler.py` `register_jobs`:**

```python
def _csp_timeout_sweeper_job() -> None:
    import os
    from agt_equities.csp_digest.approval_gate import sweep_timeouts_with_default
    from agt_equities.db import get_db_path
    default = os.environ.get("AGT_CSP_TIMEOUT_DEFAULT", "auto_reject")
    n = sweep_timeouts_with_default(get_db_path(), timeout_default=default)
    if n:
        logger.info("csp_timeout_sweeper: swept %d rows default=%s", n, default)

scheduler.add_job(
    _csp_timeout_sweeper_job,
    trigger="interval",
    minutes=2,
    id="csp_timeout_sweeper",
    name="csp_timeout_sweeper",
    replace_existing=True,
)
```

Note: `_csp_timeout_sweeper_job` is a sync function → default ThreadPoolExecutor. No `executor="asyncio"` needed (not async def).

After each sweep that changes rows, the next `_auto_execute_staged` pass (if still running) or the timeout-triggered call from the sweeper will pick up `timeout_approved` rows. **Open: Q7 — does sweeper trigger `_auto_execute_staged` directly after a timeout-approve sweep?** Recommendation: yes, call `asyncio.run_coroutine_threadsafe(_auto_execute_staged(), loop)` from the sync sweeper job.

---

## Fix C — Ticket Dedup by Ticker

**Change in `build_digest_payload` (csp_digest_runner.py:85):**

Replace `for rank, t in enumerate(staged, start=1):` with a grouping step:

```python
# Group per-account tickets by ticker, preserving screener rank order
from collections import defaultdict as _dd
ticker_groups: dict[str, list[dict]] = _dd(list)
for t in staged:
    ticker_groups[str(t.get("ticker", "?")).upper()].append(t)

seen_order = list(dict.fromkeys(
    str(t.get("ticker", "?")).upper() for t in staged
))

candidates: list[DigestCandidate] = []
for rank, ticker in enumerate(seen_order, start=1):
    tickets = ticker_groups[ticker]
    ref = tickets[0]  # screener fields from first (representative) ticket
    per_account = [(str(t.get("account_id", "")), int(t.get("quantity") or 0))
                   for t in tickets]
    candidates.append(DigestCandidate(
        rank=rank,
        ticker=ticker,
        strike=float(ref.get("strike") or 0.0),
        expiry=str(ref.get("expiry") or ""),
        premium_dollars=float(
            ref.get("premium_dollars") or
            (ref.get("limit_price", 0) * 100) or
            (ref.get("mid", 0) * 100)
        ),
        premium_pct=float(ref.get("premium_pct") or 0.0),
        ray_pct=float(ref.get("annualized_yield") or ref.get("ray_pct") or 0.0),
        delta=float(ref.get("delta") or 0.0),
        otm_pct=float(ref.get("otm_pct") or 0.0),
        ivr_pct=float(ref.get("ivr_pct") or 0.0),
        vrp=float(ref.get("vrp") or 0.0),
        fwd_pe=ref.get("fwd_pe"),
        week52_low=float(ref.get("week52_low") or 0.0),
        week52_high=float(ref.get("week52_high") or 0.0),
        spot=float(ref.get("spot") or ref.get("spot_at_staging") or 0.0),
        week52_pct_of_range=float(ref.get("week52_pct_of_range") or 0.0),
        analyst_avg=ref.get("analyst_avg"),
        analyst_sources_blurb=ref.get("analyst_sources_blurb"),
        per_account=per_account,
        ivr_benchmark_median=ref.get("ivr_benchmark_median"),
        vrp_benchmark_median=ref.get("vrp_benchmark_median"),
    ))
```

**Effect:** ARM staged for accounts DUP751003/004/005 → 1 DigestCandidate (rank 1) with
`per_account=[("DUP751003",5),("DUP751004",5),("DUP751005",5)]`. `_account_blurb` at
`formatter.py:47–53` already handles multi-account rendering correctly.

---

## Fix D — Broker-Mode-Keyed Digest View

**Change in `csp_digest/formatter.py:104` (`render_card_text`):**  
Add `mode=payload.mode` arg to `_normal_card_body` → route to paper or live card body variant.

**Split `_normal_card_body` (formatter.py:56) into two paths:**

```python
def _paper_card_body(c: DigestCandidate, comm: DigestCommentary | None) -> str:
    """Paper: ticker + per-account split — Phase B transparency."""
    accounts = _account_blurb(c.per_account)
    line1 = f"CSP #{c.rank} — {c.ticker} ${c.strike:g}P {c.expiry}  {accounts}".rstrip()
    # lines 2–5 identical to current _normal_card_body
    ...

def _live_card_body(c: DigestCandidate, comm: DigestCommentary | None) -> str:
    """Live: ticker + total notional — clean approval UI (no per-account split)."""
    total_contracts = sum(n for _, n in c.per_account)
    total_notional = total_contracts * c.strike * 100
    line1 = (
        f"CSP #{c.rank} — {c.ticker} ${c.strike:g}P {c.expiry}  "
        f"({total_contracts} contracts, ${total_notional:,.0f} notional)"
    )
    # lines 2–5 identical (analytics retained for live decision)
    ...
```

**Paper keyboard change (Fix D extension):**  
`build_inline_keyboard` currently returns `[]` for paper (formatter.py:153–154). Option C adds
paper buttons as an audit trail so Yash can explicitly approve/reject tickers before execution
even in paper mode. Paper approval is still `auto_approve` on timeout so Phase B clock is
never blocked by a forgotten tap.

**Updated `build_inline_keyboard` logic:**
```python
# Paper: show buttons but auto-execute on timeout (AGT_CSP_TIMEOUT_DEFAULT=auto_approve)
# Live: show buttons; no execute until operator taps or timeout-reject
rows: list[list[dict]] = []
for cand in payload.candidates:
    rows.append([
        {"text": f"✅ Approve {cand.ticker}",
         "callback_data": f"cta_approve:{run_id}:{cand.ticker}"},
        {"text": f"❌ Reject {cand.ticker}",
         "callback_data": f"cta_reject:{run_id}:{cand.ticker}"},
    ])
rows.append([
    {"text": "✅ Approve ALL", "callback_data": f"cta_approve_all:{run_id}"},
    {"text": "❌ Reject ALL",  "callback_data": f"cta_reject_all:{run_id}"},
])
return rows
```

Note: Paper mode NO LONGER returns `[]`. This changes the mode check from `if payload.mode != "LIVE": return []` to unconditional keyboard emission. The paper auto-execute path (`PAPER_AUTO_EXECUTE`) must be updated to check `csp_ticker_approvals` status rather than executing immediately after staging (aligns with Fix A).

---

## Fix — Callback Handler

**Pattern collision resolved:**  
Existing handler (telegram_bot.py:22707) matches `r"^csp_(?:approve|skip|submit):"`.  
This matches `csp_approve:{run_id}:{ticker}` (formatter.py:160) → `int(parts[1])` on UUID run_id → `ValueError`.  
`csp_reject:{run_id}:{ticker}` falls through (reject not in old pattern) → PTB drops silently.

**This MR renames formatter callbacks from `csp_` to `cta_` prefix and registers a new handler.**

**New handler `handle_csp_ticker_callback`** in `telegram_bot.py`:

```python
async def handle_csp_ticker_callback(update, context):
    """Handle cta_approve/reject/approve_all/reject_all taps."""
    query = update.callback_query
    await query.answer()
    data = query.data  # e.g. "cta_approve:abc123:ARM"
    parts = data.split(":")
    action = parts[0]  # cta_approve | cta_reject | cta_approve_all | cta_reject_all
    run_id = parts[1] if len(parts) > 1 else ""
    ticker = parts[2] if len(parts) > 2 else None

    if action in ("cta_approve", "cta_reject"):
        status = "approved" if action == "cta_approve" else "rejected"
        _write_csp_ticker_approval(run_id, ticker, status, decided_by="operator")
        if status == "approved":
            await _auto_execute_staged(ticker_filter=ticker)
    elif action == "cta_approve_all":
        _write_csp_ticker_approval_bulk(run_id, status="approved", decided_by="operator")
        await _auto_execute_staged()
    elif action == "cta_reject_all":
        _write_csp_ticker_approval_bulk(run_id, status="rejected", decided_by="operator")
    await query.edit_message_reply_markup(reply_markup=None)  # clear buttons after action
```

**Registration (telegram_bot.py, near line 22707):**
```python
app.add_handler(CallbackQueryHandler(
    handle_csp_ticker_callback,
    pattern=r"^cta_(?:approve|reject)(?:_all)?:"
))
```

No change to existing `handle_csp_approval_callback` registration at line 22707.

**Formatter rename:** `build_inline_keyboard` (formatter.py:160, 163, 168, 170) changes
`csp_approve:` → `cta_approve:` and `csp_reject:` → `cta_reject:` and
`csp_approve_all:` → `cta_approve_all:` / `csp_reject_all:` → `cta_reject_all:`.

---

## Schema

**New table: `csp_ticker_approvals`**

```sql
CREATE TABLE IF NOT EXISTS csp_ticker_approvals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','approved','rejected',
                                      'timeout_approved','timeout_rejected')),
    timeout_at_utc   TEXT NOT NULL,
    decided_at_utc   TEXT,
    decided_by       TEXT,   -- 'operator' | 'timeout_auto_approve' | 'timeout_auto_reject'
    UNIQUE(run_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_cta_run_id   ON csp_ticker_approvals(run_id);
CREATE INDEX IF NOT EXISTS idx_cta_status   ON csp_ticker_approvals(status, run_id);
CREATE INDEX IF NOT EXISTS idx_cta_timeout  ON csp_ticker_approvals(timeout_at_utc)
    WHERE status = 'pending';
```

Notes:
- No `partial` status — latent bug in `csp_pending_approval` not replicated here
- `timeout_at_utc` required for sweeper query `WHERE status='pending' AND timeout_at_utc < ?`
- Partial index on `timeout_at_utc` optimizes sweeper scan for pending-only rows

**`csp_pending_approval` table — NO CHANGES.** Old handler and old keyboard format preserved for backward compat. Old table's `partial` bug is a separate cleanup item.

**New terminal state in `pending_orders`:** `csp_rejected`  
Requires verifying `pending_orders.status` CHECK constraint allows this value — **INFERRED** that the CHECK is open (status TEXT, no CHECK) based on migration history. Needs verification before code.

---

## Migration Runbook

**File: `scripts/migrate_csp_ticker_approvals.py`**

```python
"""Additive migration: CREATE TABLE IF NOT EXISTS csp_ticker_approvals."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS csp_ticker_approvals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','approved','rejected',
                                      'timeout_approved','timeout_rejected')),
    timeout_at_utc   TEXT NOT NULL,
    decided_at_utc   TEXT,
    decided_by       TEXT,
    UNIQUE(run_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_cta_run_id  ON csp_ticker_approvals(run_id);
CREATE INDEX IF NOT EXISTS idx_cta_status  ON csp_ticker_approvals(status, run_id);
CREATE INDEX IF NOT EXISTS idx_cta_timeout ON csp_ticker_approvals(timeout_at_utc)
    WHERE status = 'pending';
"""
```

Idempotent (`CREATE TABLE IF NOT EXISTS`). No backup required — additive only, no ALTER on existing tables.

**Optional (if Q4=yes): extend `operator_interventions_kind_check` to include `timeout_auto_approve` and `timeout_auto_reject` in the CHECK constraint.** Table-recreate migration (same pattern as Sprint 13 MR !271). Bundle in same MR commit.

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| `_auto_execute_staged` LEFT JOIN on `pending_orders.run_id` misses old (NULL run_id) rows | Low | Low | `WHERE p.run_id IS NOT NULL` excludes NULLs; they bypass gate entirely (backward compat) |
| `engine` column is NULL for pre-MR rows | Low | Low | `COALESCE(engine,'') != 'csp'` → not_gated |
| `pending_orders.status='csp_rejected'` violates a CHECK constraint | Medium | Medium | **INFERRED no CHECK** — must verify before code |
| `sweep_timeouts_with_default` fires before digest row written | None | Medium | Digest writes rows at send time with `timeout_at_utc = now + 30min`; sweeper can't match until timeout passes |
| `_auto_execute_staged(ticker_filter=)` new parameter breaks existing callers | Low | High | Default `ticker_filter=None` → existing behavior unchanged |
| New handler not registered (pattern typo) | Medium | High | CI test: assert `handle_csp_ticker_callback` in app.handlers for pattern `^cta_` |
| `operator_interventions` CHECK violation on new kind values (if Q4=yes) | High | Medium | Bundle kind-check migration in same commit; test writes new kind values |
| Paper keyboard now emits buttons — `agentbot` or other callers check `keyboard == []` | Low | Low | Search `build_inline_keyboard` callers before code |

---

## LOC Estimate

| File | Added | Modified |
|------|-------|---------|
| `scripts/migrate_csp_ticker_approvals.py` (new) | 60 | — |
| `agt_equities/csp_digest/approval_gate.py` | 45 | 0 |
| `csp_digest_runner.py` | 50 | 25 |
| `agt_equities/csp_digest/formatter.py` | 40 | 15 |
| `agt_scheduler.py` | 25 | 5 |
| `telegram_bot.py` (new handler + gate patch) | 90 | 35 |
| `tests/test_sprint14_p2_*.py` (new) | 120 | — |
| **Total** | **430** | **80** |

---

## Architect Decision Questions

**Q1 — Timeout duration for `csp_ticker_approvals`:**  
Default `DEFAULT_TIMEOUT_MINUTES = 90` in both csp_digest_runner.py:42 and approval_gate.py:22.
The sweeper interval is 2 min (fast enough to react; won't burn DB).
- 90 min: consistent with existing; gives Yash pre-market + lunch window
- 30 min: matches dispatch default; reasonable for 09:37 digest with 10:07 timeout
- 60 min: middle ground

**Recommendation: 30 min for `csp_ticker_approvals`** (new per-ticker gate). Keep 90 min default
for old `csp_pending_approval` (backward compat, not changed in this MR).

**Q2 — Rejected ticker audit retention:**  
Recommendation: retain all rows in `csp_ticker_approvals` indefinitely (or 30-day archive
matching heartbeat_samples pattern). No DELETE on timeout or rejection. RIA compliance trail.

**Q3 — Per-ticker omission handling (EXPE not tapped before timeout):**  
If Yash approves ARM but doesn't tap EXPE before timeout:  
- Sweeper flips EXPE to `timeout_approved` (paper) or `timeout_rejected` (live) per `AGT_CSP_TIMEOUT_DEFAULT`
- `decided_by = 'timeout_auto_approve'` or `'timeout_auto_reject'` for audit clarity
- Yash can still tap "Reject EXPE" after a `timeout_approved` flip — new tap overwrites status

Recommendation: **sweeper handles omission** (Option A from prior draft). No separate "rejected_by_omission" state. Explicit tap after auto-approve = operator override, loggable in `operator_interventions`.

**Q4 — `operator_interventions` for approval/reject actions:**  
Recommendation: **yes** — each operator tap writes `kind='approve'` or `kind='reject'` row
to `operator_interventions` (target_table='csp_ticker_approvals'). Timeout sweeper writes
`kind='timeout_auto_approve'` or `kind='timeout_auto_reject'`.  
Requires extending CHECK constraint (bundle migration in same MR).  
VALID_KINDS additions: `'csp_ticker_approve'`, `'csp_ticker_reject'`,
`'csp_timeout_auto_approve'`, `'csp_timeout_auto_reject'`.

**Q5 — Phase B verdict impact:**  
No change. Engine activity = orders staged > 0. Approval/rejection is the operator gate,
not an activity metric. If scan runs and generates 10 candidates but all timeout-rejected:
that is a valid "no-execute day" for Phase B, not a missed-scan day.  
Phase B clock runs on `scan ran + candidates staged`. Approval outcome is separate.

**Q6 — Callback prefix (`cta_` confirmed or alternative?):**  
Recommendation: use `cta_` (CSP Ticker Approval). No overlap with any existing handler.
Architect to confirm or propose alternative before code.

**Q7 — Immediate execute on tap vs sweeper-only:**  
Recommendation: **immediate execute on tap** for approved tickers.  
When operator taps "Approve ARM": `handle_csp_ticker_callback` writes approval, then calls
`await _auto_execute_staged(ticker_filter="ARM")`. No waiting for next sweeper pass.  
Timeout-driven approvals: sweeper calls `asyncio.run_coroutine_threadsafe(_auto_execute_staged(), loop)`.  
This requires `_auto_execute_staged` to accept an optional `ticker_filter: str | None = None` arg.

---

## Implementation Order (single MR)

1. `scripts/migrate_csp_ticker_approvals.py` — schema
2. `agt_equities/csp_digest/approval_gate.py` — `sweep_timeouts_with_default`
3. `csp_digest_runner.py` — Fix C (dedup by ticker) + insert `csp_ticker_approvals` rows after send
4. `agt_equities/csp_digest/formatter.py` — Fix D (paper/live cards) + rename `csp_→cta_` in keyboard
5. `agt_scheduler.py` — `_csp_timeout_sweeper_job`
6. `telegram_bot.py` — `handle_csp_ticker_callback` + gate in `_auto_execute_staged` (Fix A)
7. Tests for each surface

All seven items in one squash-commit. CRITICAL tier — full CI green required before merge.

---

## Pre-Code Blockers (Architect answers required before implementation begins)

1. **Q1** — Timeout duration (30 min recommended)
2. **Q4** — operator_interventions: yes/no (yes recommended; if no, skip kind-check migration)
3. **Q6** — `cta_` prefix: confirm or propose alternative
4. **`pending_orders.status` CHECK constraint** — verify allows `'csp_rejected'` (need sqlite_master check before writing the gate code)
5. **Q7** — immediate execute on tap: confirm pattern
