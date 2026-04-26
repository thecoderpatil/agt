"""SQLiteOrderSink.stage() forwards engine/run_id/meta into ticket dicts."""
from __future__ import annotations

import pytest

from agt_equities.sinks import SQLiteOrderSink

pytestmark = pytest.mark.sprint_a


def test_stage_injects_engine_and_run_id():
    captured: list[list[dict]] = []
    sink = SQLiteOrderSink(staging_fn=lambda batch: captured.append(batch))
    tickets = [{"ticker": "MSFT", "right": "P", "strike": 400.0, "qty": 1, "limit": 1.25}]
    sink.stage(tickets, engine="csp_allocator", run_id="run-1234")
    assert len(captured) == 1
    enriched = captured[0][0]
    assert enriched["engine"] == "csp_allocator"
    assert enriched["run_id"] == "run-1234"
    assert "staged_at_utc" in enriched


def test_stage_injects_meta_fields_when_present():
    captured: list[list[dict]] = []
    sink = SQLiteOrderSink(staging_fn=lambda batch: captured.append(batch))
    tickets = [{"ticker": "AAPL", "right": "P", "strike": 180, "qty": 1, "limit": 0.50}]
    meta = {
        "broker_mode": "paper",
        "spot_at_staging": 192.34,
        "premium_at_staging": 0.55,
        "gate_verdicts": {"mode_match": True, "strike_freshness": True},
    }
    sink.stage(tickets, engine="cc_engine", run_id="r2", meta=meta)
    enriched = captured[0][0]
    assert enriched["broker_mode_at_staging"] == "paper"
    assert enriched["spot_at_staging"] == 192.34
    assert enriched["premium_at_staging"] == 0.55
    assert enriched["gate_verdicts"]["mode_match"] is True


def test_stage_setdefault_preserves_caller_value():
    captured: list[list[dict]] = []
    sink = SQLiteOrderSink(staging_fn=lambda batch: captured.append(batch))
    tickets = [{"ticker": "TSLA", "right": "P", "strike": 200, "qty": 1, "limit": 1,
                "engine": "manual_override", "run_id": "preset"}]
    sink.stage(tickets, engine="cc_engine", run_id="r3")
    enriched = captured[0][0]
    assert enriched["engine"] == "manual_override"
    assert enriched["run_id"] == "preset"


def test_stage_skips_empty_batch():
    captured: list[list[dict]] = []
    sink = SQLiteOrderSink(staging_fn=lambda batch: captured.append(batch))
    sink.stage([], engine="cc_engine", run_id="r4")
    assert captured == []
