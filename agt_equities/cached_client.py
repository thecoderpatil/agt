"""ADR-010 §5 — CachedAnthropicClient.

Sole production entry point for Anthropic SDK calls in AGT. Wraps
anthropic.Anthropic with:
  - Prompt caching (Anthropic beta header, cache_control on system)
  - Response caching (SQLite, keyed by prompt_hash, configurable TTL)
  - Daily budget enforcement (calls + input tokens, UTC day)
  - Audit logging (llm_calls row per call, including errors)
  - Timeout enforcement (default 30s)

Invariant NO_UNCACHED_LLM_CALL_IN_HOT_PATH (ADR-010 §6.1) is enforced
structurally by tests/test_no_raw_anthropic_imports.py — any import
of `anthropic` outside this module fails CI.

Not a general-purpose SDK wrapper. Shape is AGT-specific: system +
user prompts only. No tools, no vision, no streaming.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agt_equities.db import get_db_connection  # DB_PATH removed in Sprint 5 MR B (E-M-4) — was unused

if TYPE_CHECKING:
    import anthropic  # pragma: no cover — type only

log = logging.getLogger(__name__)


# --- Error hierarchy ---------------------------------------------------------


class CachedClientError(Exception):
    """Base for all cached_client errors."""


class BudgetExceeded(CachedClientError):
    """Pre-call budget check failed; no API call was made."""


class Timeout(CachedClientError):
    """Request exceeded timeout_seconds."""


class ParseError(CachedClientError):
    """Response body could not be parsed. Raised by callers that
    validate the response; cached_client itself does not raise this
    on successful API return."""


# --- Response dataclass ------------------------------------------------------


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_hit: bool           # AGT response_cache hit (no API call)
    cache_created: bool       # Anthropic prompt cache was written
    cache_read: bool          # Anthropic prompt cache was read
    prompt_hash: str          # sha256(system|user|model|max_tokens)[:16]
    response_hash: str        # sha256(text)[:16]
    request_duration_ms: int
    run_id: str


# --- Schema migration --------------------------------------------------------


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_response_cache (
    prompt_hash TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    response_text TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_at_utc TEXT NOT NULL,
    expires_at_utc TEXT NOT NULL,
    response_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_cache_expires
    ON llm_response_cache(expires_at_utc);

CREATE TABLE IF NOT EXISTS llm_budget (
    date_utc TEXT PRIMARY KEY,
    calls_count INTEGER NOT NULL DEFAULT 0,
    input_tokens_total INTEGER NOT NULL DEFAULT 0,
    output_tokens_total INTEGER NOT NULL DEFAULT 0,
    last_updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    caller_module TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    response_hash TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_hit INTEGER NOT NULL,
    anthropic_cache_created INTEGER NOT NULL,
    anthropic_cache_read INTEGER NOT NULL,
    request_duration_ms INTEGER,
    error_type TEXT,
    error_msg TEXT,
    called_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run_id ON llm_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_called_at ON llm_calls(called_at_utc);
"""


def _ensure_schema(db_path: str | Path | None) -> None:
    with closing(get_db_connection(db_path=db_path)) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()


# --- Helpers -----------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _today_utc_date() -> str:
    return _utcnow().strftime("%Y-%m-%d")


def _compute_prompt_hash(*, system: str, user: str, model: str, max_tokens: int) -> str:
    canonical = f"{model}|{max_tokens}|{system}|{user}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _compute_response_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --- Client ------------------------------------------------------------------


