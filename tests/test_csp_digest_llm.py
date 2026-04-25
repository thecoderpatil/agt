"""Tests for csp_digest.llm_commentary — Anthropic mock + 8 burn safeguards."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agt_equities.csp_digest.cost_ledger import (
    daily_cost_usd,
    record_llm_call,
)
from agt_equities.csp_digest.llm_commentary import (
    COMMENTARY_TOOL,
    DEFAULT_DAILY_BUDGET_USD,
    DEFAULT_MODEL,
    SYSTEM_PROMPT,
    _compute_cost_usd,
    _estimate_tokens,
    _model_pricing,
    _truncate_user_payload,
    generate_commentary,
)
from agt_equities.csp_digest.types import DigestCommentary

pytestmark = pytest.mark.sprint_a


# ---------- safeguard #1: max_tokens=500 default ----------


def test_default_max_tokens_is_500():
    from agt_equities.csp_digest.llm_commentary import DEFAULT_MAX_TOKENS
    assert DEFAULT_MAX_TOKENS == 500


def test_default_model_is_sonnet_4_6():
    assert DEFAULT_MODEL == "claude-sonnet-4-6"


def test_default_daily_budget_is_5_usd():
    assert DEFAULT_DAILY_BUDGET_USD == 5.0


# ---------- safeguard #2: tool-use schema locked ----------


def test_commentary_tool_has_required_per_ticker():
    schema = COMMENTARY_TOOL["input_schema"]
    assert schema["required"] == ["per_ticker"]


def test_commentary_tool_per_ticker_required_fields():
    item = COMMENTARY_TOOL["input_schema"]["properties"]["per_ticker"]["items"]
    assert set(item["required"]) == {
        "ticker", "news_summary", "rank_rationale", "concern_flag",
    }


def test_commentary_tool_concern_flag_nullable():
    item = COMMENTARY_TOOL["input_schema"]["properties"]["per_ticker"]["items"]
    assert "null" in item["properties"]["concern_flag"]["type"]


# ---------- safeguard #3: input envelope cap ----------


def test_truncate_user_payload_under_cap_unchanged():
    short = "hello world"
    assert _truncate_user_payload(short, cap_tokens=100) == short


def test_truncate_user_payload_over_cap_trimmed():
    long = "x" * 20000
    out = _truncate_user_payload(long, cap_tokens=100)
    assert len(out) < len(long)
    assert "[... truncated for envelope cap ...]" in out


def test_estimate_tokens_uses_4_char_heuristic():
    assert _estimate_tokens("a" * 400) == 100


# ---------- safeguard #6: prompt cache breakpoint ----------


def test_system_prompt_explicit_locked():
    assert "disciplined analyst" in SYSTEM_PROMPT
    assert "do NOT vote" in SYSTEM_PROMPT


# ---------- safeguard #7: $5/day tripwire ----------


@pytest.fixture
def db_path(tmp_path):
    import sys
    from pathlib import Path
    _SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import migrate_llm_cost_ledger
    p = tmp_path / "agt_desk.db"
    migrate_llm_cost_ledger.migrate(str(p))
    return p


def test_record_llm_call_writes_row(db_path):
    now = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)
    record_llm_call(
        db_path,
        timestamp_utc=now,
        run_id="r1", call_site="csp_digest", model="claude-sonnet-4-6",
        input_tokens=2000, cached_input_tokens=0, output_tokens=300,
        cost_usd=0.0105, status="ok",
    )
    cost = daily_cost_usd(db_path, now_utc=now)
    assert cost == pytest.approx(0.0105, rel=1e-6)


def test_daily_cost_filtered_by_call_site(db_path):
    now = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)
    record_llm_call(db_path, timestamp_utc=now, run_id="r1",
                    call_site="csp_digest", model="claude-sonnet-4-6",
                    input_tokens=0, cached_input_tokens=0, output_tokens=0,
                    cost_usd=1.0, status="ok")
    record_llm_call(db_path, timestamp_utc=now, run_id="r2",
                    call_site="weekly_review", model="claude-opus-4-6",
                    input_tokens=0, cached_input_tokens=0, output_tokens=0,
                    cost_usd=2.0, status="ok")
    assert daily_cost_usd(db_path, call_site="csp_digest", now_utc=now) == pytest.approx(1.0)
    assert daily_cost_usd(db_path, call_site="weekly_review", now_utc=now) == pytest.approx(2.0)
    assert daily_cost_usd(db_path, now_utc=now) == pytest.approx(3.0)


def test_daily_cost_excludes_rows_older_than_24h(db_path):
    now = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=25)
    record_llm_call(db_path, timestamp_utc=old, run_id="r_old",
                    call_site="csp_digest", model="claude-sonnet-4-6",
                    input_tokens=0, cached_input_tokens=0, output_tokens=0,
                    cost_usd=10.0, status="ok")
    assert daily_cost_usd(db_path, now_utc=now) == 0.0


def test_generate_commentary_aborts_on_budget_exceeded(db_path):
    now = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)
    record_llm_call(db_path, timestamp_utc=now, run_id="prior",
                    call_site="csp_digest", model="claude-sonnet-4-6",
                    input_tokens=0, cached_input_tokens=0, output_tokens=0,
                    cost_usd=5.5, status="ok")
    factory_called = {"n": 0}

    def factory():
        factory_called["n"] += 1
        return SimpleNamespace()

    out = asyncio.run(generate_commentary(
        "user payload", run_id="r1", db_path=db_path,
        anthropic_factory=factory,
        now_utc=now,
    ))
    assert out == {}
    assert factory_called["n"] == 0  # never called Anthropic


# ---------- safeguard #4 + #5: timeout + retry ----------


def test_generate_commentary_swallows_exception_returns_empty(db_path):
    def factory():
        client = MagicMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
        client.aclose = AsyncMock()
        return client

    out = asyncio.run(generate_commentary(
        "x", run_id="r1", db_path=db_path,
        anthropic_factory=factory,
    ))
    assert out == {}
    assert daily_cost_usd(db_path) == 0.0  # error rows have cost_usd=0


def test_generate_commentary_swallows_timeout_returns_empty(db_path):
    def factory():
        client = MagicMock()
        client.messages.create = AsyncMock(side_effect=TimeoutError("timeout"))
        client.aclose = AsyncMock()
        return client

    out = asyncio.run(generate_commentary(
        "x", run_id="r1", db_path=db_path,
        anthropic_factory=factory,
    ))
    assert out == {}


def test_generate_commentary_retries_once_on_failure(db_path):
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        client = MagicMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("x"))
        client.aclose = AsyncMock()
        return client

    asyncio.run(generate_commentary(
        "x", run_id="r1", db_path=db_path, retry_budget=1,
        anthropic_factory=factory,
    ))
    assert calls["n"] == 2  # initial + 1 retry


# ---------- safeguard #8: deterministic fallback returns empty ----------


def test_generate_commentary_no_anthropic_returns_empty(db_path):
    # Force the late import to fail by passing a factory that raises ImportError
    def factory():
        raise ImportError("anthropic not installed in this test env")

    out = asyncio.run(generate_commentary(
        "x", run_id="r1", db_path=db_path,
        anthropic_factory=factory,
    ))
    assert out == {}


# ---------- happy path tool-use parse ----------


def test_generate_commentary_parses_tool_use_block(db_path):
    fake_tool_use = SimpleNamespace(
        type="tool_use",
        input={
            "per_ticker": [
                {
                    "ticker": "DELL",
                    "news_summary": "AI-server upgrade",
                    "macro_context": "tech green",
                    "rank_rationale": "highest RAY",
                    "concern_flag": None,
                },
                {
                    "ticker": "OXY",
                    "news_summary": "8-K item 2.04",
                    "macro_context": "energy weak",
                    "rank_rationale": "rank suspect",
                    "concern_flag": "red",
                    "concern_reason": "Triggering event under credit agreement",
                },
            ],
        },
    )
    fake_msg = SimpleNamespace(
        content=[fake_tool_use],
        usage=SimpleNamespace(
            input_tokens=2000,
            cache_read_input_tokens=500,
            output_tokens=300,
        ),
    )

    def factory():
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=fake_msg)
        client.aclose = AsyncMock()
        return client

    out = asyncio.run(generate_commentary(
        "user", run_id="r1", db_path=db_path,
        anthropic_factory=factory,
    ))
    assert "DELL" in out and "OXY" in out
    assert isinstance(out["OXY"], DigestCommentary)
    assert out["OXY"].concern_flag == "red"
    assert out["OXY"].concern_reason == "Triggering event under credit agreement"
    assert out["DELL"].concern_flag is None
    # cost ledger row should exist
    cost = daily_cost_usd(db_path)
    assert cost > 0


def test_generate_commentary_normalizes_unknown_concern_flag(db_path):
    fake_tool_use = SimpleNamespace(
        type="tool_use",
        input={"per_ticker": [{
            "ticker": "DELL",
            "news_summary": "x", "macro_context": "x", "rank_rationale": "x",
            "concern_flag": "ORANGE",  # invalid
        }]},
    )
    fake_msg = SimpleNamespace(content=[fake_tool_use], usage=None)

    def factory():
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=fake_msg)
        client.aclose = AsyncMock()
        return client

    out = asyncio.run(generate_commentary(
        "x", run_id="r1", db_path=db_path,
        anthropic_factory=factory,
    ))
    assert out["DELL"].concern_flag is None  # normalized to None


# ---------- pricing ----------


def test_pricing_table_has_default_models():
    in_p, cache_p, out_p = _model_pricing("claude-sonnet-4-6")
    assert (in_p, cache_p, out_p) == (3.0, 0.30, 15.0)


def test_pricing_unknown_model_defaults_to_sonnet():
    assert _model_pricing("unknown-model") == _model_pricing("claude-sonnet-4-6")


def test_compute_cost_with_cached_input_savings():
    cost = _compute_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=2000, cached_input_tokens=1000, output_tokens=500,
    )
    # fresh 1000 tokens * $3/M + cached 1000 * $0.30/M + 500 out * $15/M
    expected = (1000 * 3.0 + 1000 * 0.30 + 500 * 15.0) / 1_000_000
    assert cost == pytest.approx(expected, rel=1e-9)
