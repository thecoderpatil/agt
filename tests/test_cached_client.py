"""ADR-010 §10.2 minimum test suite for CachedAnthropicClient.

All tests use a synthetic sqlite DB (tmp_path) and monkeypatch the
Anthropic SDK to a MagicMock — no network, no API key required.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from agt_equities.cached_client import (
    BudgetExceeded,
    CachedAnthropicClient,
    LLMResponse,
    _compute_prompt_hash,
    _ensure_schema,
)

pytestmark = [pytest.mark.sprint_a, pytest.mark.agt_tripwire_exempt]


# --- Fake SDK fixture --------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


class _FakeResponse:
    def __init__(self, text: str = "hello world", usage: _FakeUsage | None = None):
        self.content = [_FakeTextBlock(text=text)]
        self.usage = usage or _FakeUsage()


def _make_client(tmp_path, monkeypatch, *, budget_calls=50, budget_tokens=500_000):
    db_path = tmp_path / "agt_test.db"
    _ensure_schema(db_path)
    fake_sdk = MagicMock()
    fake_sdk.messages.create.return_value = _FakeResponse()
    with patch("anthropic.Anthropic", return_value=fake_sdk):
        client = CachedAnthropicClient(
            api_key="test-key",
            db_path=db_path,
            daily_budget_calls=budget_calls,
            daily_budget_input_tokens=budget_tokens,
        )
    return client, fake_sdk, db_path


# --- Tests -------------------------------------------------------------------


def test_cached_client_prompt_hash_deterministic():
    """Same (system, user, model, max_tokens) → same prompt_hash."""
    h1 = _compute_prompt_hash(system="sys", user="user", model="claude-sonnet-4-6", max_tokens=1024)
    h2 = _compute_prompt_hash(system="sys", user="user", model="claude-sonnet-4-6", max_tokens=1024)
    h3 = _compute_prompt_hash(system="sys", user="user", model="claude-sonnet-4-6", max_tokens=2048)
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16


def test_cached_client_response_cache_hit(tmp_path, monkeypatch):
    """Second identical call returns cached response without hitting SDK."""
    client, fake_sdk, db_path = _make_client(tmp_path, monkeypatch)
    r1 = client.messages_create(
        model="claude-sonnet-4-6",
        system="sys",
        user="user",
        max_tokens=1024,
        run_id="run-1",
        caller_module="test",
    )
    r2 = client.messages_create(
        model="claude-sonnet-4-6",
        system="sys",
        user="user",
        max_tokens=1024,
        run_id="run-2",
        caller_module="test",
    )
    assert r1.cache_hit is False
    assert r2.cache_hit is True
    assert r2.text == r1.text
    assert fake_sdk.messages.create.call_count == 1


def test_cached_client_budget_precheck_calls(tmp_path, monkeypatch):
    """Exceeding daily calls budget raises BudgetExceeded before SDK call."""
    client, fake_sdk, _ = _make_client(tmp_path, monkeypatch, budget_calls=1)
    client.messages_create(
        model="claude-sonnet-4-6", system="s1", user="u1",
        run_id="r1", caller_module="test",
    )
    with pytest.raises(BudgetExceeded, match="call budget"):
        client.messages_create(
            model="claude-sonnet-4-6", system="s2", user="u2",
            run_id="r2", caller_module="test",
        )
    assert fake_sdk.messages.create.call_count == 1


def test_cached_client_budget_precheck_tokens(tmp_path, monkeypatch):
    """Exceeding daily input-token budget raises BudgetExceeded."""
    client, fake_sdk, _ = _make_client(
        tmp_path, monkeypatch, budget_tokens=10,
    )
    with pytest.raises(BudgetExceeded, match="token budget"):
        client.messages_create(
            model="claude-sonnet-4-6", system="s" * 1000, user="u" * 1000,
            run_id="r1", caller_module="test",
        )
    assert fake_sdk.messages.create.call_count == 0


def test_cached_client_writes_audit_row(tmp_path, monkeypatch):
    """Every messages_create call writes exactly one llm_calls row."""
    client, _, db_path = _make_client(tmp_path, monkeypatch)
    client.messages_create(
        model="claude-sonnet-4-6", system="sys", user="user",
        run_id="run-audit", caller_module="test_audit",
    )
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT run_id, caller_module, error_type, cache_hit "
            "FROM llm_calls WHERE run_id = ?", ("run-audit",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("run-audit", "test_audit", None, 0)


def test_cached_client_audit_row_on_error(tmp_path, monkeypatch):
    """SDK error path also writes an audit row with error_type set."""
    client, fake_sdk, db_path = _make_client(tmp_path, monkeypatch)
    fake_sdk.messages.create.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        client.messages_create(
            model="claude-sonnet-4-6", system="sys", user="user",
            run_id="run-err", caller_module="test_err",
        )
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT error_type, error_msg FROM llm_calls WHERE run_id = ?",
            ("run-err",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "RuntimeError"
    assert "boom" in rows[0][1]


def test_cached_client_response_cache_respects_ttl(tmp_path, monkeypatch):
    """Expired cache row is ignored; SDK is re-hit."""
    client, fake_sdk, db_path = _make_client(tmp_path, monkeypatch)
    client.messages_create(
        model="claude-sonnet-4-6", system="sys", user="user",
        run_id="r1", caller_module="test",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE llm_response_cache SET expires_at_utc = '2000-01-01T00:00:00.000000Z'"
        )
        conn.commit()
    client.messages_create(
        model="claude-sonnet-4-6", system="sys", user="user",
        run_id="r2", caller_module="test",
    )
    assert fake_sdk.messages.create.call_count == 2
