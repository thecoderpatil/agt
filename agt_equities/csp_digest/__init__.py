"""CSP Digest package — formatter + LLM commentary + approval gate + cost ledger.

Per ADR-CSP_TELEGRAM_DIGEST_v1. This package is the LIBRARY layer for
the live-capital CSP approval digest. Scheduler hook + telegram bot
wiring + csp_allocator AllocatorResult persistence are intentionally
deferred to a follow-on MR after observation week.

Public surface:
    DigestCandidate           — frozen dataclass per candidate
    DigestPayload             — bundle: candidates + commentary + meta
    render_card_text          — markdown card body for Telegram
    build_inline_keyboard     — per-ticker approve/reject button rows
    generate_commentary       — Anthropic tool-use call with 8 safeguards
    record_llm_call           — write to llm_cost_ledger
    daily_cost_usd            — read trailing 24h cost from ledger
    identity_approval_gate    — paper-mode default (approve everything)
    fail_closed_timeout_gate  — live default after observation week
"""
from __future__ import annotations

from agt_equities.csp_digest.approval_gate import (
    fail_closed_timeout_gate,
    identity_approval_gate,
)
from agt_equities.csp_digest.cost_ledger import daily_cost_usd, record_llm_call
from agt_equities.csp_digest.formatter import (
    build_inline_keyboard,
    render_card_text,
    render_digest_header,
)
from agt_equities.csp_digest.llm_commentary import (
    COMMENTARY_TOOL,
    DEFAULT_MODEL,
    LLMCommentaryError,
    generate_commentary,
)
from agt_equities.csp_digest.types import DigestCandidate, DigestCommentary, DigestPayload

__all__ = [
    "COMMENTARY_TOOL",
    "DEFAULT_MODEL",
    "DigestCandidate",
    "DigestCommentary",
    "DigestPayload",
    "LLMCommentaryError",
    "build_inline_keyboard",
    "daily_cost_usd",
    "fail_closed_timeout_gate",
    "generate_commentary",
    "identity_approval_gate",
    "record_llm_call",
    "render_card_text",
    "render_digest_header",
]
