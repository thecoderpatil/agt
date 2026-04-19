# ADR-010 — CSP Approval Digest Composer (live Telegram approval gate)

**Status:** Draft v2 — Phase 1 shipped; Phase 2 Composer design locked
**Date:** 2026-04-18 (v1), 2026-04-18 (v2 update)
**Author:** Architect (Cowork, Opus)
**Supersedes:** ADR-010 v1 (2026-04-18, Phase 1 scope only — Phase 1 content preserved verbatim in §3 below)
**Related:** ADR-008 (Shadow Scan — DecisionSink), ADR-012 (Learning Loop — approval outcomes feed prompt amendment), ADR-013 (Self-Healing v2 — Pydantic boundary applies to LLM response parsing), ADR-015 (Level-4 Tier Migration — composer is prerequisite for live CSP autonomy), MR !69 (CSPCandidate Protocol + approval_gate seam, `60f65be9`), `project_end_state_vision.md` (memory)

---

## 1. Context

MR !69 (`60f65be9`, 2026-04-16) shipped the structural seam:

```python
run_csp_allocator(..., approval_gate: Callable[[list[CSPCandidate]], list[CSPCandidate]] = identity)
```

Phase 1 of this ADR (v1, 2026-04-18) shipped the Telegram-backed state machine — `csp_pending_approval` table + `telegram_approval_gate()` function in `agt_equities/csp_approval_gate.py`. Phase 1 provides the operator hand-off (Yash taps ✅/⏭ on a bare candidate digest) but carries no LLM reasoning — `_build_digest_text()` is static formatting.

Phase 2 was originally sketched as "add LLM ranking + reasoning" (v1 §3.2, ~400–500 LOC). When Dispatch E attempted to ship a `cached_client` wrapper for the Anthropic SDK call ahead of the composer, recon proved zero `messages.create` call sites exist in the codebase — wrapping a nonexistent caller is dead code. Dispatch E was cancelled with a sequencing fix: **write the composer first, then wrap.**

This ADR v2 locks the Phase 2 Composer design in full implementation detail so that:

1. The composer module (`agt_equities/csp_digest_composer.py`) has an unambiguous Coder-ready spec.
2. The `cached_client` wrapper (`agt_equities/cached_client.py`) has a concrete target to wrap — the composer's sole LLM entry point.
3. Fail-open semantics are explicit — any composer error path returns the Phase 1 bare digest, never blocks the approval gate.
4. Budget, observability, and invariant coverage are defined before the first LLM token fires in production.

The composer is the first production LLM call in AGT. ADR-010 Phase 2 is therefore the de-facto standard for all future AGT LLM integrations — ADR-014 (Synthetic Data Eval) references this ADR's cached_client contract verbatim for scenario-bank evaluation calls.

---

## 2. Scope

**In scope (Phase 2):**
- `csp_digest_composer.py` — module structure, entry points, input/output contracts
- LLM prompt design (system + user templates, JSON-constrained output)
- News ingestion integration (Finnhub existing client, per-ticker with TTL cache)
- `cached_client.py` — Anthropic SDK wrapper with prompt caching + response caching + daily budget
- Error handling — timeout, parse failure, API error, schema drift → all collapse to Phase 1 bare digest fallback
- Decision audit trail — every composer call persisted to `decisions_repo` with full prompt hash + response metadata
- Pydantic v2 validation of LLM response per ADR-013 Layer 1 pattern
- Phase 2 invariants (`NO_UNCACHED_LLM_CALL_IN_HOT_PATH`, `NO_COMPOSER_EXCEPTION_WITHOUT_FALLBACK`, `NO_DIGEST_LLM_BUDGET_OVERRUN`)
- Test harness — deterministic mock client, scenario bank, parse-robustness suite

**Out of scope:**
- Phase 1 re-design (shipped, preserved in §3 for completeness)
- Phase 3 deferred items (prompt caching TTL tuning, multi-day memory, ranking feedback loop) — §7
- News client replacement (Finnhub stays; replace only if rate-limit becomes operational issue)
- Multi-model routing (single Sonnet model; Opus escalation deferred to ADR-015)
- News feed separate from per-ticker (macro-headline integration deferred)
- Allocator-internal changes (MR !69 seam preserved verbatim)

---

