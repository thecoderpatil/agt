# ADR-008 — Shadow Scan via OrderSink + RunContext + DecisionSink

**Status:** Draft
**Date:** 2026-04-17
**Author:** Architect (Cowork)
**Reviewers:** Codex, Gemini, Deep Research (all converged)
**Supersedes:** Env-var + collector seam (rejected)

---

## 1. Problem

We run CC / CSP-allocator / harvest / roll engines against the live portfolio. Today there is no way to exercise the full decision surface end-to-end against real account state *without* staging orders, updating `pending_orders`, writing `cc_cycle_log`, writing `bucket3_dynamic_exit_log`, and mutating glide paths. That coupling blocks three things we actively need:

1. **Triageable digests.** We want a one-command "dry run" that prints every order the four engines *would* stage against today's real positions, for Cowork-side review, without touching a single table.
2. **Adversarial backtesting.** We want to replay historical account states through the current decision tree to hunt bugs that the paper desk is too quiet to surface (empty portfolio, stable leverage).
3. **Refactor safety.** Every time we edit `_run_cc_logic`, `_scan_and_stage_defensive_rolls`, `run_csp_allocator`, or `scan_csp_harvest_candidates` we are one-shot-ing production paths with no differential test to tell us we broke the decision itself (as opposed to the plumbing).

## 2. Rejected path — env var + collector

Initial proposal was `AGT_SHADOW_MODE=true` read from a module-level flag, with a `CollectorOrderSink` swapped in by a wrapper script. Three reviewers (Codex, Gemini pro, Gemini Deep Research) independently rejected it on the same grounds:

- **Concurrent state bleed (Critical).** Module-level env reads are process-global. The scheduler, Telegram command handlers, and an ad-hoc shadow invocation share one process. Flipping the flag mid-run re-routes *in-flight* production staging into the collector.
- **Hidden mutative side effects (High).** `_run_cc_logic` does more than call `append_pending_tickets`. It `UPDATE ... SET status='superseded'`, it writes `cc_cycle_log`, it writes `bucket3_dynamic_exit_log`, it touches glide paths. An order-level shim does not trap those writes.
- **Phantom margin drift (Medium).** Even read-only IB calls (`reqMktData`, `accountSummary`, chain walks) churn pacing counters and the in-memory `accountValues` dict. A shadow run against the live Gateway taints the next live decision.

None of those are fixable by "just be careful with the env var." They require an explicit composition seam below the CLI boundary.

## 3. Decision — OrderSink + RunContext + DecisionSink

Adopt three small protocols and a frozen context object. Every engine that today writes to SQLite or pushes orders to IB gets a **ctx** parameter and calls **ctx.order_sink.stage(...)** / **ctx.decision_sink.record_*(...)** instead. The caller at the composition root (scheduler daemon, CLI entry, Telegram handler) picks which sinks to wire in.

```
agt_equities/runtime.py         # RunMode, RunContext, OrderSink, DecisionSink protocols
agt_equities/sinks.py           # SQLiteOrderSink, CollectorOrderSink, SQLiteDecisionSink,
                                # CollectorDecisionSink, NullDecisionSink
scripts/shadow_scan.py          # CLI: build shadow ctx, run scans, emit digest + JSON
```

### 3.1 Core types

```python
class RunMode(str, Enum):
    LIVE = "live"
    SHADOW = "shadow"

class OrderSink(Protocol):
    def stage(self, tickets: list[dict], *, engine: str, run_id: str,
              meta: dict | None = None) -> None: ...

class DecisionSink(Protocol):
    def record_cc_cycle(self, entries: list[dict], *, run_id: str) -> None: ...
    def record_dynamic_exit(self, entries: list[dict], *, run_id: str) -> None: ...

@dataclass(frozen=True)
class RunContext:
    mode: RunMode
    run_id: str
    order_sink: OrderSink
    decision_sink: DecisionSink
    db_path: str | None = None
```

### 3.2 Sinks

| Sink class | Mode | Behavior |
|---|---|---|
| `SQLiteOrderSink` | LIVE | Calls existing `append_pending_tickets` / `supersede_existing_staged_rows`. |
| `CollectorOrderSink` | SHADOW | Appends to in-memory `list[ShadowOrder]`, thread-safe via `Lock`. |
| `SQLiteDecisionSink` | LIVE | Calls `_log_cc_cycle`, `persist_dynamic_exit_rows`. |
| `CollectorDecisionSink` | SHADOW | Appends to in-memory `list[ShadowDecision]`. |
| `NullDecisionSink` | (tests) | No-op. |

### 3.3 Composition — live vs shadow

