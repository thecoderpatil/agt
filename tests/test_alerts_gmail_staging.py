"""MR #2 — alerts.stage_gmail_draft() staging semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.agt_tripwire_exempt


def _fake_alert(severity="crit", kind="TEST_KIND", aid=77):
    return {
        "id": aid,
        "created_ts": 1_700_000_000.0,
        "kind": kind,
        "severity": severity,
        "payload": {"subject": "Test subject", "body": "Test body line"},
        "attempts": 0,
    }


def test_non_crit_alerts_are_not_staged(tmp_path):
    from agt_equities.alerts import stage_gmail_draft

    for sev in ("info", "warn", "debug"):
        out = stage_gmail_draft(_fake_alert(severity=sev), output_dir=tmp_path)
        assert out is None, f"severity={sev!r} must not be staged"
    assert list(tmp_path.iterdir()) == []


def test_crit_alert_writes_json_draft(tmp_path):
    from agt_equities.alerts import stage_gmail_draft

    alert = _fake_alert()
    out = stage_gmail_draft(alert, output_dir=tmp_path)
    assert out is not None
    assert out.exists()
    assert out.suffix == ".json"

    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["severity"] == "crit"
    assert doc["kind"] == "TEST_KIND"
    assert doc["alert_id"] == 77
    assert doc["to"] == "yashpatil@gmail.com"
    assert doc["subject"].startswith("[AGT CRIT] TEST_KIND")
    assert "Test subject" in doc["subject"]
    assert doc["body"] == "Test body line"
    assert doc["payload"]["body"] == "Test body line"


def test_payload_can_be_raw_string(tmp_path):
    from agt_equities.alerts import stage_gmail_draft

    alert = _fake_alert()
    alert["payload"] = json.dumps({"subject": "s2", "body": "b2"})
    out = stage_gmail_draft(alert, output_dir=tmp_path)
    assert out is not None
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["subject"].endswith("s2")
    assert doc["body"] == "b2"


def test_unserializable_payload_falls_back_gracefully(tmp_path):
    """String payload that isn't JSON is wrapped under '_raw'."""
    from agt_equities.alerts import stage_gmail_draft

    alert = _fake_alert()
    alert["payload"] = "not-json{"
    out = stage_gmail_draft(alert, output_dir=tmp_path)
    assert out is not None
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert "_raw" in doc["payload"]


def test_gmail_drafts_dir_default_is_under_logs():
    from agt_equities.alerts import GMAIL_DRAFTS_DIR

    assert GMAIL_DRAFTS_DIR.parts[-2:] == ("logs", "gmail_drafts")