## 3. Phase 1 — As Shipped (2026-04-18)

The following reflects `agt_equities/csp_approval_gate.py` at `568ba4e0`. Preserved for ADR completeness; no changes proposed.

### 3.1 Table — `csp_pending_approval`

```sql
CREATE TABLE csp_pending_approval (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    household_id TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL,
    sent_at_utc TEXT NOT NULL,
    timeout_at_utc TEXT NOT NULL,
    telegram_message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','rejected','timeout','error')),
    approved_indices_json TEXT,
    resolved_at_utc TEXT,
    resolved_by TEXT
);
```

### 3.2 Gate function

`telegram_approval_gate(candidates, *, db_path=None, timeout_minutes=30) -> list[CSPCandidate]`:

1. Persist batch to `csp_pending_approval` (status=`pending`, 30-min timeout).
2. Send Telegram digest via raw `requests.post` (matches `telegram_utils` pattern; no PTB dependency in allocator path).
3. Render bare digest (`_build_digest_text`) — one line per candidate: `{i+1}. <b>{ticker}</b> ${strike:.0f}P {expiry} ${mid:.2f} ({yield:.1%}/yr) [{household}]`.
4. Inline keyboard: per-candidate ✅/⏭ + single Submit.
5. Poll `csp_pending_approval.status` every 5s until resolved or timeout.
6. On `approved`: return `[candidates[i] for i in json.loads(approved_indices_json) if 0 <= i < n]`.
7. On `rejected`, `timeout`, or `error`: return `[]` (fail-closed for live capital).
8. Fail-open on DB insert failure (return identity — logged warning).

### 3.3 Composition root wiring

Env-driven switch (as-shipped):
```python
if os.environ.get("AGT_CSP_REQUIRE_APPROVAL", "false") == "true":
    approval_gate = telegram_approval_gate
else:
    approval_gate = identity
```

### 3.4 Phase 1 invariants (shipped)

- `NO_PENDING_APPROVAL_BEYOND_TIMEOUT` — any `csp_pending_approval.status='pending'` row with `timeout_at_utc + 60s < now` = high-severity incident.
- `NO_APPROVED_INDICES_OUT_OF_BOUNDS` — runtime drop + warning inside `telegram_approval_gate`.

---

## 4. Phase 2 — Composer Design

### 4.1 Design principles

1. **The composer is a pure transformation.** Inputs: `list[CSPCandidate]` + environmental context (news, market, mode). Outputs: structured ranked digest. No side effects other than (a) LLM API call through cached_client, (b) audit row to `decisions_repo`. No direct DB mutation, no Telegram send, no allocator mutation.
2. **Fail-open is structural, not optional.** Any composer exception — timeout, parse error, network — yields the Phase 1 bare digest with a `DEGRADED` banner. The gate never fails because composition failed; it only fails because the operator said no or the clock ran out.
3. **LLM output is untrusted.** Validated by Pydantic v2 before rendering. Drift in the model's JSON format triggers fallback, not render-garbage.
4. **Determinism hooks everywhere.** Every LLM call carries a `run_id` + deterministic prompt hash. Re-running the same candidate batch (same day, same tickers) yields the same cached response. Tests fixture against recorded responses.
5. **Cost ceiling is a budget, not a guess.** Daily call budget enforced by `cached_client`; overruns log an incident and fall back to static.

### 4.2 Module structure — `agt_equities/csp_digest_composer.py`

```python
"""agt_equities.csp_digest_composer — ADR-010 Phase 2.

Composes a ranked, reasoned digest of CSP candidates for Telegram
operator approval. Replaces csp_approval_gate._build_digest_text
when AGT_CSP_COMPOSER_ENABLED=true; falls back transparently on
any composer error.

Architecture:
    allocator -> telegram_approval_gate -> [composer?] -> digest text
                                              |
                                              +-- cached_client (LLM)
                                              +-- news_client (Finnhub)
                                              +-- decisions_repo (audit)

Fail-open contract:
    - LLM timeout (>30s)         -> static fallback + DEGRADED banner + incident
    - LLM parse error            -> static fallback + DEGRADED banner + incident
    - LLM budget exhausted       -> static fallback (no banner — expected) + incident
    - News API down              -> proceed WITHOUT news bullets (not fatal)
    - Pydantic validation error  -> static fallback + DEGRADED banner + incident
    - Any other exception        -> static fallback + DEGRADED banner + incident (catch-all)
"""
```