```python
# live (scheduler / Telegram handlers / dev_cli.py)
ctx = RunContext(
    mode=RunMode.LIVE,
    run_id=uuid.uuid4().hex,
    order_sink=SQLiteOrderSink(append_pending_tickets),
    decision_sink=SQLiteDecisionSink(_log_cc_cycle, persist_dynamic_exit_rows),
    db_path=PROD_DB_PATH,
)

# shadow (scripts/shadow_scan.py)
shadow_db = clone_sqlite_db_with_wal(PROD_DB_PATH)   # temporary safety belt
order_sink = CollectorOrderSink()
decision_sink = CollectorDecisionSink()
ctx = RunContext(
    mode=RunMode.SHADOW,
    run_id=uuid.uuid4().hex,
    order_sink=order_sink,
    decision_sink=decision_sink,
    db_path=shadow_db,
)
```

### 3.4 Temporary SQLite clone

Codex's pragmatic addition, adopted: **until CC and dynamic-exit writes are fully extracted to `DecisionSink`, shadow runs against a cloned `agt_desk.db` copy.** That gives immediate containment for any residual `conn.execute("UPDATE ...")` call we haven't hunted down yet. The clone is a belt-and-suspenders, not the end state.

Clone helper: copy `agt_desk.db` + `-wal` + `-shm` to a tmpdir via `sqlite3.Connection.backup()` (handles live WAL frames). Shadow ctx points `db_path` at the tmpdir copy. Run ends, tmpdir is deleted. Under no circumstance does shadow_scan accept the production `DB_PATH`.

## 4. Migration plan — strangler fig

Five MRs, each independently mergeable, each keeping CI green. Order matters — we ship the plumbing with zero behavior change, then flip engines over one at a time.

### MR 1 — Plumbing (zero behavior change)

- Add `agt_equities/runtime.py` + `agt_equities/sinks.py`.
- Add `clone_sqlite_db_with_wal` helper.
- Add `scripts/shadow_scan.py` skeleton that builds a shadow ctx, calls a stubbed "no engines yet" path, prints empty digest.
- Tests: instantiation + sink contract coverage, including `CollectorOrderSink` thread-safety under 8 concurrent producers.
- **Invariants:** new `NO_SHADOW_SQLITE_ON_PROD_DB` guard in `agt_equities/invariants/checks.py` — if `shadow_scan.py` is ever invoked with `db_path == PROD_DB_PATH` (`C:\AGT_Telegram_Bridge\agt_desk.db`), raise at entry.

### MR 2 — CSP allocator opt-in

CSP allocator already has a `staging_callback` seam; this MR rewires it to `ctx.order_sink.stage`. Change is local.

- `run_csp_allocator(..., ctx: RunContext)` — add required ctx parameter.
- All call sites (`telegram_bot.py::_csp_allocator_job_impl`, `dev_cli.py::scan_csp`, scheduler) build a live ctx and pass through.
- `scripts/shadow_scan.py --engine csp` starts working end-to-end.
- Tests: existing CSP allocator tests pass; new `test_csp_allocator_shadow_mode.py` verifies `CollectorOrderSink` captures the ticket and **no row lands in `pending_orders`**.

### MR 3 — CSP harvest opt-in

Same shape as MR 2. `scan_csp_harvest_candidates(ib_conn, ctx: RunContext)`.

- `scripts/shadow_scan.py --engine harvest` works.
- Paper Gateway is fine as the IB data source for harvest because harvest only reads; no mktData pacing pressure worth worrying about in a one-shot invocation.

### MR 4 — Roll engine opt-in

`roll_engine.stage_roll_orders(..., ctx: RunContext)`.

- Roll has the simplest order-staging path; no cycle-log or dynamic-exit writes.
- `scripts/shadow_scan.py --engine roll` works.

### MR 5 — CC engine split (the hard one)

This is the one that actually justifies `DecisionSink`. Today `_run_cc_logic` is monolithic: it scans, picks strikes, writes `cc_cycle_log`, writes `bucket3_dynamic_exit_log`, stages orders, and calls `supersede_existing_staged_rows` — all interleaved. Before it can be shadow-safe we split decision production from decision persistence:

```python
@dataclass
class CCScanResult:
    staged_orders: list[dict]
    cycle_log_entries: list[dict]
    dynamic_exit_entries: list[dict]
    skipped: list[dict]
    digest_lines: list[str]

async def collect_cc_recommendations(...) -> CCScanResult: ...
```

Then the emit step becomes:

```python
cc_result = await collect_cc_recommendations(...)
if cc_result.staged_orders:
    if ctx.is_live:
        supersede_existing_staged_rows(db_path=ctx.db_path)
    ctx.order_sink.stage(cc_result.staged_orders, engine="cc_engine", run_id=ctx.run_id)
if cc_result.cycle_log_entries:
    ctx.decision_sink.record_cc_cycle(cc_result.cycle_log_entries, run_id=ctx.run_id)
if cc_result.dynamic_exit_entries:
    ctx.decision_sink.record_dynamic_exit(cc_result.dynamic_exit_entries, run_id=ctx.run_id)
```

