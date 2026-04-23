"""Tests for csp_digest formatter — golden card output + flag pinning."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agt_equities.csp_digest.formatter import (
    build_inline_keyboard,
    render_card_text,
    render_digest_header,
)
from agt_equities.csp_digest.types import (
    DigestCandidate,
    DigestCommentary,
    DigestPayload,
)

pytestmark = pytest.mark.sprint_a


def _utc(year=2026, month=4, day=28, hour=13, minute=35):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _candidate(rank, ticker, **kwargs):
    base = dict(
        rank=rank,
        ticker=ticker,
        strike=115.0,
        expiry="May 2",
        premium_dollars=2.45,
        premium_pct=2.1,
        ray_pct=78.0,
        delta=0.22,
        otm_pct=6.4,
        ivr_pct=42.0,
        vrp=1.38,
        fwd_pe=18.4,
        week52_low=98.20,
        week52_high=142.50,
        spot=122.85,
        week52_pct_of_range=43.0,
        analyst_avg=4.1,
        analyst_sources_blurb="IBKR 4, TV Strong Buy",
        per_account=[("Vikram Ind", 2), ("Yash Roth", 5)],
        ivr_benchmark_median=30.0,
        vrp_benchmark_median=1.0,
    )
    base.update(kwargs)
    return DigestCandidate(**base)


def _payload(candidates, commentaries=None, mode="LIVE", rank_disagreement=None):
    return DigestPayload(
        mode=mode,
        sent_at_utc=_utc(),
        timeout_at_utc=_utc(hour=15, minute=5),
        candidates=candidates,
        commentaries=commentaries or {},
        accounts_blurb="Yash Ind • Yash Roth • Vikram Ind",
        rank_disagreement=rank_disagreement,
    )


# ---------- header ----------


def test_render_digest_header_includes_mode_count_timeout():
    p = _payload([_candidate(1, "DELL")])
    h = render_digest_header(p)
    assert "🎯 CSP Digest" in h
    assert "Mode: LIVE" in h
    assert "1 candidates" in h
    assert "Timeout:" in h
    assert "Yash Ind • Yash Roth • Vikram Ind" in h


# ---------- card body — IVR/VRP color ----------


def test_card_renders_green_when_ivr_above_benchmark():
    c = _candidate(1, "DELL", ivr_pct=50.0, ivr_benchmark_median=30.0)
    p = _payload([c])
    out = render_card_text(p)
    assert "IVR 50% 🟢" in out


def test_card_renders_red_when_vrp_below_benchmark():
    c = _candidate(1, "DELL", vrp=0.5, vrp_benchmark_median=1.0)
    p = _payload([c])
    out = render_card_text(p)
    assert "VRP 0.50 🔴" in out


def test_card_renders_white_when_no_benchmark():
    c = _candidate(1, "DELL", ivr_benchmark_median=None, vrp_benchmark_median=None)
    p = _payload([c])
    out = render_card_text(p)
    assert "IVR 42% ⚪" in out
    assert "VRP 1.38 ⚪" in out


# ---------- multi-account blurb ----------


def test_card_renders_multi_account_alphabetically():
    c = _candidate(1, "DELL", per_account=[("Yash Roth", 5), ("Vikram Ind", 2)])
    out = render_card_text(_payload([c]))
    # Vikram alphabetically first
    assert "(2* Vikram Ind, 5* Yash Roth)" in out


def test_card_renders_single_account_blurb():
    c = _candidate(1, "OXY", per_account=[("Yash Ind", 3)])
    out = render_card_text(_payload([c]))
    assert "(3* Yash Ind)" in out


def test_card_renders_no_account_blurb_when_empty():
    c = _candidate(1, "OXY", per_account=[])
    out = render_card_text(_payload([c]))
    # No parens
    assert "()" not in out


# ---------- 52w + analyst lines ----------


def test_card_renders_analyst_n_a_when_none():
    c = _candidate(1, "DELL", analyst_avg=None, analyst_sources_blurb=None)
    out = render_card_text(_payload([c]))
    assert "Analyst avg: n/a" in out


def test_card_renders_fwd_pe_n_a_when_none():
    c = _candidate(1, "DELL", fwd_pe=None)
    out = render_card_text(_payload([c]))
    assert "Fwd P/E n/a" in out


# ---------- commentary ----------


def test_card_renders_news_commentary_unavailable_when_no_commentary():
    out = render_card_text(_payload([_candidate(1, "DELL")]))
    assert "📰 [news commentary unavailable]" in out


def test_card_renders_news_summary_and_macro():
    comm = DigestCommentary(
        ticker="DELL",
        news_summary="AI-server momentum analyst upgrade",
        macro_context="tech green pre-mkt",
        rank_rationale="highest RAY clean news",
    )
    out = render_card_text(_payload([_candidate(1, "DELL")], {"DELL": comm}))
    assert "📰 AI-server momentum analyst upgrade" in out
    assert "Macro: tech green pre-mkt" in out
    assert "Rank #1 rationale: highest RAY clean news" in out


# ---------- concern flag pinning ----------


def test_red_concern_pins_to_top():
    c1 = _candidate(1, "DELL")
    c2 = _candidate(2, "OXY")
    c3 = _candidate(3, "MRNA")
    comm_oxy = DigestCommentary(
        ticker="OXY", news_summary="ok", macro_context="ok",
        rank_rationale="ok", concern_flag="red",
        concern_reason="8-K item 2.04 triggering event",
    )
    out = render_card_text(_payload(
        [c1, c2, c3],
        {"OXY": comm_oxy},
    ))
    assert "🚫 CSP #2 — OXY" in out
    # OXY (red flag) should appear before DELL (no flag) in the rendered order
    oxy_pos = out.index("CSP #2")
    dell_pos = out.index("CSP #1")
    assert oxy_pos < dell_pos


def test_blue_concern_pins_below_red_above_normal():
    c1 = _candidate(1, "DELL")
    c2 = _candidate(2, "OXY")
    c3 = _candidate(3, "MRNA")
    out = render_card_text(_payload(
        [c1, c2, c3],
        {
            "OXY": DigestCommentary(
                ticker="OXY", news_summary="x", macro_context="x",
                rank_rationale="x", concern_flag="red", concern_reason="r",
            ),
            "MRNA": DigestCommentary(
                ticker="MRNA", news_summary="x", macro_context="x",
                rank_rationale="x", concern_flag="blue", concern_reason="b",
            ),
        },
    ))
    oxy_pos = out.index("CSP #2")
    mrna_pos = out.index("CSP #3")
    dell_pos = out.index("CSP #1")
    assert oxy_pos < mrna_pos < dell_pos


def test_concern_reason_rendered_in_card():
    comm = DigestCommentary(
        ticker="OXY", news_summary="x", macro_context="x", rank_rationale="x",
        concern_flag="red", concern_reason="8-K item 2.04 triggering event",
    )
    out = render_card_text(_payload([_candidate(1, "OXY")], {"OXY": comm}))
    assert "🚫 CONCERN: 8-K item 2.04 triggering event" in out


def test_blue_concern_renders_look_deeper_label():
    comm = DigestCommentary(
        ticker="MRNA", news_summary="x", macro_context="x", rank_rationale="x",
        concern_flag="blue", concern_reason="FDA advcom upcoming",
    )
    out = render_card_text(_payload([_candidate(1, "MRNA")], {"MRNA": comm}))
    assert "🔵 LOOK DEEPER: FDA advcom upcoming" in out


# ---------- rank disagreement ----------


def test_rank_disagreement_appended_at_bottom():
    out = render_card_text(_payload(
        [_candidate(1, "DELL"), _candidate(2, "OXY")],
        rank_disagreement={
            "llm_order": ["OXY", "DELL"],
            "reason": "OXY had a 5.02 filing in past 24h",
        },
    ))
    assert "🤔 Rank disagreement:" in out
    assert "LLM would rank: OXY > DELL" in out
    assert "OXY had a 5.02 filing" in out


def test_rank_disagreement_not_rendered_when_none():
    out = render_card_text(_payload([_candidate(1, "DELL")]))
    assert "Rank disagreement" not in out


# ---------- inline keyboard ----------


def test_keyboard_live_mode_returns_per_ticker_rows_plus_all_row():
    p = _payload([_candidate(1, "DELL"), _candidate(2, "OXY")])
    kb = build_inline_keyboard(p, run_id="run-001")
    assert len(kb) == 3  # 2 ticker rows + ALL row
    assert kb[0][0]["text"] == "✅ Approve DELL"
    assert kb[0][1]["text"] == "❌ Reject DELL"
    assert kb[0][0]["callback_data"] == "csp_approve:run-001:DELL"
    assert kb[2][0]["callback_data"] == "csp_approve_all:run-001"


def test_keyboard_paper_mode_returns_empty():
    p = _payload([_candidate(1, "DELL")], mode="PAPER")
    kb = build_inline_keyboard(p, run_id="x")
    assert kb == []