### 4.3 Public interface

```python
from agt_equities.csp_digest_composer import compose_digest, ComposedDigest

def compose_digest(
    candidates: list[CSPCandidate],
    *,
    run_id: str,
    household_id: str,
    db_path: str | None = None,
    client: "CachedAnthropicClient | None" = None,
    news_client: "NewsClient | None" = None,
    fallback_on_error: bool = True,
) -> ComposedDigest:
    """Compose a ranked digest. Deterministic given (candidates, date, run_id).

    Returns ComposedDigest with:
        - text: str (Telegram-ready HTML, same keyboard contract as Phase 1)
        - ranking: list[int] — indices into `candidates` in preferred display order
        - reasoning_per_index: dict[int, str] — one-line rationale per candidate
        - news_bullets_per_index: dict[int, list[str]] — up to 3 bullets each
        - degraded: bool — True if any fail-open branch fired
        - llm_metadata: LLMMetadata — prompt_hash, model, usage, cache_hit, run_id
    """
```

### 4.4 Internal flow (reference implementation)

```python
def compose_digest(candidates, *, run_id, household_id, db_path=None,
                   client=None, news_client=None, fallback_on_error=True):
    if not candidates:
        return _empty_digest(run_id)

    client = client or CachedAnthropicClient.from_env(db_path=db_path)
    news_client = news_client or get_default_news_client()

    try:
        # Step 1 — Enrich candidates with news (best-effort; news failures are non-fatal)
        news_by_ticker = _fetch_news_batch(
            news_client,
            tickers=[c.ticker for c in candidates],
            ttl_hours=1,
        )  # {ticker: list[NewsBullet]} — empty list on per-ticker failure

        # Step 2 — Build LLM prompt from candidates + news + static context
        system_prompt = _build_system_prompt(household_id=household_id)
        user_prompt = _build_user_prompt(candidates, news_by_ticker)

        # Step 3 — LLM call via cached_client (handles budget, caching, audit)
        response = client.messages_create(
            model="claude-sonnet-4-6",
            system=system_prompt,
            user=user_prompt,
            max_tokens=2048,
            cache_ttl_hours=24,
            run_id=run_id,
            caller_module="csp_digest_composer",
        )

        # Step 4 — Parse + validate response (Pydantic v2)
        parsed = _parse_llm_response(response.text)  # ComposerLLMOutput, raises on drift

        # Step 5 — Persist full audit to decisions_repo
        _persist_composer_decision(
            db_path=db_path,
            run_id=run_id,
            household_id=household_id,
            prompt_hash=response.prompt_hash,
            response_hash=response.response_hash,
            model=response.model,
            usage=response.usage,
            cache_hit=response.cache_hit,
            parsed_output=parsed,
        )

        # Step 6 — Render text + package result
        text = _render_digest_text(candidates, parsed, news_by_ticker, degraded=False)
        return ComposedDigest(
            text=text,
            ranking=parsed.ranking,
            reasoning_per_index={i: r for i, r in enumerate(parsed.reasoning)},
            news_bullets_per_index={
                i: news_by_ticker.get(c.ticker, [])[:3]
                for i, c in enumerate(candidates)
            },
            degraded=False,
            llm_metadata=LLMMetadata.from_response(response, run_id=run_id),
        )

    except (BudgetExceeded, Timeout, ParseError, ValidationError) as exc:
        logger.warning("csp_digest_composer: %s — falling back to static", exc)
        _persist_composer_fallback(db_path, run_id, household_id, reason=str(exc))
        _emit_incident(
            invariant_id="NO_COMPOSER_EXCEPTION_WITHOUT_FALLBACK",
            severity="medium",
            detail={"run_id": run_id, "reason": str(exc)},
            db_path=db_path,
        )
        if fallback_on_error:
            return _static_fallback_digest(candidates, run_id, household_id)
        raise
    except Exception as exc:
        logger.exception("csp_digest_composer: unexpected error — falling back")
        _persist_composer_fallback(db_path, run_id, household_id, reason=f"unexpected:{type(exc).__name__}")
        _emit_incident(
            invariant_id="NO_COMPOSER_EXCEPTION_WITHOUT_FALLBACK",
            severity="high",
            detail={"run_id": run_id, "exc_type": type(exc).__name__, "exc_str": str(exc)},
            db_path=db_path,
        )
        if fallback_on_error:
            return _static_fallback_digest(candidates, run_id, household_id)
        raise
```

