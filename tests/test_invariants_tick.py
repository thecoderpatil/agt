"""MR !84 — shared invariants tick module.

``agt_equities.invariants.tick.check_invariants_tick`` runs every 60s in
whichever process owns the gated job set (bot when USE_SCHEDULER_DAEMON=0,
daemon when =1). Covered here:

    * ``_evidence_fingerprint`` — stable, order-independent, non-serializable-safe.
    * ``check_invariants_tick`` — registers one incident per Violation with
      YAML-sourced severity + scrutiny_tier, and swallows every failure
      mode (import, load_invariants, run_all, register).
    * Detector stamp — bot vs scheduler strings propagate into
      incidents_repo.register correctly.

Lazy imports inside the tick mean monkeypatching
``agt_equities.invariants.load_invariants``/``run_all`` and
``agt_equities.incidents_repo.register`` resolves against the patched
module attributes at call time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

pytestmark = pytest.mark.sprint_a


def _make_violation(
    invariant_id: str = "NO_LIVE_IN_PAPER",
    description: str = "dummy violation",
    evidence: dict[str, Any] | None = None,
    severity: str = "critical",
):
    from agt_equities.invariants.types import Violation
    return Violation(
        invariant_id=invariant_id,
        description=description,
        evidence=evidence if evidence is not None else {"row_id": 1},
        severity=severity,
    )


def _fake_manifest() -> list[dict[str, Any]]:
    return [
        {
            "id": "NO_LIVE_IN_PAPER",
            "description": "stub",
            "check_fn": "check_no_live_in_paper",
            "scrutiny_tier": "architect_only",
            "fix_by_sprint": "existing",
            "max_consecutive_violations": 1,
            "severity_floor": "critical",
        },
        {
            "id": "NO_BELOW_BASIS_CC",
            "description": "stub",
            "check_fn": "check_no_below_basis_cc",
            "scrutiny_tier": "low",
            "fix_by_sprint": "wheel-hardening",
            "max_consecutive_violations": 1,
            "severity_floor": "high",
        },
    ]


@pytest.fixture
def patched_tick(monkeypatch):
    import agt_equities.incidents_repo as ir_mod
    import agt_equities.invariants as inv_mod

    state: dict[str, Any] = {
        "register_calls": [],
        "run_all_results": {},
        "manifest": _fake_manifest(),
        "register_raises": None,
        "run_all_raises": None,
        "load_raises": None,
    }

    def fake_load_invariants(*args, **kwargs):
        if state["load_raises"] is not None:
            raise state["load_raises"]
        return state["manifest"]

    def fake_run_all(*args, **kwargs):
        if state["run_all_raises"] is not None:
            raise state["run_all_raises"]
        return state["run_all_results"]

    def fake_register(incident_key, **kwargs):
        if state["register_raises"] is not None:
            raise state["register_raises"]
        entry = {"incident_key": incident_key, **kwargs}
        state["register_calls"].append(entry)
        return {"incident_key": incident_key, "id": len(state["register_calls"])}

    monkeypatch.setattr(inv_mod, "load_invariants", fake_load_invariants)
    monkeypatch.setattr(inv_mod, "run_all", fake_run_all)
    monkeypatch.setattr(ir_mod, "register", fake_register)
    return state


# ---------------------------------------------------------------------------
# _evidence_fingerprint
# ---------------------------------------------------------------------------

def test_evidence_fingerprint_stable_across_key_order():
    from agt_equities.invariants.tick import _evidence_fingerprint
    a = _evidence_fingerprint({"b": 2, "a": 1})
    b = _evidence_fingerprint({"a": 1, "b": 2})
    assert a == b


def test_evidence_fingerprint_handles_none():
    from agt_equities.invariants.tick import _evidence_fingerprint
    fp = _evidence_fingerprint(None)
    assert isinstance(fp, str)
    assert len(fp) == 12


def test_evidence_fingerprint_handles_non_serializable():
    from agt_equities.invariants.tick import _evidence_fingerprint

    class Weird:
        def __repr__(self):
            return "<weird>"

    fp = _evidence_fingerprint({"ts": datetime.now(timezone.utc), "w": Weird()})
    assert isinstance(fp, str)
    assert len(fp) == 12


def test_evidence_fingerprint_differs_for_different_evidence():
    from agt_equities.invariants.tick import _evidence_fingerprint
    a = _evidence_fingerprint({"row_id": 1})
    b = _evidence_fingerprint({"row_id": 2})
    assert a != b


# ---------------------------------------------------------------------------
# check_invariants_tick — happy path
# ---------------------------------------------------------------------------

def test_tick_registers_one_incident_per_violation(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    v = _make_violation(evidence={"row_id": 7})
    patched_tick["run_all_results"] = {"NO_LIVE_IN_PAPER": [v]}
    n = check_invariants_tick()
    assert n == 1
    calls = patched_tick["register_calls"]
    assert len(calls) == 1
    assert calls[0]["invariant_id"] == "NO_LIVE_IN_PAPER"
    assert calls[0]["severity"] == "critical"
    assert calls[0]["scrutiny_tier"] == "architect_only"


def test_tick_zero_violations_zero_registers(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    patched_tick["run_all_results"] = {"NO_LIVE_IN_PAPER": []}
    n = check_invariants_tick()
    assert n == 0
    assert patched_tick["register_calls"] == []


def test_tick_same_evidence_yields_idempotent_key(patched_tick):
    """Two Violations with identical evidence produce identical keys."""
    from agt_equities.invariants.tick import check_invariants_tick
    v1 = _make_violation(evidence={"row_id": 7})
    v2 = _make_violation(evidence={"row_id": 7})
    patched_tick["run_all_results"] = {"NO_LIVE_IN_PAPER": [v1, v2]}
    check_invariants_tick()
    keys = [c["incident_key"] for c in patched_tick["register_calls"]]
    assert keys[0] == keys[1]  # idempotency punt to incidents_repo


def test_tick_different_evidence_yields_different_keys(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    v1 = _make_violation(evidence={"row_id": 7})
    v2 = _make_violation(evidence={"row_id": 8})
    patched_tick["run_all_results"] = {"NO_LIVE_IN_PAPER": [v1, v2]}
    check_invariants_tick()
    keys = [c["incident_key"] for c in patched_tick["register_calls"]]
    assert keys[0] != keys[1]


def test_tick_multiple_invariants(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    v1 = _make_violation(invariant_id="NO_LIVE_IN_PAPER", evidence={"row_id": 7})
    v2 = _make_violation(invariant_id="NO_BELOW_BASIS_CC", evidence={"cc_id": 42})
    patched_tick["run_all_results"] = {
        "NO_LIVE_IN_PAPER": [v1],
        "NO_BELOW_BASIS_CC": [v2],
    }
    n = check_invariants_tick()
    assert n == 2
    by_id = {
        c["invariant_id"]: c for c in patched_tick["register_calls"]
    }
    assert by_id["NO_LIVE_IN_PAPER"]["scrutiny_tier"] == "architect_only"
    assert by_id["NO_BELOW_BASIS_CC"]["scrutiny_tier"] == "low"
    assert by_id["NO_BELOW_BASIS_CC"]["severity"] == "high"


# ---------------------------------------------------------------------------
# check_invariants_tick — detector stamp
# ---------------------------------------------------------------------------

def test_tick_bot_detector_stamp(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    patched_tick["run_all_results"] = {
        "NO_LIVE_IN_PAPER": [_make_violation(evidence={"row_id": 1})]
    }
    check_invariants_tick(detector="telegram_bot.invariants_tick")
    assert (
        patched_tick["register_calls"][0]["detector"]
        == "telegram_bot.invariants_tick"
    )


def test_tick_scheduler_detector_stamp(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    patched_tick["run_all_results"] = {
        "NO_LIVE_IN_PAPER": [_make_violation(evidence={"row_id": 1})]
    }
    check_invariants_tick(detector="agt_scheduler.heartbeat")
    assert (
        patched_tick["register_calls"][0]["detector"]
        == "agt_scheduler.heartbeat"
    )


def test_tick_default_detector_stamp(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    patched_tick["run_all_results"] = {
        "NO_LIVE_IN_PAPER": [_make_violation(evidence={"row_id": 1})]
    }
    check_invariants_tick()
    assert (
        patched_tick["register_calls"][0]["detector"]
        == "adr_007.invariants.tick"
    )


# ---------------------------------------------------------------------------
# check_invariants_tick — never raises
# ---------------------------------------------------------------------------

def test_tick_swallows_load_invariants_failure(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    patched_tick["load_raises"] = RuntimeError("yaml parse explosion")
    n = check_invariants_tick()
    assert n == 0
    assert patched_tick["register_calls"] == []


def test_tick_swallows_run_all_failure(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    patched_tick["run_all_raises"] = RuntimeError("db boom")
    n = check_invariants_tick()
    assert n == 0
    assert patched_tick["register_calls"] == []


def test_tick_swallows_register_failure(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    patched_tick["register_raises"] = RuntimeError("db locked")
    patched_tick["run_all_results"] = {
        "NO_LIVE_IN_PAPER": [_make_violation()]
    }
    # Must not raise.
    n = check_invariants_tick()
    assert n == 0


def test_tick_continues_after_per_violation_failure(patched_tick, monkeypatch):
    """If register fails on one violation, tick still tries the rest."""
    from agt_equities.invariants.tick import check_invariants_tick
    import agt_equities.incidents_repo as ir_mod

    call_count = [0]

    def flaky_register(incident_key, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("first one blows up")
        patched_tick["register_calls"].append(
            {"incident_key": incident_key, **kwargs}
        )
        return {"incident_key": incident_key, "id": call_count[0]}

    monkeypatch.setattr(ir_mod, "register", flaky_register)
    v1 = _make_violation(invariant_id="NO_LIVE_IN_PAPER", evidence={"r": 1})
    v2 = _make_violation(invariant_id="NO_BELOW_BASIS_CC", evidence={"r": 2})
    patched_tick["run_all_results"] = {
        "NO_LIVE_IN_PAPER": [v1],
        "NO_BELOW_BASIS_CC": [v2],
    }
    n = check_invariants_tick()
    # first violation blew up, second succeeded
    assert n == 1
    assert len(patched_tick["register_calls"]) == 1


def test_tick_missing_meta_falls_back_to_medium(patched_tick):
    """Violation whose invariant_id is absent from the manifest gets
    the safe default severity/scrutiny so registration still proceeds."""
    from agt_equities.invariants.tick import check_invariants_tick
    patched_tick["manifest"] = []  # empty manifest -> no metadata
    patched_tick["run_all_results"] = {
        "UNKNOWN_INV": [_make_violation(invariant_id="UNKNOWN_INV")]
    }
    n = check_invariants_tick()
    assert n == 1
    c = patched_tick["register_calls"][0]
    assert c["severity"] == "medium"
    assert c["scrutiny_tier"] == "medium"


def test_tick_observed_state_serializes_detected_at(patched_tick):
    from agt_equities.invariants.tick import check_invariants_tick
    v = _make_violation(evidence={"row_id": 1})
    patched_tick["run_all_results"] = {"NO_LIVE_IN_PAPER": [v]}
    check_invariants_tick()
    obs = patched_tick["register_calls"][0]["observed_state"]
    # detected_at always present on Violation; must serialize to iso str.
    assert isinstance(obs["detected_at"], str)
    assert "T" in obs["detected_at"]


def test_tick_observed_state_handles_missing_detected_at(patched_tick):
    """Custom object without detected_at should null it out, not raise."""
    from agt_equities.invariants.tick import check_invariants_tick

    @dataclass
    class FakeViolation:
        invariant_id: str = "NO_LIVE_IN_PAPER"
        description: str = "fake"
        evidence: dict = None
        severity: str = "critical"

    patched_tick["run_all_results"] = {
        "NO_LIVE_IN_PAPER": [FakeViolation(evidence={"row_id": 1})]
    }
    n = check_invariants_tick()
    assert n == 1
    obs = patched_tick["register_calls"][0]["observed_state"]
    assert obs["detected_at"] is None
