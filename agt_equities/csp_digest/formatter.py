"""CSP Digest formatter — markdown card body + Telegram inline keyboard.

Per ADR-CSP_TELEGRAM_DIGEST_v1 "Card format — locked 2026-04-22".

Pure functions. No I/O. No telegram-bot SDK import — keyboard returned
as nested list-of-list of dicts the caller passes through to PTB.
"""
from __future__ import annotations

from typing import Iterable

from agt_equities.csp_digest.types import DigestCandidate, DigestCommentary, DigestPayload

# Pinning order rule: red flags first, then blue, then normal (by rank asc).
_FLAG_PRIORITY = {"red": 0, "blue": 1, None: 2}


def render_digest_header(payload: DigestPayload) -> str:
    """Top-of-message digest header.

    Format (per ADR):
        🎯 CSP Digest — Tue 2026-04-28 09:35 ET
        Mode: LIVE • 5 candidates • Timeout: 11:05 ET
        Accounts: Yash Ind • Yash Roth • Vikram Ind
    """
    sent_local = payload.sent_at_utc.strftime("%a %Y-%m-%d %H:%M UTC")
    timeout_local = payload.timeout_at_utc.strftime("%H:%M UTC")
    return (
        f"🎯 CSP Digest — {sent_local}\n"
        f"Mode: {payload.mode} • {len(payload.candidates)} candidates • "
        f"Timeout: {timeout_local}\n"
        f"Accounts: {payload.accounts_blurb}"
    )


def _color_emoji(value: float | None, benchmark: float | None) -> str:
    """🟢 if value > benchmark, 🔴 if value < benchmark, ⚪ if no benchmark."""
    if benchmark is None or value is None:
        return "⚪"
    if value > benchmark:
        return "🟢"
    if value < benchmark:
        return "🔴"
    return "⚪"


def _account_blurb(per_account: list[tuple[str, int]]) -> str:
    """`(2* Vikram Ind, 5* Yash Roth)` style. Alphabetical by account name."""
    if not per_account:
        return ""
    sorted_pairs = sorted(per_account, key=lambda p: p[0])
    parts = [f"{n}* {label}" for label, n in sorted_pairs]
    return "(" + ", ".join(parts) + ")"


def _normal_card_body(c: DigestCandidate, comm: DigestCommentary | None) -> str:
    """Card body without the per-card prefix (which the dispatcher adds)."""
    accounts = _account_blurb(c.per_account)
    line1 = (
        f"CSP #{c.rank} — {c.ticker} ${c.strike:g}P {c.expiry}  {accounts}".rstrip()
    )
    line2 = (
        f"Premium ${c.premium_dollars:.2f} ({c.premium_pct:.1f}%) • "
        f"RAY {c.ray_pct:.0f}% • δ {c.delta:.2f} • OTM {c.otm_pct:.1f}%"
    )
    line3 = (
        f"IVR {c.ivr_pct:.0f}% {_color_emoji(c.ivr_pct, c.ivr_benchmark_median)} • "
        f"VRP {c.vrp:.2f} {_color_emoji(c.vrp, c.vrp_benchmark_median)} • "
        f"Fwd P/E {c.fwd_pe:.1f}" if c.fwd_pe is not None else
        f"IVR {c.ivr_pct:.0f}% {_color_emoji(c.ivr_pct, c.ivr_benchmark_median)} • "
        f"VRP {c.vrp:.2f} {_color_emoji(c.vrp, c.vrp_benchmark_median)} • Fwd P/E n/a"
    )
    line4 = (
        f"52w: ${c.week52_low:.2f} — ${c.week52_high:.2f} (spot ${c.spot:.2f}, "
        f"{c.week52_pct_of_range:.0f}% of range)"
    )
    line5 = (
        f"Analyst avg: {c.analyst_avg:.1f}/5 ({c.analyst_sources_blurb or 'n/a'})"
        if c.analyst_avg is not None else
        "Analyst avg: n/a"
    )

    body_lines = [line1, line2, line3, line4, line5, ""]
    if comm is not None:
        ns = comm.news_summary.strip() if comm.news_summary else ""
        mc = comm.macro_context.strip() if comm.macro_context else ""
        if ns or mc:
            body_lines.append(f"📰 {ns}")
            if mc:
                body_lines.append(f"Macro: {mc}")
        if comm.rank_rationale:
            body_lines.append("")
            body_lines.append(f"Rank #{c.rank} rationale: {comm.rank_rationale}")
        if comm.concern_flag in ("red", "blue") and comm.concern_reason:
            emoji = "🚫" if comm.concern_flag == "red" else "🔵"
            label = "CONCERN" if comm.concern_flag == "red" else "LOOK DEEPER"
            body_lines.append("")
            body_lines.append(f"{emoji} {label}: {comm.concern_reason}")
    else:
        body_lines.append("📰 [news commentary unavailable]")
    return "\n".join(body_lines).rstrip()


def render_card_text(payload: DigestPayload) -> str:
    """Render the full digest as a single markdown text block.

    Concern-flagged candidates pin to top (red before blue). Order
    within a flag tier follows screener rank ascending. Normal cards
    follow original screener rank order.
    """
    pieces: list[str] = [render_digest_header(payload)]

    # Sort: by flag priority (red > blue > none), then by rank asc.
    def _key(cand: DigestCandidate) -> tuple[int, int]:
        comm = payload.commentaries.get(cand.ticker)
        flag = comm.concern_flag if comm else None
        return (_FLAG_PRIORITY[flag], cand.rank)

    ordered = sorted(payload.candidates, key=_key)
    for cand in ordered:
        comm = payload.commentaries.get(cand.ticker)
        body = _normal_card_body(cand, comm)
        if comm and comm.concern_flag == "red":
            pieces.append("")
            pieces.append("🚫 " + body)
        elif comm and comm.concern_flag == "blue":
            pieces.append("")
            pieces.append("🔵 " + body)
        else:
            pieces.append("")
            pieces.append(body)

    if payload.rank_disagreement and isinstance(payload.rank_disagreement, dict):
        order = payload.rank_disagreement.get("llm_order")
        reason = payload.rank_disagreement.get("reason")
        if order and reason:
            pieces.append("")
            pieces.append("🤔 Rank disagreement:")
            pieces.append(f"   LLM would rank: {' > '.join(order)}")
            pieces.append(f"   Reason: {reason}")
    return "\n".join(pieces)


def build_inline_keyboard(payload: DigestPayload, *, run_id: str) -> list[list[dict]]:
    """Per-ticker [✅ Approve] [❌ Reject] rows + one ALL row at the end.

    Returns a nested list-of-list of dicts compatible with
    python-telegram-bot's InlineKeyboardButton.from_dict.

    Paper mode returns an empty list — paper auto-executes; the digest
    card displays `📄 Paper mode — executed automatically` instead.
    """
    if payload.mode != "LIVE":
        return []
    rows: list[list[dict]] = []
    for cand in payload.candidates:
        rows.append([
            {
                "text": f"✅ Approve {cand.ticker}",
                "callback_data": f"csp_approve:{run_id}:{cand.ticker}",
            },
            {
                "text": f"❌ Reject {cand.ticker}",
                "callback_data": f"csp_reject:{run_id}:{cand.ticker}",
            },
        ])
    rows.append([
        {"text": "✅ Approve ALL",
         "callback_data": f"csp_approve_all:{run_id}"},
        {"text": "❌ Reject ALL",
         "callback_data": f"csp_reject_all:{run_id}"},
    ])
    return rows