### 4.5 Data contracts (Pydantic v2)

```python
from pydantic import BaseModel, Field, conint, confloat

class NewsBullet(BaseModel):
    headline: str = Field(max_length=200)
    source: str
    published_at_utc: str
    url: str | None = None

class ComposerLLMOutput(BaseModel):
    """Strict JSON schema the LLM must return. Drift = fallback."""
    ranking: list[conint(ge=0)] = Field(min_length=1, max_length=10)  # indices into candidates[]
    reasoning: list[str] = Field(min_length=1, max_length=10)
    risk_flags: list[str] = Field(default_factory=list, max_length=5)
    summary: str = Field(max_length=500)
    model_config = {"extra": "forbid"}  # unknown keys = validation error = fallback

class LLMMetadata(BaseModel):
    model: str
    prompt_hash: str  # sha256(system + user + model + max_tokens)[:16]
    response_hash: str
    input_tokens: int
    output_tokens: int
    cache_hit: bool
    run_id: str

class ComposedDigest(BaseModel):
    text: str
    ranking: list[int]
    reasoning_per_index: dict[int, str]
    news_bullets_per_index: dict[int, list[NewsBullet]]
    degraded: bool
    llm_metadata: LLMMetadata | None  # None only for static fallback
```

### 4.6 Prompt design — system

```
You are the CSP selection assistant for AGT Equities, a California
state-registered RIA running the Heitkoetter Wheel strategy. You
review a list of cash-secured put candidates the allocator has
already filtered through 7 hard gates (delta, IVR, earnings blackout,
correlation, dividend, VIX acceleration, ROLC).

Your job: RANK the candidates for the operator (Yash) to review
on his phone. Provide concise per-candidate reasoning (≤25 words),
surface risk flags that transcend the hard gates (macro,
sector-specific, event-driven), and summarize the batch in ≤80 words.

You do not decide what trades execute. The operator does.

Strict output contract — JSON matching this schema, no markdown,
no commentary outside the JSON:

{
  "ranking": [<indices in preferred display order>],
  "reasoning": [<one string per candidate, index-aligned>],
  "risk_flags": [<zero or more batch-level concerns>],
  "summary": "<≤80 word overview>"
}

Constraints:
- ranking must be a permutation of [0..len(candidates)-1]
- reasoning must have exactly len(candidates) entries
- Do NOT invent tickers or prices not in the input
- Do NOT output dollar amounts larger than $1,000,000 (operator
  sanity check — we do not size single positions at that scale)
- Do NOT mention specific account numbers or household names
- If a candidate looks problematic, say so in risk_flags — do
  not unilaterally drop it from the ranking (the operator decides)
```

### 4.7 Prompt design — user

```
Date: {date_utc_iso}
Household: {household_id}
Mode: {current_mode}  # PEACETIME | AMBER | WARTIME — should be PEACETIME if we're asking
Leverage: {current_leverage:.2f}x

Candidates (index-aligned):
{for i, c in enumerate(candidates):}
  [{i}] {c.ticker} ${c.strike:.2f}P {c.expiry}
      mid=${c.mid:.2f}  ann_yield={c.annualized_yield:.1%}
      delta={c.delta:.3f}  ivr={c.ivr:.1%}
      otm_pct={c.otm_pct:.1%}  dte={c.dte_days}

News (24h, from Finnhub):
{for ticker, bullets in news_by_ticker.items():}
  {ticker}:
    - {bullet.headline} ({bullet.source}, {bullet.published_at_utc})

Output the ranking JSON now.
```

Rendered with f-strings at build time. Total prompt size under 4k tokens for 5 candidates + news; well inside Sonnet context window.

### 4.8 News ingestion contract

`_fetch_news_batch(client, tickers, ttl_hours=1) -> dict[str, list[NewsBullet]]`:

