"""CSP Digest types — DigestCandidate, DigestCommentary, DigestPayload.

All frozen. The orchestrator (deferred MR) builds these from
AllocatorResult + NewsBundle dict; formatter consumes; LLM emits
DigestCommentary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

ConcernFlag = Literal["red", "blue"]


@dataclass(frozen=True)
class DigestCandidate:
    """One CSP candidate row in the digest.

    Reflects screener + allocator output. Per-account quantities are a
    list of (account_label, contracts) tuples so the formatter can
    render the inline `(2* Vikram Ind, 5* Yash Roth)` block.

    Most fields are display-as-given. `benchmark_median` enables
    🟢/🔴 coloring of IVR/VRP relative to rolling 90d ticker history.
    """

    rank: int                                # 1-indexed
    ticker: str
    strike: float
    expiry: str                              # e.g. "May 2"
    premium_dollars: float
    premium_pct: float                        # e.g. 2.1
    ray_pct: float                            # 78.0
    delta: float                              # 0.22
    otm_pct: float                            # 6.4
    ivr_pct: float                            # 42.0
    vrp: float                                # 1.38
    fwd_pe: float | None                     # 18.4 or None if unknown
    week52_low: float
    week52_high: float
    spot: float
    week52_pct_of_range: float                # 0..100
    analyst_avg: float | None                # 4.1 or None
    analyst_sources_blurb: str | None        # "IBKR 4, TV Strong Buy, ..."
    per_account: list[tuple[str, int]] = field(default_factory=list)
    # Optional: rolling 90d benchmarks for coloring
    ivr_benchmark_median: float | None = None
    vrp_benchmark_median: float | None = None


@dataclass(frozen=True)
class DigestCommentary:
    """LLM-generated annotation per candidate.

    `concern_flag` ∈ {"red", "blue", None}. If non-null, `concern_reason`
    must be present. The formatter pins flagged candidates above the
    normal list and renders the flag emoji + reason in the card body.
    """

    ticker: str
    news_summary: str
    macro_context: str
    rank_rationale: str
    concern_flag: ConcernFlag | None = None
    concern_reason: str | None = None


@dataclass(frozen=True)
class DigestPayload:
    """Bundle the formatter consumes to render the full digest message.

    `mode` = "LIVE" or "PAPER" — drives header text + presence of
    inline-keyboard. `timeout_at_utc` is when unresolved cards SKIP.
    """

    mode: Literal["LIVE", "PAPER"]
    sent_at_utc: datetime
    timeout_at_utc: datetime
    candidates: list[DigestCandidate]
    commentaries: dict[str, DigestCommentary]    # keyed by ticker
    accounts_blurb: str                          # "Yash Ind • Yash Roth • Vikram Ind"
    rank_disagreement: dict | None = None        # Reading 2 optional
    macro_blurb: str | None = None               # one-line macro summary
