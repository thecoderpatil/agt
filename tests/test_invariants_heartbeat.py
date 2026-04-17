"""ADR-007 Step 4 — scheduler heartbeat wiring for safety invariants.

Covers:
    * ``agt_scheduler._evidence_fingerprint`` — stable, order-independent,
      non-serializable-safe.
    * ``agt_scheduler._check_invariants_tick`` — registers one incident per
      Violation with YAML-sourced severity + scrutiny_tier, swallows every
      failure mode (import, load_invariants, run_all, register).
    * ``agt_scheduler._heartbeat_job`` integration — tick is invoked every
      60s, tick exceptions do not propagate into the APScheduler loop.

The tests monkeypatch on ``agt_equities.invariants`` and
``agt_equities.incidents_repo`` because ``_check_invariants_tick`` imports
lazily inside the function body (import happens at call time, so attribute
lookups resolve against the monkeypatched modules).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

def _make_violation(
    invariant_id: str = "NO_LIVE_IN_PAPER",
    description: str = "dummy violation",
    evidence: dict[str, Any] | None = None,
    severity: str = "critical",
):
    """Build a real ``Violation`` via the production dataclass."""
    from agt_equities.invariants.types import Violation
    return Violation(
        invariant_id=invariant_id,
        description=description,
        evidence=evidence if evidence is not None else {"row_id": 1},
        severity=severity,
    )


def _fake_manifest() -> list[dict[str, Any]]:
    """Minimal manifest matching safety_invariants.yaml shape."""
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
def scheduler_module():
    """Return the live agt_scheduler module (reloads to reset state if needed)."""
    import agt_scheduler
    return agt_scheduler


@pytest.fixture
def patched_invariants(monkeypatch):
    """Monkeypatch run_all + load_invariants + incidents_repo.register.

    Returns a dict with ``register_calls`` list (every call's kwargs),
    ``run_all_results`` mutable dict the test sets to drive the tick, and
    ``manifest`` the test can mutate to drive metadata lookup.
    """
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

def test_evidence_fingerprint_empty_is_stable(scheduler_module):
    a = scheduler_module._evidence_fingerprint({})
    b = scheduler_module._evidence_fingerprint({})
    c = scheduler_module._evidence_fingerprint(None)
    assert a == b == c
    assert len(a) == 12


def test_evidence_fingerprint_order_independent(scheduler_module):
    a = scheduler_module._evidence_fingerprint({"x": 1, "y": 2, "z": [3, 4]})
    b = scheduler_module._evidence_fingerprint({"z": [3, 4], "y": 2, "x": 1})
    assert a == b


def test_evidence_fingerprint_different_inputs_differ(scheduler_module):
    a = scheduler_module._evidence_fingerprint({"row_id": 1})
    b = scheduler_module._evidence_fingerprint({"row_id": 2})
    assert a != b


def test_evidence_fingerprint_non_serializable_does_not_raise(scheduler_module):
    # datetime objects are not JSON serializable by default.
    evidence = {"ts": datetime(2026, 4, 16, tzinfo=timezone.utc), "n": 5}
    fp = scheduler_module._evidence_fingerprint(evidence)
    assert isinstance(fp, str)
    assert len(fp) == 12


def test_evidence_fingerprint_length_is_12_chars(scheduler_module):
    fp = scheduler_module._evidence_fingerprint({"a": "b"})
    # SHA1 truncated to 12 chars per the module's design.
    assert len(fp) == 12
    int(fp, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# _check_invariants_tick — happy paths
# ---------------------------------------------------------------------------

def test_tick_no_violations_registers_nothing(scheduler_module, patched_invariants):
    patched_invariants["run_all_results"] = {
        "NO_LIVE_IN_PAPER": [],
        "NO_BELOW_BASIS_CC": [],
    }
    scheduler_module._check_invariants_tick()
    assert patched_invariants["register_calls"] == []


def test_tick_one_violation_registers_one_incident(
    scheduler_module, patched_invariants
):
    v = _make_violation(evidence={"row_id": 42})
    patched_invariants["run_all_results"] = {"NO_LIVE_IN_PAPER": [v]}
    scheduler_module._check_invariants_tick()
    assert len(patched_invariants["register_calls"]) == 1
    call = patched_invariants["register_calls"][0]
    assert call["incident_key"].startswith("NO_LIVE_IN_PAPER:")
    # Severity + scrutiny_tier are pulled from the YAML entry, not the
    # Violation. NO_LIVE_IN_PAPER has severity_floor=critical, scrutiny=
    # architect_only in the fake manifest (matching real safety_invariants.yaml).
    assert call["severity"] == "critical"
    assert call["scrutiny_tier"] == "architect_only"
    assert call["detector"] == "agt_scheduler.heartbeat"
    assert call["invariant_id"] == "NO_LIVE_IN_PAPER"
    observed = call["observed_state"]
    assert observed["description"] == "dummy violation"
    assert observed["evidence"] == {"row_id": 42}
    assert observed["detected_at"] is not None


def test_tick_pulls_scrutiny_and_severity_per_invariant(
    scheduler_module, patched_invariants
):
    v_live = _make_violation("NO_LIVE_IN_PAPER", evidence={"r": 1})
    v_cc = _make_violation("NO_BELOW_BASIS_CC", evidence={"r": 2})
    patched_invariants["run_all_results"] = {
        "NO_LIVE_IN_PAPER": [v_live],
        "NO_BELOW_BASIS_CC": [v_cc],
    }
    scheduler_module._check_invariants_tick()
    calls_by_inv = {c["invariant_id"]: c for c in patched_invariants["register_calls"]}
    assert calls_by_inv["NO_LIVE_IN_PAPER"]["severity"] == "critical"
    assert calls_by_inv["NO_LIVE_IN_PAPER"]["scrutiny_tier"] == "architect_only"
    assert calls_by_inv["NO_BELOW_BASIS_CC"]["severity"] == "high"
    assert calls_by_inv["NO_BELOW_BASIS_CC"]["scrutiny_tier"] == "low"


def test_tick_unknown_invariant_falls_back_to_medium(
    scheduler_module, patched_invariants
):
    """YAML miss → register with ``medium`` / ``medium`` rather than crash."""
    v = _make_violation(invariant_id="INVENTED_INVARIANT", evidence={"x": 1})
    patched_invariants["run_all_results"] = {"INVENTED_INVARIANT": [v]}
    # Manifest does NOT contain INVENTED_INVARIANT.
    scheduler_module._check_invariants_tick()
    assert len(patched_invariants["register_calls"]) == 1
    call = patched_invariants["register_calls"][0]
    assert call["severity"] == "medium"
    assert call["scrutiny_tier"] == "medium"


def test_tick_multiple_violations_same_invariant_distinct_evidence_distinct_keys(
    scheduler_module, patched_invariants
):
    v1 = _make_violation(evidence={"row_id": 1})
    v2 = _make_violation(evidence={"row_id": 2})
    patched_invariants["run_all_results"] = {"NO_LIVE_IN_PAPER": [v1, v2]}
    scheduler_module._check_invariants_tick()
    assert len(patched_invariants["register_calls"]) == 2
    keys = [c["incident_key"] for c in patched_invariants["register_calls"]]
    assert keys[0] != keys[1]
    assert all(k.startswith("NO_LIVE_IN_PAPER:") for k in keys)


def test_tick_idempotent_key_for_same_evidence(
    scheduler_module, patched_invariants
):
    """Repeat detections of the exact same breach must hit the same key
    so the idempotent ``register`` call bumps consecutive_breaches."""
    v1 = _make_violation(evidence={"row_id": 99})
    v2 = _make_violation(evidence={"row_id": 99})
    patched_invariants["run_all_results"] = {"NO_LIVE_IN_PAPER": [v1, v2]}
    scheduler_module._check_invariants_tick()
    assert len(patched_invariants["register_calls"]) == 2
    keys = [c["incident_key"] for c in patched_invariants["register_calls"]]
    assert keys[0] == keys[1]


def test_tick_observed_state_includes_description_evidence_detected_at(
    scheduler_module, patched_invariants
):
    v = _make_violation(
        description="below-basis CC @ 40 < 86.61",
        evidence={"ticker": "UBER", "strike": 40.0, "basis": 86.61},
    )
    patched_invariants["run_all_results"] = {"NO_LIVE_IN_PAPER": [v]}
    scheduler_module._check_invariants_tick()
    observed = patched_invariants["register_calls"][0]["observed_state"]
    assert observed["description"] == "below-basis CC @ 40 < 86.61"
    assert observed["evidence"]["ticker"] == "UBER"
    assert observed["evidence"]["strike"] == 40.0
    # detected_at is ISO-8601, parseable, tz-aware.
    parsed = datetime.fromisoformat(observed["detected_at"])
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# _check_invariants_tick — failure modes (belt-and-suspenders)
# ---------------------------------------------------------------------------

def test_tick_register_failure_does_not_propagate(
    scheduler_module, patched_invariants
):
    v = _make_violation(evidence={"row_id": 1})
    patched_invariants["run_all_results"] = {"NO_LIVE_IN_PAPER": [v]}
    patched_invariants["register_raises"] = RuntimeError("DB locked")
    # Must not raise — live capital, heartbeat must survive.
    scheduler_module._check_invariants_tick()
    # Nothing accumulated because register raised before append.
    assert patched_invariants["register_calls"] == []


def test_tick_run_all_failure_does_not_propagate(
    scheduler_module, patched_invariants
):
    patched_invariants["run_all_raises"] = RuntimeError("yaml parse error")
    scheduler_module._check_invariants_tick()
    assert patched_invariants["register_calls"] == []


def test_tick_load_invariants_failure_does_not_propagate(
    scheduler_module, patched_invariants
):
    patched_invariants["load_raises"] = FileNotFoundError("yaml missing")
    scheduler_module._check_invariants_tick()
    assert patched_invariants["register_calls"] == []


def test_tick_register_failure_for_one_violation_does_not_stop_others(
    scheduler_module, patched_invariants, monkeypatch
):
    """If register raises for violation #1, violation #2 should still be
    attempted — each register call sits in its own try/except."""
    call_count = {"n": 0}
    accepted: list[dict] = []

    def flaky_register(incident_key, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient")
        entry = {"incident_key": incident_key, **kwargs}
        accepted.append(entry)
        return {"incident_key": incident_key, "id": 1}

    import agt_equities.incidents_repo as ir_mod
    monkeypatch.setattr(ir_mod, "register", flaky_register)

    v1 = _make_violation(evidence={"row_id": 1})
    v2 = _make_violation(evidence={"row_id": 2})
    patched_invariants["run_all_results"] = {"NO_LIVE_IN_PAPER": [v1, v2]}
    scheduler_module._check_invariants_tick()
    assert call_count["n"] == 2
    assert len(accepted) == 1
    assert accepted[0]["observed_state"]["evidence"] == {"row_id": 2}


# ---------------------------------------------------------------------------
# Heartbeat job integration — tick is actually wired in
# ---------------------------------------------------------------------------

def _get_heartbeat_callable(scheduler_module, monkeypatch):
    """Return the registered heartbeat_writer job's callable, with the
    ``write_heartbeat`` import inside ``register_jobs`` replaced by a stub so
    the test does not touch a real DB."""
    # register_jobs imports write_heartbeat + sweep_orphan_staged_orders from
    # agt_equities.health locally. Patch the source module BEFORE register_jobs
    # runs so the closure picks up the fake.
    import agt_equities.health as health_mod
    calls: list[dict] = []

    def fake_write_heartbeat(daemon_name, *, client_id=None, notes=None, **kwargs):
        calls.append({
            "daemon_name": daemon_name, "client_id": client_id, "notes": notes,
        })

    monkeypatch.setattr(health_mod, "write_heartbeat", fake_write_heartbeat)
    # Stub orphan sweep so register_jobs does not try to touch a DB.
    monkeypatch.setattr(
        health_mod, "sweep_orphan_staged_orders", lambda *a, **k: 0
    )

    from agt_equities.ib_conn import IBConnector, IBConnConfig
    sched = scheduler_module.build_scheduler()
    conn = IBConnector(config=IBConnConfig(client_id=2))
    scheduler_module.register_jobs(sched, conn)
    job = sched.get_job("heartbeat_writer")
    assert job is not None, "heartbeat_writer not registered"
    return job.func, calls


def test_heartbeat_job_invokes_invariant_tick(
    scheduler_module, patched_invariants, monkeypatch
):
    hb_fn, hb_calls = _get_heartbeat_callable(scheduler_module, monkeypatch)
    v = _make_violation(evidence={"row_id": 7})
    patched_invariants["run_all_results"] = {"NO_LIVE_IN_PAPER": [v]}
    hb_fn()
    # write_heartbeat ran once.
    assert len(hb_calls) == 1
    # Invariant tick ran and registered one incident.
    assert len(patched_invariants["register_calls"]) == 1


def test_heartbeat_job_continues_when_tick_blows_up(
    scheduler_module, monkeypatch
):
    """Inner _check_invariants_tick catches everything, but we also wrap
    the call inside _heartbeat_job — belt and suspenders. Simulate a
    hard failure by monkeypatching _check_invariants_tick to raise."""
    hb_fn, hb_calls = _get_heartbeat_callable(scheduler_module, monkeypatch)

    def boom():
        raise RuntimeError("wired-in tick blew up")

    monkeypatch.setattr(scheduler_module, "_check_invariants_tick", boom)
    # Must NOT raise. Heartbeat must survive.
    hb_fn()
    assert len(hb_calls) == 1  # write_heartbeat still ran


def test_heartbeat_job_write_heartbeat_still_runs_when_tick_raises(
    scheduler_module, monkeypatch
):
    """Ordering guarantee: write_heartbeat runs BEFORE the invariant tick,
    so a tick crash cannot suppress the liveness signal."""
    hb_fn, hb_calls = _get_heartbeat_callable(scheduler_module, monkeypatch)
    monkeypatch.setattr(
        scheduler_module, "_check_invariants_tick",
        lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    hb_fn()
    assert hb_calls[0]["daemon_name"] == scheduler_module.DAEMON_NAME