- One Finnhub call per unique ticker (batched via thread pool, max_workers=5).
- Per-ticker failure → empty list for that ticker (logged, not raised).
- In-memory TTL cache keyed by `(ticker, date_utc)` — hit-rate should be ~100% within a single allocator run.
- All-tickers-failed → empty dict (composer proceeds without news; degraded=False because news is best-effort).

---

## 5. `cached_client` — Anthropic wrapper (Dispatch E unblock)

### 5.1 Public interface

```python
# agt_equities/cached_client.py

class CachedAnthropicClient:
    """Production wrapper for anthropic.Anthropic.

    Responsibilities:
    - Prompt caching via Anthropic beta header (cache_control on system prompt)
    - Response caching keyed by prompt_hash with configurable TTL
    - Daily budget enforcement (counter in llm_budget table, 24h TTL)
    - Audit logging (every call -> decisions_repo via caller's run_id)
    - Timeout enforcement (30s default; configurable)

    Not a general-purpose SDK wrapper. Shape is AGT-specific:
    system + user prompts only (no tools, no vision, no streaming).
    """

    def __init__(
        self,
        api_key: str,
        *,
        db_path: str | Path | None = None,
        daily_budget_calls: int = 50,
        daily_budget_input_tokens: int = 500_000,
        timeout_seconds: float = 30.0,
    ): ...

    @classmethod
    def from_env(cls, *, db_path=None) -> "CachedAnthropicClient":
        """Constructor using ANTHROPIC_API_KEY from .env."""

    def messages_create(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
        cache_ttl_hours: int = 24,
        run_id: str,
        caller_module: str,
    ) -> "LLMResponse":
        """Execute a cached Anthropic message call.

        Raises:
            BudgetExceeded: daily call or token cap hit (before API call)
            Timeout: request exceeded timeout_seconds
            anthropic.APIError: SDK-level failure
        """
```

### 5.2 `LLMResponse` dataclass

```python
@dataclass(frozen=True)
class LLMResponse:
    text: str                 # The single assistant turn, stripped
    model: str
    input_tokens: int
    output_tokens: int
    cache_hit: bool           # True if response came from response_cache
    cache_created: bool       # True if Anthropic prompt cache was written (first call)
    cache_read: bool          # True if Anthropic prompt cache was read (subsequent)
    prompt_hash: str          # sha256(system|user|model|max_tokens)[:16]
    response_hash: str        # sha256(text)[:16]
    request_duration_ms: int
    run_id: str
```

### 5.3 Caching architecture

**Two caches, different purposes:**

1. **Anthropic prompt cache (beta header).** System prompt is marked `cache_control={"type": "ephemeral"}` so Anthropic's infrastructure caches the prompt tokenization. ~5-min TTL, server-side; reduces cost on system-prompt re-use within a short window.
2. **AGT response cache (SQLite-backed).** Full response body cached in `llm_response_cache` table keyed by `prompt_hash`. TTL configurable per-call (default 24h). Read-through: if hit, return cached `LLMResponse` without API call.

```sql
CREATE TABLE llm_response_cache (
    prompt_hash TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    response_text TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_at_utc TEXT NOT NULL,
    expires_at_utc TEXT NOT NULL,
    response_hash TEXT NOT NULL
);
CREATE INDEX idx_llm_cache_expires ON llm_response_cache(expires_at_utc);
```

Cache eviction: lazy + scheduled. Invariant `NO_LLM_CACHE_BLOAT` enforces row count ≤10,000; overflow triggers eviction of expired rows in FIFO order.

### 5.4 Budget enforcement

```sql
CREATE TABLE llm_budget (
    date_utc TEXT PRIMARY KEY,         -- YYYY-MM-DD
    calls_count INTEGER NOT NULL DEFAULT 0,
    input_tokens_total INTEGER NOT NULL DEFAULT 0,
    output_tokens_total INTEGER NOT NULL DEFAULT 0,
    last_updated_utc TEXT NOT NULL
);
```

Pre-call check: `SELECT calls_count, input_tokens_total FROM llm_budget WHERE date_utc = ?` — reject if any limit would be breached by this call. Post-call UPSERT increments counters atomically.

Defaults:
- `daily_budget_calls = 50` (roughly 10 allocator runs × 5 retries)
- `daily_budget_input_tokens = 500_000` (comfortable headroom for 5-candidate batches with news + reasoning)

