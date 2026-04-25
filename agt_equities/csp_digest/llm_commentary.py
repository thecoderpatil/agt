"""LLM commentary call — Anthropic tool-use with 8 burn safeguards.

Per ADR-CSP_TELEGRAM_DIGEST_v1 §"LLM role — commentary ONLY" and
§"API burn safeguards — eight layers".

The LLM does NOT vote, re-rank, or gate execution. It produces
structured per-ticker commentary the formatter renders.

Eight safeguards enforced here:
  1. max_tokens=500
  2. tool-use structured output (no free-form JSON parsing)
  3. input envelope cap = 3,000 tokens (estimate, then truncate)
  4. 30-second request timeout (hard)
  5. retry budget = 1
  6. prompt caching via cache_control on system + tool block
  7. $5/day tripwire (read trailing 24h llm_cost_ledger)
  8. deterministic fallback returns empty commentary on any failure

Safeguard #7's tripwire reads `llm_cost_ledger` via cost_ledger module.

Caller passes `db_path` for ledger I/O. None disables ledger entirely
(test surface only).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agt_equities.csp_digest.cost_ledger import daily_cost_usd, record_llm_call
from agt_equities.csp_digest.types import DigestCommentary

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 500
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_RETRY_BUDGET = 1
DEFAULT_INPUT_TOKEN_CAP = 3000
DEFAULT_DAILY_BUDGET_USD = 5.0
DEFAULT_CALL_SITE = "csp_digest"

SYSTEM_PROMPT = (
    "You are a disciplined analyst for AGT Equities RIA. Your role is to "
    "annotate a CSP candidate list with news context and concerns. "
    "You do NOT vote, re-rank, or recommend execution. You annotate only."
)


COMMENTARY_TOOL = {
    "name": "record_csp_commentary",
    "description": (
        "Record annotations for each CSP candidate. Call exactly once "
        "with the full per_ticker list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "per_ticker": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "news_summary": {"type": "string", "maxLength": 400},
                        "macro_context": {"type": "string", "maxLength": 200},
                        "rank_rationale": {"type": "string", "maxLength": 200},
                        "concern_flag": {"type": ["string", "null"]},
                        "concern_reason": {"type": ["string", "null"], "maxLength": 300},
                    },
                    "required": ["ticker", "news_summary", "rank_rationale", "concern_flag"],
                },
            },
            "rank_disagreement": {
                "type": ["object", "null"],
                "properties": {
                    "llm_order": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string", "maxLength": 300},
                },
            },
        },
        "required": ["per_ticker"],
    },
}


class LLMCommentaryError(Exception):
    """Raised internally when the LLM call cannot complete; caller
    should fallthrough to the deterministic empty-commentary digest."""


def _estimate_tokens(text: str) -> int:
    # Rough heuristic: ~4 chars per token for English text.
    return max(1, len(text) // 4)


def _model_pricing(model: str) -> tuple[float, float, float]:
    """Return (input_per_mtok, cached_input_per_mtok, output_per_mtok) USD.

    Pricing as of 2026 — Sonnet 4.6 / Opus 4.6 / Haiku 4.5 rates.
    """
    table = {
        "claude-sonnet-4-6": (3.0, 0.30, 15.0),
        "claude-opus-4-6":   (15.0, 1.50, 75.0),
        "claude-haiku-4-5-20251001": (0.80, 0.08, 4.0),
    }
    return table.get(model, (3.0, 0.30, 15.0))  # default to Sonnet pricing


def _compute_cost_usd(
    model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int,
) -> float:
    in_p, cache_p, out_p = _model_pricing(model)
    fresh_input = max(0, input_tokens - cached_input_tokens)
    return (
        fresh_input * in_p / 1_000_000
        + cached_input_tokens * cache_p / 1_000_000
        + output_tokens * out_p / 1_000_000
    )


def _truncate_user_payload(user_text: str, cap_tokens: int) -> str:
    """Trim user_text to roughly cap_tokens. Cuts from the end."""
    if _estimate_tokens(user_text) <= cap_tokens:
        return user_text
    keep_chars = cap_tokens * 4
    return user_text[:keep_chars] + "\n[... truncated for envelope cap ...]"


async def generate_commentary(
    user_payload_text: str,
    *,
    run_id: str,
    db_path: str | Path | None,
    model: str = DEFAULT_MODEL,
    daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    retry_budget: int = DEFAULT_RETRY_BUDGET,
    input_token_cap: int = DEFAULT_INPUT_TOKEN_CAP,
    call_site: str = DEFAULT_CALL_SITE,
    anthropic_factory=None,
    now_utc: datetime | None = None,
) -> dict[str, DigestCommentary]:
    """Call Anthropic with all 8 safeguards. Return commentary keyed by ticker.

    Returns empty dict on any failure (deterministic fallback). Never
    raises. Always records the attempt in llm_cost_ledger if db_path
    is provided.

    `anthropic_factory` is an optional zero-arg async callable that
    returns an Anthropic-like client instance. Defaults to lazy
    `anthropic.AsyncAnthropic()` — late import keeps the package
    optional in test environments.
    """
    if db_path is not None:
        spent = daily_cost_usd(db_path, call_site=call_site, now_utc=now_utc)
        if spent >= daily_budget_usd:
            logger.warning(
                "csp_digest.llm_budget_exceeded spent=%.4f budget=%.2f",
                spent, daily_budget_usd,
            )
            record_llm_call(
                db_path,
                timestamp_utc=now_utc or datetime.now(timezone.utc),
                run_id=run_id, call_site=call_site, model=model,
                input_tokens=0, cached_input_tokens=0, output_tokens=0,
                cost_usd=0.0, status="budget_exceeded",
            )
            return {}

    user_text = _truncate_user_payload(user_payload_text, input_token_cap)

    if anthropic_factory is None:
        # ADR-010 §6.1: this module MUST NOT import `anthropic` directly.
        # Production callers pass an Anthropic client factory built from
        # agt_equities.cached_client (or a tool-use extension of it).
        # See ADR-CSP_TELEGRAM_DIGEST_v1 §"LLM role" — the actual SDK
        # binding is wired at the orchestrator MR (deferred from this
        # library MR), not here.
        logger.warning(
            "csp_digest.no_anthropic_factory: returning empty commentary"
        )
        return {}

    last_exc: Exception | None = None
    for attempt in range(retry_budget + 1):
        try:
            client = anthropic_factory()
            try:
                msg = await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    timeout=timeout_s,
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=[COMMENTARY_TOOL],
                    tool_choice={"type": "tool", "name": "record_csp_commentary"},
                    messages=[{"role": "user", "content": user_text}],
                )
            finally:
                close = getattr(client, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        pass
            result = _parse_tool_use(msg)
            input_tok, cached_input_tok, output_tok = _read_usage(msg)
            cost = _compute_cost_usd(model, input_tok, cached_input_tok, output_tok)
            if db_path is not None:
                record_llm_call(
                    db_path,
                    timestamp_utc=now_utc or datetime.now(timezone.utc),
                    run_id=run_id, call_site=call_site, model=model,
                    input_tokens=input_tok, cached_input_tokens=cached_input_tok,
                    output_tokens=output_tok, cost_usd=cost, status="ok",
                )
            return result
        except TimeoutError as exc:
            last_exc = exc
            logger.warning(
                "csp_digest.llm_timeout attempt=%d/%d err=%s",
                attempt + 1, retry_budget + 1, exc,
            )
            status: str = "timeout"
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "csp_digest.llm_err attempt=%d/%d err=%s",
                attempt + 1, retry_budget + 1, exc,
            )
            status = "error"

    if db_path is not None:
        record_llm_call(
            db_path,
            timestamp_utc=now_utc or datetime.now(timezone.utc),
            run_id=run_id, call_site=call_site, model=model,
            input_tokens=0, cached_input_tokens=0, output_tokens=0,
            cost_usd=0.0, status=status,
            error_class=type(last_exc).__name__ if last_exc else None,
        )
    return {}


def _parse_tool_use(msg) -> dict[str, DigestCommentary]:
    """Pull the tool_use block out of an Anthropic Messages response."""
    content = getattr(msg, "content", None) or []
    for block in content:
        block_type = getattr(block, "type", None) if hasattr(block, "type") else block.get("type") if isinstance(block, dict) else None
        if block_type != "tool_use":
            continue
        block_input = getattr(block, "input", None) if hasattr(block, "input") else block.get("input") if isinstance(block, dict) else None
        if not isinstance(block_input, dict):
            continue
        per_ticker = block_input.get("per_ticker") or []
        out: dict[str, DigestCommentary] = {}
        for entry in per_ticker:
            if not isinstance(entry, dict):
                continue
            ticker = entry.get("ticker")
            if not ticker:
                continue
            flag = entry.get("concern_flag")
            if flag not in (None, "red", "blue"):
                flag = None
            out[ticker.upper()] = DigestCommentary(
                ticker=ticker.upper(),
                news_summary=str(entry.get("news_summary") or ""),
                macro_context=str(entry.get("macro_context") or ""),
                rank_rationale=str(entry.get("rank_rationale") or ""),
                concern_flag=flag,
                concern_reason=entry.get("concern_reason") if flag else None,
            )
        return out
    return {}


def _read_usage(msg) -> tuple[int, int, int]:
    """Pull (input_tokens, cached_input_tokens, output_tokens) from response."""
    usage = getattr(msg, "usage", None)
    if usage is None and isinstance(msg, dict):
        usage = msg.get("usage")
    if usage is None:
        return (0, 0, 0)

    def _get(name: str) -> int:
        v = getattr(usage, name, None) if not isinstance(usage, dict) else usage.get(name)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    input_tokens = _get("input_tokens")
    cached_input_tokens = _get("cache_read_input_tokens")
    output_tokens = _get("output_tokens")
    return (input_tokens, cached_input_tokens, output_tokens)
