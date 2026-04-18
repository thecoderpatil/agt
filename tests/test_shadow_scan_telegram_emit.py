"""Tests for MR 6a: shadow_scan --emit telegram digest formatter + send path.

sprint_a: no IB connection, no DB writes, no real Telegram sends.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_order(engine="csp", ticker="AAPL", right="P", strike=150.0,
                qty=1, limit=1.50, decided_at="2026-04-18T12:00:00+00:00",
                meta=None):
    from agt_equities.sinks import ShadowOrder
    return ShadowOrder(
        engine=engine,
        run_id="testrun",
        ticker=ticker,
        right=right,
        strike=strike,
        qty=qty,
        limit=limit,
        decided_at=decided_at,
        meta=meta if meta is not None else {},
    )


def _make_ctx(run_id="testrun"):
    ctx = MagicMock()
    ctx.run_id = run_id
    ctx.mode.value = "shadow"
    ctx.db_path = ":memory:"
    return ctx


# ---------------------------------------------------------------------------
# 1. Groups by engine
# ---------------------------------------------------------------------------

def test_render_digest_groups_by_engine():
    from scripts.shadow_scan import _render_telegram_digest
    orders = [
        _make_order(engine="csp", ticker="AAPL"),
        _make_order(engine="cc", ticker="TSLA"),
    ]
    ctx = _make_ctx()
    messages = _render_telegram_digest(orders, [], ctx)
    assert len(messages) == 2
    csp_msg = next(m for m in messages if "csp" in m)
    cc_msg = next(m for m in messages if "cc" in m)
    assert "AAPL" in csp_msg
    assert "TSLA" in cc_msg


# ---------------------------------------------------------------------------
# 2. Sorts tickers within engine
# ---------------------------------------------------------------------------

def test_render_digest_sorts_tickers_within_engine():
    from scripts.shadow_scan import _render_telegram_digest
    orders = [
        _make_order(ticker="TSLA"),
        _make_order(ticker="AAPL"),
        _make_order(ticker="MSFT"),
    ]
    ctx = _make_ctx()
    messages = _render_telegram_digest(orders, [], ctx)
    assert len(messages) >= 1
    msg = messages[0]
    aapl_pos = msg.index("AAPL")
    msft_pos = msg.index("MSFT")
    tsla_pos = msg.index("TSLA")
    assert aapl_pos < msft_pos < tsla_pos


# ---------------------------------------------------------------------------
# 3. Missing household renders [?]
# ---------------------------------------------------------------------------

def test_render_digest_missing_household_renders_placeholder():
    from scripts.shadow_scan import _render_telegram_digest
    orders = [_make_order(meta={})]
    ctx = _make_ctx()
    messages = _render_telegram_digest(orders, [], ctx)
    assert "[?]" in messages[0]


# ---------------------------------------------------------------------------
# 4. Empty snapshot produces single stub message
# ---------------------------------------------------------------------------

def test_render_digest_empty_snapshot_produces_single_stub_message():
    from scripts.shadow_scan import _render_telegram_digest
    ctx = _make_ctx(run_id="emptyrun")
    messages = _render_telegram_digest([], [], ctx)
    assert len(messages) == 1
    assert "emptyrun" in messages[0]
    assert "no engines" in messages[0].lower()


# ---------------------------------------------------------------------------
# 5. Splits at bullet boundary over 4096 chars
# ---------------------------------------------------------------------------

def test_render_digest_splits_at_bullet_boundary_over_4096_chars():
    from scripts.shadow_scan import _render_telegram_digest
    orders = [
        _make_order(ticker=f"T{i:03d}", meta={"household": "HH1"})
        for i in range(100)
    ]
    ctx = _make_ctx()
    messages = _render_telegram_digest(orders, [], ctx)
    assert len(messages) > 1
    for msg in messages:
        assert len(msg) <= 4096


# ---------------------------------------------------------------------------
# 6. Limit decimal format preserved
# ---------------------------------------------------------------------------

def test_render_digest_preserves_limit_decimal_format():
    from scripts.shadow_scan import _render_telegram_digest
    orders = [_make_order(limit=123.45, meta={"household": "HH1"})]
    ctx = _make_ctx()
    messages = _render_telegram_digest(orders, [], ctx)
    assert "$123.45" in messages[0]
    assert "$123.4500" not in messages[0]


# ---------------------------------------------------------------------------
# 7. HTML-escape special chars in ticker and household
# ---------------------------------------------------------------------------

def test_render_digest_html_safe_for_special_chars():
    from scripts.shadow_scan import _render_telegram_digest
    orders = [_make_order(ticker="A&B", meta={"household": "HH<1>"})]
    ctx = _make_ctx()
    messages = _render_telegram_digest(orders, [], ctx)
    msg = messages[0]
    assert "A&B" not in msg
    assert "HH<1>" not in msg
    assert "&amp;" in msg


# ---------------------------------------------------------------------------
# 8. Telegram failure does not suppress JSON artifact
# ---------------------------------------------------------------------------

def test_emit_telegram_falls_through_to_json_on_send_failure(tmp_path):
    from scripts.shadow_scan import main
    with patch(
        "agt_equities.telegram_utils.requests.post",
        side_effect=OSError("network down"),
    ):
        with patch("scripts.shadow_scan.clone_sqlite_db_with_wal", return_value=":memory:"):
            with patch("scripts.shadow_scan.REPORTS_DIR", tmp_path):
                main(["--emit", "telegram", "--engine", "csp"])
    artifacts = list(tmp_path.glob("shadow_scan_*.json"))
    assert len(artifacts) == 1
    data = json.loads(artifacts[0].read_text())
    assert "run_id" in data


# ---------------------------------------------------------------------------
# 9. Happy path: send called + JSON written
# ---------------------------------------------------------------------------

def test_emit_telegram_happy_path_writes_both(tmp_path):
    from scripts.shadow_scan import main
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    with patch("agt_equities.telegram_utils.requests.post", return_value=mock_resp):
        with patch("scripts.shadow_scan.clone_sqlite_db_with_wal", return_value=":memory:"):
            with patch("scripts.shadow_scan.REPORTS_DIR", tmp_path):
                main(["--emit", "telegram", "--engine", "csp"])
    artifacts = list(tmp_path.glob("shadow_scan_*.json"))
    assert len(artifacts) == 1


# ---------------------------------------------------------------------------
# 10. Rate-limit sleep between messages
# ---------------------------------------------------------------------------

def test_send_telegram_digest_rate_limits_between_messages():
    from agt_equities.telegram_utils import send_telegram_digest
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    with patch("agt_equities.telegram_utils.requests.post", return_value=mock_resp):
        with patch("agt_equities.telegram_utils.time.sleep") as mock_sleep:
            send_telegram_digest(
                ["msg1", "msg2", "msg3"],
                bot_token="fake_token",
                chat_id="12345",
            )
    assert mock_sleep.call_count == 2
    for c in mock_sleep.call_args_list:
        assert c == call(0.3)
