"""tests/test_observability_digest.py — ADR-017 §9 Mega-MR A.1.

Covers build_observability_snapshot + render_observability_card:
  - all five sources are queried once per snapshot
  - renderer surfaces section_error rather than swallowing
  - each section preserves its native severity model
  - G1/G3/G4 render as "not yet instrumented" independent of underlying status
  - fail-soft: one broken source does not derail the others
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agt_equities.observability import digest as obs_digest
from agt_equities.observability.digest import (
    FlexStatus,
    HeartbeatStatus,
    ObservabilitySnapshot,
    PromotionGateRow,
    build_observability_snapshot,
    render_observability_card,
)

pytestmark = pytest.mark.sprint_a


def _stub_gate(gate_id: str, status: str = "green", message: str = "ok"):
    return SimpleNamespace(
        gate_id=gate_id, status=status, value=None, threshold=None, message=message
    )


@pytest.fixture
def patch_sources(monkeypatch):
    """Patch each upstream source with a deterministic fixture."""
    calls = {"architect": 0, "authorable": 0, "heartbeats": 0, "flex": 0, "promotion": 0}

    def fake_list_architect_only(**kw):
        calls["architect"] += 1
        return [{"invariant_id": "FAKE_ARCH", "scrutiny_tier": "architect_only",
                 "status": "open", "consecutive_breaches": 3}]

    def fake_list_authorable(**kw):
        calls["authorable"] += 1
        return [{"invariant_id": "FAKE_AUTH", "scrutiny_tier": "medium",
                 "status": "rejected_once", "consecutive_breaches": 2}]

    def fake_heartbeats(*, now_utc, db_path=None):
        calls["heartbeats"] += 1
        return [
            HeartbeatStatus("agt_bot", now_utc, 30.0, "fresh"),
            HeartbeatStatus("agt_scheduler", now_utc, 600.0, "stale"),
        ]

    def fake_flex(*, now_utc, db_path=None):
        calls["flex"] += 1
        return FlexStatus(
            last_sync_utc=now_utc,
            status="success",
            zero_row_suspicion=False,
            stale=False,
            sync_id=42,
        )

    def fake_promotion(*, db_path=None):
        calls["promotion"] += 1
        out: list[PromotionGateRow] = []
        for eng in ("entry", "exit"):
            out.append(PromotionGateRow(eng, "G1", "not yet instrumented", "stub"))
            out.append(PromotionGateRow(eng, "G2", "green", "0 trips"))
            out.append(PromotionGateRow(eng, "G3", "not yet instrumented", "stub"))
            out.append(PromotionGateRow(eng, "G4", "not yet instrumented", "stub"))
            out.append(PromotionGateRow(eng, "G5", "green", "N/A"))
        return out

    import agt_equities.incidents_repo as ir
    monkeypatch.setattr(ir, "list_architect_only", fake_list_architect_only)
    monkeypatch.setattr(ir, "list_authorable", fake_list_authorable)
    monkeypatch.setattr(obs_digest, "_query_heartbeats", fake_heartbeats)
    monkeypatch.setattr(obs_digest, "_flex_snapshot", fake_flex)
    monkeypatch.setattr(obs_digest, "_promotion_rows", fake_promotion)
    return calls


def test_snapshot_queries_all_five_sources(patch_sources):
    snap = build_observability_snapshot()
    assert patch_sources == {"architect": 1, "authorable": 1, "heartbeats": 1,
                              "flex": 1, "promotion": 1}
    assert snap.architect_only_error is None
    assert snap.authorable_error is None
    assert snap.heartbeats_error is None
    assert snap.flex_error is None
    assert snap.promotion_error is None
    assert len(snap.architect_only) == 1
    assert len(snap.authorable) == 1
    assert len(snap.heartbeats) == 2
    assert snap.flex is not None and snap.flex.sync_id == 42
    assert len(snap.promotion) == 10


def test_renderer_handles_section_error_gracefully():
    snap = ObservabilitySnapshot(
        generated_at_utc=datetime(2026, 4, 24, 22, 35, tzinfo=timezone.utc),
        architect_only=[],
        architect_only_error="boom in architect",
        authorable=[],
        authorable_error=None,
        heartbeats=[],
        heartbeats_error="heartbeat query failed",
        flex=None,
        flex_error="flex boom",
        promotion=[],
        promotion_error=None,
    )
    card = render_observability_card(snap)
    # Each failed section must surface the error — not be silently empty.
    assert "section failed: boom in architect" in card
    assert "section failed: heartbeat query failed" in card
    assert "section failed: flex boom" in card
    # Non-failed sections render their empty-state sentinels.
    assert "📊 Authorable" in card
    assert "🚦 Promotion-gate" in card


def test_renderer_preserves_distinct_severity_models(patch_sources):
    snap = build_observability_snapshot()
    card = render_observability_card(snap)
    # Architect-only renders scrutiny_tier.
    assert "FAKE_ARCH" in card and "architect_only" in card
    # Authorable renders scrutiny_tier distinct from architect_only.
    assert "FAKE_AUTH" in card and "medium" in card
    # Heartbeats render age in seconds + per-state icon.
    assert "age=30s" in card or "30s" in card
    # Flex renders sync_id.
    assert "sync_id=42" in card
    # Promotion-gate rows carry engine + gate id.
    assert "entry.G2" in card and "exit.G5" in card


def test_promotion_gates_g1_g3_g4_rendered_as_not_yet_instrumented(patch_sources):
    snap = build_observability_snapshot()
    card = render_observability_card(snap)
    # G1 / G3 / G4 must be "not yet instrumented" regardless of upstream status.
    for gate in ("entry.G1", "entry.G3", "entry.G4", "exit.G1", "exit.G3", "exit.G4"):
        assert f"<code>{gate}</code> — not yet instrumented" in card, f"missing {gate}"
    # G2 / G5 render actual status (green in fixture).
    assert "✅ <code>entry.G2</code> green" in card
    assert "✅ <code>exit.G5</code> green" in card


def test_snapshot_fail_soft_on_broken_source(monkeypatch):
    """One source raises; the rest populate normally."""
    import agt_equities.incidents_repo as ir

    def boom(**kw):
        raise RuntimeError("upstream kaboom")

    def ok_authorable(**kw):
        return []

    def ok_heartbeats(*, now_utc, db_path=None):
        return []

    def ok_flex(*, now_utc, db_path=None):
        return FlexStatus(None, None, False, True, None)

    def ok_promotion(*, db_path=None):
        return []

    monkeypatch.setattr(ir, "list_architect_only", boom)
    monkeypatch.setattr(ir, "list_authorable", ok_authorable)
    monkeypatch.setattr(obs_digest, "_query_heartbeats", ok_heartbeats)
    monkeypatch.setattr(obs_digest, "_flex_snapshot", ok_flex)
    monkeypatch.setattr(obs_digest, "_promotion_rows", ok_promotion)

    snap = build_observability_snapshot()
    assert snap.architect_only_error == "upstream kaboom"
    assert snap.authorable_error is None
    assert snap.heartbeats_error is None
    assert snap.flex_error is None
    assert snap.promotion_error is None


def test_underscore_invariant_name_renders_verbatim():
    """Regression: NO_UNAPPROVED_LIVE_CSP must render with underscores intact.

    Bug 2 (Sprint 14 P5): parse_mode="Markdown" caused Telegram to treat
    underscores as italic delimiters, fragmenting the name into broken pieces.
    Under HTML mode the name is wrapped in <code>...</code> and underscores are
    preserved literally.
    """
    snap = ObservabilitySnapshot(
        generated_at_utc=datetime(2026, 4, 29, 7, 30, tzinfo=timezone.utc),
        architect_only=[{
            "invariant_id": "NO_UNAPPROVED_LIVE_CSP",
            "scrutiny_tier": "architect_only",
            "status": "open",
            "consecutive_breaches": 4108,
        }],
        architect_only_error=None,
        authorable=[],
        authorable_error=None,
        heartbeats=[],
        heartbeats_error=None,
        flex=None,
        flex_error=None,
        promotion=[],
        promotion_error=None,
    )
    card = render_observability_card(snap)
    assert "<code>NO_UNAPPROVED_LIVE_CSP</code>" in card, (
        "Invariant name with underscores must be wrapped in <code>; "
        "underscores must not be consumed as Markdown italic delimiters"
    )
    assert "breaches=4108" in card


def test_html_escape_applied_to_exception_strings():
    """html.escape must sanitise section_error strings containing HTML chars.

    Ensures exception messages with <, >, & are not rendered as raw HTML tags
    in the Telegram card.
    """
    snap = ObservabilitySnapshot(
        generated_at_utc=datetime(2026, 4, 29, 7, 30, tzinfo=timezone.utc),
        architect_only=[],
        architect_only_error="<script>alert(1)</script>",
        authorable=[],
        authorable_error=None,
        heartbeats=[],
        heartbeats_error=None,
        flex=None,
        flex_error="db error: table 'x' doesn't exist & pool exhausted",
        promotion=[],
        promotion_error=None,
    )
    card = render_observability_card(snap)
    assert "&lt;script&gt;" in card, "< must be escaped to &lt;"
    assert "<script>" not in card.replace("&lt;script&gt;", ""), (
        "Raw <script> tag must not appear in HTML output"
    )
    assert "&amp;" in card or "pool exhausted" in card