- The `supersede` call stays gated on `ctx.is_live` — we do *not* want shadow runs racing the live auto-executor on the same `pending_orders.status` column even inside a cloned DB (the clone is a defense, not the reason).
- Tests: differential snapshot — capture live-mode `_run_cc_logic` output today, refactor, assert identical tickets produced in live mode; add shadow-mode test that proves no DB writes occur.
- After MR 5 lands, the SQLite clone from §3.4 becomes optional. We keep it for one sprint as a safety margin, then drop.

### MR 6 — Telegram digest emitter

`scripts/shadow_scan.py --emit telegram` pipes the drained `CollectorOrderSink` + `CollectorDecisionSink` contents to Telegram as a single rendered digest. Used for Cowork-side triage.

- Format: one message per engine, ticker-sorted, `ShadowOrder` → bullet with `engine / ticker / right / strike / qty / limit / decided_at / meta.household`.
- JSON sibling always written to `reports/shadow_scan_<run_id>.json` so Cowork can `Read` it directly.
- No staging, no approval gate — this is observation-only.

## 5. Out of scope (explicitly)

- **Cached LLM replay.** Deep Research suggested hash-keyed LLM caching for cost control. Not in v1. The shadow scan calls the real Opus if the engine under test calls it (today only CSP allocator's `candidate_reasoning` seam does). Spend is bounded by shadow-scan invocation cadence, which is operator-gated.
- **Mock IB gateway.** We do not build a `MockBroker` for v1. Shadow scan reads from the real Gateway (paper or live, per `--gateway` flag). Market-data pacing is managed by existing `reqMktData` chunking + `cancelMktData` try/finally (shipped MR !62-!64).
- **Autonomous scheduling.** Shadow scan is a CLI + ad-hoc Telegram command. It is not added to APScheduler. Cowork triggers it when reviewing.
- **CSP allocator autonomy.** Per AGT end-state vision, CSP selection never goes autonomous; shadow mode is purely additive to the Telegram-digest review flow.

## 6. Invariant additions

Under `agt_equities/invariants/`, ADR-007 style:

- **`NO_SHADOW_ON_PROD_DB`** — manifest severity=high, scrutiny_tier=architect_only. Tripped if `shadow_scan.py` process spawns with `AGT_DB_PATH == PROD_DB_PATH`.
- **`NO_LIVE_CTX_IN_SHADOW_SCRIPT`** — high / architect_only. Runtime assert inside `scripts/shadow_scan.py`: after ctx construction, `assert ctx.mode is RunMode.SHADOW`.
- **`NO_ORDER_STAGE_OUTSIDE_SINK`** — medium / high. Static check via `grep -rn "append_pending_tickets\|pending_orders.*INSERT"` limited to `sinks.py` — any other file referencing those call sites fails CI.

## 7. Reviewer convergence

All three reviewers (Gemini pro, Deep Research, Codex) independently recommended the same structural shape: composition-root DI over runtime env branching, with sinks as the seam. Codex's additional points adopted:

- `DecisionSink` alongside `OrderSink` (not just orders — every mutating write).
- Temporary SQLite clone during MR 2-4 to contain residual writes in the engines we haven't extracted yet.
- Run-context is `@dataclass(frozen=True)` with `run_id` so every shadow artifact (JSON, digest, log line) is correlatable.
- No cached LLM / fake market data in v1 — scope discipline.

## 8. Success criteria

1. `python scripts/shadow_scan.py --engine all --gateway paper --emit json` runs end-to-end against today's real portfolio and produces a `reports/shadow_scan_<run_id>.json` with all tickets every engine would have staged.
2. Grepping `pending_orders` / `cc_cycle_log` / `bucket3_dynamic_exit_log` timestamps before and after the shadow run shows **zero** new rows in the production DB.
3. Differential test: running the same state through live ctx vs shadow ctx produces byte-identical order tickets (proves the split did not change decision logic).
4. Invariants `NO_SHADOW_ON_PROD_DB` and `NO_LIVE_CTX_IN_SHADOW_SCRIPT` are armed and populating incidents if tripped.
5. CI stays green through all 6 MRs; post-MR-6 pytest count ≥ `current + 20` (roughly the new test surface we're adding).

## 9. Timeline

- MR 1 (plumbing): 1 session
- MR 2 (CSP allocator): 1 session, trivial given existing staging_callback
- MR 3 (CSP harvest): 1 session
- MR 4 (roll): 0.5 session
- MR 5 (CC split): 2 sessions, **this is the risk-concentrated one**, needs differential harness before any refactor
- MR 6 (digest emitter): 0.5 session

Total: ~6 sessions. No live-capital impact at any step — the first five MRs keep live-mode behavior byte-identical and only add the shadow lane.

---

**Next action when resumed:** start MR 1 (plumbing). Files to create: `agt_equities/runtime.py`, `agt_equities/sinks.py`, `scripts/shadow_scan.py` skeleton, `tests/test_shadow_scan_plumbing.py`. Zero engine changes in MR 1.