Overrides in `.env`:
```
AGT_LLM_DAILY_BUDGET_CALLS=50
AGT_LLM_DAILY_BUDGET_TOKENS=500000
```

### 5.5 Audit logging

Every `messages_create` call writes a row to `decisions_repo.llm_calls` table (new):

```sql
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    caller_module TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    response_hash TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_hit INTEGER NOT NULL,           -- response_cache hit
    anthropic_cache_created INTEGER NOT NULL,
    anthropic_cache_read INTEGER NOT NULL,
    request_duration_ms INTEGER,
    error_type TEXT,                      -- null if success
    error_msg TEXT,
    called_at_utc TEXT NOT NULL
);
CREATE INDEX idx_llm_calls_run_id ON llm_calls(run_id);
CREATE INDEX idx_llm_calls_called_at ON llm_calls(called_at_utc);
```

`decisions_repo.record_llm_call(...)` is the write helper; reads are via `get_llm_calls_for_run(run_id)` for retrospective audit.

### 5.6 Error types

```python
class CachedClientError(Exception): pass
class BudgetExceeded(CachedClientError): pass  # pre-call, no API hit
class Timeout(CachedClientError): pass          # request > timeout_seconds
class ParseError(CachedClientError): pass       # only raised if composer unwraps and re-raises from its Pydantic layer
```

---

## 6. Phase 2 Invariants

Per ADR-013 Layer 1 pattern (Pydantic at the boundary) and Layer 2 pattern (per-minute SQL incidents tick):

### 6.1 `NO_UNCACHED_LLM_CALL_IN_HOT_PATH` (Layer 3 — lint-style)

Tier-2 severity. `scripts/precommit_loc_gate.py` extension: any commit touching `agt_equities/**` that adds an import of `anthropic` outside `agt_equities/cached_client.py` fails the gate. Enforced structurally, not at runtime.

Sentinel test: `tests/test_no_raw_anthropic_imports.py` — AST-walks `agt_equities/` for `import anthropic` or `from anthropic import ...`; allowlist = `{cached_client.py}`.

### 6.2 `NO_COMPOSER_EXCEPTION_WITHOUT_FALLBACK` (Layer 2 — incidents tick)

Medium severity. Emitted by the composer itself on fail-open. Rate threshold: >5 incidents in 24h → escalate to high, block composer enablement flag for 1h (auto-disable — falls back to Phase 1 bare digest until a human re-enables).

### 6.3 `NO_DIGEST_LLM_BUDGET_OVERRUN` (Layer 2 — incidents tick)

Medium severity. Emitted by `cached_client.messages_create` pre-call check when budget would be exceeded. Triggers an alert to `alerts_queue` for Yash.

### 6.4 `NO_LLM_CACHE_BLOAT` (Layer 2 — per-minute SQL)

Low severity. `SELECT COUNT(*) FROM llm_response_cache > 10000` → trigger eviction job.

### 6.5 `NO_STALE_COMPOSER_AUDIT` (Layer 2 — per-minute SQL)

Medium severity. `llm_calls` rows with `error_type IS NULL AND response_hash IS NULL` indicate a composer call that started but never completed auditing — suggests a crash mid-call. >3 in 24h = escalate.

---

## 7. Phase 3 — Deferred

Scope explicitly out of Phase 2 (moved here for traceability):

- **Multi-day memory.** Rolling context window of recent approvals/rejections (last 30 days) injected into system prompt. Requires data maturity: need ≥2 weeks of Phase 2 production data before designing.
- **Ranking feedback loop.** Approved candidates reinforce prompt amendments (per ADR-012); rejected candidates penalize similar patterns. Requires Phase 2 + ADR-012 both stabilized.
- **Macro headline integration.** Beyond per-ticker news. Single daily market summary injected. Deferred until Phase 2 operational cost + token spend observed.
- **Multi-model routing.** Escalate from Sonnet to Opus for "hard" batches (high leverage, mixed sectors, elevated VIX). Deferred until ADR-015 (Tier Migration) locks — this is really a Level-4 concern.
- **Prompt versioning + A/B.** Record system prompt version in `llm_calls` table; A/B-test prompt variants across allocator runs. Deferred until we have a second prompt worth testing.