class CachedAnthropicClient:
    """See module docstring. Thread-safety: one instance per thread
    (SDK client + sqlite connection per-call). Intentional — AGT's
    LLM call rate is single-digit per day; no need for pooling.
    """

    def __init__(
        self,
        api_key: str,
        *,
        db_path: str | Path | None = None,
        daily_budget_calls: int = 50,
        daily_budget_input_tokens: int = 500_000,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be non-empty")
        self._api_key = api_key
        self._db_path = db_path
        self._daily_budget_calls = int(daily_budget_calls)
        self._daily_budget_input_tokens = int(daily_budget_input_tokens)
        self._timeout_seconds = float(timeout_seconds)
        _ensure_schema(db_path)
        # Lazy SDK import so test_no_raw_anthropic_imports.py sees only
        # this module as an importer of `anthropic`.
        import anthropic
        self._sdk = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds)

    @classmethod
    def from_env(cls, *, db_path: str | Path | None = None) -> "CachedAnthropicClient":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in environment")
        calls = int(os.environ.get("AGT_LLM_DAILY_BUDGET_CALLS", "50"))
        tokens = int(os.environ.get("AGT_LLM_DAILY_BUDGET_TOKENS", "500000"))
        return cls(
            api_key=api_key,
            db_path=db_path,
            daily_budget_calls=calls,
            daily_budget_input_tokens=tokens,
        )

    # --- Public API -------------------------------------------------

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
    ) -> LLMResponse:
        if not run_id:
            raise ValueError("run_id must be non-empty")
        if not caller_module:
            raise ValueError("caller_module must be non-empty")

        prompt_hash = _compute_prompt_hash(
            system=system, user=user, model=model, max_tokens=max_tokens,
        )

        # --- 1. Response cache read-through
        cached = self._read_response_cache(prompt_hash=prompt_hash, model=model)
        if cached is not None:
            response = LLMResponse(
                text=cached["response_text"],
                model=cached["model"],
                input_tokens=int(cached["input_tokens"]),
                output_tokens=int(cached["output_tokens"]),
                cache_hit=True,
                cache_created=False,
                cache_read=False,
                prompt_hash=prompt_hash,
                response_hash=cached["response_hash"],
                request_duration_ms=0,
                run_id=run_id,
            )
            self._record_call(
                run_id=run_id,
                caller_module=caller_module,
                model=model,
                prompt_hash=prompt_hash,
                response=response,
                error_type=None,
                error_msg=None,
            )
            return response

        # --- 2. Budget pre-check (only on cache miss)
        self._budget_precheck(estimated_input_tokens=self._estimate_tokens(system, user))

        # --- 3. SDK call
        t0 = time.monotonic()
        try:
            sdk_response = self._sdk.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user}],
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
        except Exception as exc:
            error_type = type(exc).__name__
            is_timeout = "timeout" in error_type.lower() or "timeout" in str(exc).lower()
            self._record_call(
                run_id=run_id,
                caller_module=caller_module,
                model=model,
                prompt_hash=prompt_hash,
                response=None,
                error_type=error_type,
                error_msg=str(exc)[:500],
            )
            if is_timeout:
                raise Timeout(f"request exceeded {self._timeout_seconds}s: {exc}") from exc
            raise

        request_duration_ms = int((time.monotonic() - t0) * 1000)

        text = _extract_assistant_text(sdk_response)
        usage = getattr(sdk_response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_created_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)

        response_hash = _compute_response_hash(text)
        response = LLMResponse(
            text=text,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit=False,
            cache_created=cache_created_tokens > 0,
            cache_read=cache_read_tokens > 0,
            prompt_hash=prompt_hash,
            response_hash=response_hash,
            request_duration_ms=request_duration_ms,
            run_id=run_id,
        )

        # --- 4. Persist (cache + budget + audit)
        self._write_response_cache(
            prompt_hash=prompt_hash,
            response=response,
            cache_ttl_hours=cache_ttl_hours,
        )
        self._budget_increment(
            input_tokens=input_tokens + cache_read_tokens + cache_created_tokens,
            output_tokens=output_tokens,
        )
        self._record_call(
            run_id=run_id,
            caller_module=caller_module,
            model=model,
            prompt_hash=prompt_hash,
            response=response,
            error_type=None,
            error_msg=None,
        )
        return response

    # --- Internals --------------------------------------------------

    @staticmethod
    def _estimate_tokens(system: str, user: str) -> int:
        return (len(system) + len(user) + 3) // 4

    def _read_response_cache(self, *, prompt_hash: str, model: str) -> dict[str, Any] | None:
        with closing(get_db_connection(db_path=self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT model, response_text, input_tokens, output_tokens,
                       expires_at_utc, response_hash
                FROM llm_response_cache
                WHERE prompt_hash = ? AND model = ?
                """,
                (prompt_hash, model),
            ).fetchone()
        if row is None:
            return None
        if row["expires_at_utc"] <= _utcnow_iso():
            return None
        return dict(row)

    def _write_response_cache(
        self, *, prompt_hash: str, response: LLMResponse, cache_ttl_hours: int,
    ) -> None:
        expires = (_utcnow() + timedelta(hours=cache_ttl_hours)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with closing(get_db_connection(db_path=self._db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO llm_response_cache (
                    prompt_hash, model, response_text, input_tokens,
                    output_tokens, cached_at_utc, expires_at_utc, response_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prompt_hash, response.model, response.text,
                    response.input_tokens, response.output_tokens,
                    _utcnow_iso(), expires, response.response_hash,
                ),
            )
            conn.commit()

    def _budget_precheck(self, *, estimated_input_tokens: int) -> None:
        date_utc = _today_utc_date()
        with closing(get_db_connection(db_path=self._db_path)) as conn:
            row = conn.execute(
                """
                SELECT calls_count, input_tokens_total
                FROM llm_budget WHERE date_utc = ?
                """,
                (date_utc,),
            ).fetchone()
        calls = row[0] if row else 0
        tokens = row[1] if row else 0
        if calls + 1 > self._daily_budget_calls:
            raise BudgetExceeded(
                f"daily call budget exceeded: {calls}+1 > {self._daily_budget_calls}"
            )
        if tokens + estimated_input_tokens > self._daily_budget_input_tokens:
            raise BudgetExceeded(
                f"daily input-token budget exceeded: "
                f"{tokens}+{estimated_input_tokens} > {self._daily_budget_input_tokens}"
            )

    def _budget_increment(self, *, input_tokens: int, output_tokens: int) -> None:
        date_utc = _today_utc_date()
        with closing(get_db_connection(db_path=self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO llm_budget (
                    date_utc, calls_count, input_tokens_total,
                    output_tokens_total, last_updated_utc
                ) VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(date_utc) DO UPDATE SET
                    calls_count = calls_count + 1,
                    input_tokens_total = input_tokens_total + excluded.input_tokens_total,
                    output_tokens_total = output_tokens_total + excluded.output_tokens_total,
                    last_updated_utc = excluded.last_updated_utc
                """,
                (date_utc, input_tokens, output_tokens, _utcnow_iso()),
            )
            conn.commit()

    def _record_call(
        self,
        *,
        run_id: str,
        caller_module: str,
        model: str,
        prompt_hash: str,
        response: LLMResponse | None,
        error_type: str | None,
        error_msg: str | None,
    ) -> None:
        with closing(get_db_connection(db_path=self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO llm_calls (
                    run_id, caller_module, model, prompt_hash, response_hash,
                    input_tokens, output_tokens, cache_hit,
                    anthropic_cache_created, anthropic_cache_read,
                    request_duration_ms, error_type, error_msg, called_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, caller_module, model, prompt_hash,
                    response.response_hash if response else None,
                    response.input_tokens if response else None,
                    response.output_tokens if response else None,
                    1 if (response and response.cache_hit) else 0,
                    1 if (response and response.cache_created) else 0,
                    1 if (response and response.cache_read) else 0,
                    response.request_duration_ms if response else None,
                    error_type, error_msg, _utcnow_iso(),
                ),
            )
            conn.commit()


def _extract_assistant_text(sdk_response: Any) -> str:
    """Pull the single text block from an Anthropic messages response.

    Raises ParseError if the response shape is unrecognized (e.g.,
    SDK major-version bump, tool_use block where we expect text).
    """
    content = getattr(sdk_response, "content", None)
    if not content:
        raise ParseError("sdk_response.content is empty or missing")
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", None)
            if text is None:
                raise ParseError("text block missing .text attribute")
            return str(text).strip()
    raise ParseError(f"no text block in response content (types: {[getattr(b,'type',None) for b in content]})")


__all__ = [
    "CachedAnthropicClient",
    "LLMResponse",
    "CachedClientError",
    "BudgetExceeded",
    "Timeout",
    "ParseError",
]