Re-scoping trigger: one of these becomes "in scope" only when 2+ weeks of Phase 2 production data + a concrete problem statement. No speculative shipping.

---

## 8. Migration plan

| MR | Title | LOC | Blast | Dependencies | Target session |
|---|---|---|---|---|---|
| MR-E.0 | `cached_client.py` + `llm_response_cache` + `llm_budget` + `llm_calls` tables | ~250 | Low (no caller yet; ships dark) | None (ADR-010 v2 locks contract) | Coder, single dispatch |
| MR-E.1 | `csp_digest_composer.py` + Pydantic models + tests (mock client) | ~350 | Low (still dark; composer not wired) | MR-E.0 | Coder, single dispatch |
| MR-E.2 | News enrichment integration (`_fetch_news_batch`) + TTL cache + tests | ~150 | Low | MR-E.1 | Coder, single dispatch |
| MR-E.3 | Wire composer into `csp_approval_gate` under `AGT_CSP_COMPOSER_ENABLED=false` (default off) | ~80 | Medium (touches Phase 1 path, but flag off) | MR-E.2 | Coder, single dispatch |
| MR-E.4 | Invariants §6.1–§6.5 + tests | ~200 | Low | MR-E.3 | Coder, single dispatch |
| MR-E.5 | Paper dry-run: enable flag on `U22076329` paper account for 1 week, monitor | ~0 (config) | Zero (paper) | MR-E.4 | Operator, manual |
| MR-E.6 | Live enablement: flip `AGT_CSP_COMPOSER_ENABLED=true` in live env | ~0 (config) | High | 2+ weeks of MR-E.5 clean data | Operator + Architect review |

Total Phase 2: 5 code MRs + 2 operational steps. Estimated 4–6 weeks from MR-E.0 start to MR-E.6 live flip.

**Sequencing note:** MR-E.0 ships `cached_client` alone. This is the Dispatch E target. Re-authored Dispatch E = MR-E.0.

---

## 9. Pre-resolved design decisions

All locked by this ADR; only the stated "revisit trigger" forces re-escalation.

- **Q1 — LLM model: LOCKED = `claude-sonnet-4-6`.** Opus is overkill for ranking 5 candidates; cost/perf ratio puts Sonnet at 90% capability for 20% cost. **Revisit only if:** MR-E.5 paper week shows Sonnet hallucinating tickers, fabricating prices, or ranking quality is below the static fallback's "alphabetical by ticker" baseline.
- **Q2 — Response cache TTL: LOCKED = 24h default.** CSP allocator runs once per day (9:35 AM scan). Same-day re-run = cache hit (free). **Revisit only if:** ops adds a midday allocator run.
- **Q3 — Budget defaults: LOCKED = 50 calls/day, 500k input tokens/day.** ~$5/day ceiling at current Sonnet pricing. **Revisit only if:** we onboard a second LLM caller (learning loop, synthetic eval) — re-baseline across all callers at that time.
- **Q4 — JSON output vs tool use: LOCKED = JSON in text response + Pydantic parse.** Tool-use adds dependency surface (tool schema version, Anthropic SDK tool-use mode). JSON is simpler, lower-dependency, easier to mock in tests. **Revisit only if:** parse error rate exceeds 5% in MR-E.5 paper week.
- **Q5 — News source: LOCKED = Finnhub.** Already integrated, rate limit adequate. **Revisit only if:** Finnhub free-tier rate limit (60 calls/min) becomes operationally constraining (>5 rate-limit errors/day).
- **Q6 — Prompt caching enablement: LOCKED = on (Anthropic beta header on system prompt).** ~90% cost saving on cached calls, minimal downside. **Revisit only if:** Anthropic deprecates the beta or changes semantics materially.
- **Q7 — Multi-household batching: LOCKED = per-household composer invocation.** One `compose_digest` call per household (Yash_Household, Vikram_Household, etc.). Matches the mode-per-household invariant (Act 60 vs advisory). **Revisit:** none anticipated.

---

## 10. Test harness

### 10.1 Deterministic mock client

`tests/fixtures/mock_anthropic.py`:

```python
class MockCachedAnthropicClient:
    """Tests-only replacement for CachedAnthropicClient.

    Responses are keyed by prompt_hash and pulled from a recorded
    JSON fixture file (tests/fixtures/llm_responses.json).
    """
    def __init__(self, fixture_path: Path): ...
    def messages_create(self, **kwargs) -> LLMResponse: ...
```

Fixture file seeded from 10 canonical scenarios (empty, 1-candidate, 3-candidate, 5-candidate, WARTIME, degraded-news, parse-drift, budget-exhausted, timeout, schema-extra-fields).

### 10.2 Scenario bank coverage

Minimum test suite (+sprint_a marker):

- `test_composer_empty_candidates` — returns empty digest, no LLM call
- `test_composer_happy_path` — 3 candidates, mock returns valid JSON, digest text matches snapshot
- `test_composer_parse_error_falls_back` — mock returns malformed JSON, digest is Phase 1 static + DEGRADED
- `test_composer_pydantic_validation_error_falls_back` — mock returns valid JSON with extra key, falls back
- `test_composer_budget_exceeded_falls_back` — mock client raises `BudgetExceeded`, returns static
- `test_composer_timeout_falls_back` — mock raises `Timeout`, returns static + incident emitted
- `test_composer_news_failure_proceeds_without_news` — news client raises, digest has empty news bullets, degraded=False
- `test_composer_persists_audit_row` — asserts `llm_calls` row written per call
- `test_composer_ranking_is_permutation` — validates output ranking covers all candidate indices exactly once
- `test_composer_degraded_banner_in_fallback_text` — DEGRADED banner appears iff degraded=True
- `test_cached_client_prompt_hash_deterministic` — same (system, user, model) = same prompt_hash
- `test_cached_client_response_cache_hit` — second identical call returns cached response, no API hit
- `test_cached_client_budget_precheck` — call at budget-1 succeeds, call at budget fails with BudgetExceeded

Target: +14 tests in MR-E.1 through MR-E.4.

---

## 11. Open questions (owed to next ADR iteration)

- **Does the composer need to know current leverage?** §4.7 user prompt includes it, but the allocator's mode-gate already blocks entries under AMBER/WARTIME. Including leverage in the prompt is informational for Sonnet's reasoning only. Keep for now; revisit in MR-E.5 if it doesn't affect rankings.
- **Should we version the system prompt?** Phase 3 item. Record `prompt_version` in `llm_calls` when we have a v2 prompt worth testing. Pre-emptively added a `prompt_version TEXT` column in `llm_calls`? — defer to MR-E.1 recon; if trivial, add; if intrusive, skip.
- **Do we need per-household mode-aware prompting?** E.g., Act 60 principal account has different constraints than advisory client. Phase 2 doesn't differentiate; all calls use the same system prompt. Deferred to ADR-015 tier migration design.

---

## 12. Success criteria (Phase 2 exit)

1. `AGT_CSP_COMPOSER_ENABLED=true` on paper `U22076329` for 7 consecutive CSP scan days without incident escalation.
2. Composer call success rate (non-fallback) ≥ 95% over the 7-day paper window.
3. Response cache hit rate ≥ 40% (same-day re-runs during dev).
4. Zero incidents of classes §6.1, §6.2 (critical), §6.5.
5. CI: +14 sprint_a tests minimum.
6. Daily LLM spend ≤ $5 during paper week.
7. Yash subjective review: digest reasoning is "actionable" ≥80% of the time. Captured via `/feedback_digest` Telegram command writing to `decisions_repo.operator_feedback`.

Passing all 7 → MR-E.6 live flip eligibility.

---

## 13. Notes

- This ADR is the first production LLM integration in AGT. It sets the precedent for ADR-014 (Synthetic Data Eval), ADR-012 (Learning Loop), and any future LLM callers. The `cached_client` contract is generic; the composer is CSP-specific.
- Dispatch E (cached_client wrapper) is re-authored as MR-E.0 against this ADR's §5 contract.
- Cost note: ~$0.03 per composer call at Sonnet pricing with prompt caching. $5/day budget = ~150 calls/day capacity. Well above expected 5–10 calls/day operational load. Budget is defensive, not operational.
- Per ADR-013 Layer 1: Pydantic `extra="forbid"` in `ComposerLLMOutput` is load-bearing. Any LLM output drift (added field, renamed field) triggers fallback immediately — not silent wrong behavior.

**End of ADR-010 v2.**
