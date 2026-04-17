"""Tests for agt_equities.author_critic — ADR-007 Step 6.

Covers the mechanical Critic pipeline, confidence arithmetic, state
write-back, the LLM-Critic escalation gate, and the CLI shim at
``scripts/author_critic.py``.

Tripwire discipline
-------------------

Every test opens its own temp SQLite DB and passes it as ``db_path``.
No test touches production state.

Mocking strategy
----------------

- ``subprocess.run`` is monkey-patched whenever pytest-under-pytest
  would otherwise recurse into the host test run.
- ``_check_invariant_still_present`` is exercised via the real
  ``invariants.runner`` code path with an invariant_id deliberately
  left off the manifest (fast-path return). One happy test uses a
  simulated still-present/absent result by calling the internal
  helpers directly.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest


pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# DB fixtures (mirror tests/test_incidents_repo.py)
# ---------------------------------------------------------------------------

_INCIDENTS_DDL = """
CREATE TABLE incidents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_key         TEXT NOT NULL,
    invariant_id         TEXT,
    severity             TEXT NOT NULL,
    scrutiny_tier        TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open',
    detector             TEXT NOT NULL,
    detected_at          TEXT NOT NULL,
    closed_at            TEXT,
    last_action_at       TEXT,
    consecutive_breaches INTEGER NOT NULL DEFAULT 1,
    observed_state       TEXT,
    desired_state        TEXT,
    confidence           REAL,
    mr_iid               INTEGER,
    ddiff_url            TEXT,
    rejection_history    TEXT
)
"""

_INCIDENTS_INDEXES = [
    """
    CREATE UNIQUE INDEX idx_incidents_active_key
    ON incidents(incident_key)
    WHERE status NOT IN ('merged','resolved','rejected_permanently')
    """,
    "CREATE INDEX idx_incidents_status ON incidents(status)",
    "CREATE INDEX idx_incidents_invariant_id ON incidents(invariant_id)",
    "CREATE INDEX idx_incidents_mr_iid ON incidents(mr_iid)",
]

_REMEDIATION_DDL = """
CREATE TABLE remediation_incidents (
    incident_id       TEXT PRIMARY KEY,
    first_detected    TEXT NOT NULL,
    directive_source  TEXT,
    fix_authored_at   TEXT,
    mr_iid            INTEGER,
    branch_name       TEXT,
    status            TEXT NOT NULL DEFAULT 'new',
    rejection_reasons TEXT,
    last_nudged_at    TEXT,
    architect_reason  TEXT,
    updated_at        TEXT
)
"""


def _init_db(tmp_path: Path, *, with_remediation: bool = True) -> str:
    db = tmp_path / "author_critic_test.db"
    conn = sqlite3.connect(db)
    conn.execute(_INCIDENTS_DDL)
    for stmt in _INCIDENTS_INDEXES:
        conn.execute(stmt)
    if with_remediation:
        conn.execute(_REMEDIATION_DDL)
    conn.commit()
    conn.close()
    return str(db)


def _seed(
    db_path: str,
    *,
    incident_key: str = "TEST_INCIDENT",
    invariant_id: str | None = None,
    scrutiny_tier: str = "medium",
    severity: str = "warn",
    status: str = "open",
) -> int:
    from agt_equities import incidents_repo as repo  # noqa: WPS433

    row = repo.register(
        incident_key,
        severity=severity,
        scrutiny_tier=scrutiny_tier,
        detector="test_seed",
        invariant_id=invariant_id,
        db_path=db_path,
    )
    incident_id = int(row["id"])
    if status != "open":
        # Advance to the seeded status without triggering allowed-from checks
        # by issuing raw UPDATEs. Tests that need a particular status are
        # usually exercising guardrails, not the state machine itself.
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE incidents SET status = ? WHERE id = ?",
                (status, incident_id),
            )
            conn.commit()
        finally:
            conn.close()
    return incident_id


# ---------------------------------------------------------------------------
# MechanicalCriticResult
# ---------------------------------------------------------------------------

def test_mech_result_json_roundtrip() -> None:
    from agt_equities.author_critic import MechanicalCriticResult

    mech = MechanicalCriticResult(
        passed=True,
        failed_checks=[],
        evidence={"path_whitelist": {"passed": True, "protected_hits": []}},
        computed_confidence=0.85,
        needs_fixup=False,
        needs_architect_reason=None,
    )
    blob = mech.to_json()
    rt = MechanicalCriticResult.from_json(blob)
    assert rt.passed is True
    assert rt.computed_confidence == pytest.approx(0.85)
    assert rt.failed_checks == []
    assert rt.needs_fixup is False
    assert rt.needs_architect_reason is None


def test_mech_result_from_json_tolerates_missing_optional_fields() -> None:
    from agt_equities.author_critic import MechanicalCriticResult

    blob = json.dumps({
        "passed": False,
        "failed_checks": ["path_whitelist"],
        "evidence": {},
        "computed_confidence": 0.0,
    })
    rt = MechanicalCriticResult.from_json(blob)
    assert rt.passed is False
    assert rt.needs_fixup is False
    assert rt.needs_architect_reason is None


# ---------------------------------------------------------------------------
# fetch_mr_changed_paths
# ---------------------------------------------------------------------------

def test_fetch_mr_changed_paths_parses_new_paths(monkeypatch) -> None:
    from agt_equities import author_critic
    from agt_equities import remediation

    def fake_request(method, path, payload=None):
        assert method == "GET"
        assert path == "/projects/81096827/merge_requests/42/changes"
        return {
            "changes": [
                {"new_path": "foo.py", "old_path": "foo.py"},
                {"new_path": "bar.py"},
                {"old_path": "gone.py"},  # deletion — old_path fallback
                "not-a-dict",             # malformed — ignored
            ],
        }

    monkeypatch.setattr(remediation, "_gitlab_request", fake_request)
    paths = author_critic.fetch_mr_changed_paths(42)
    assert paths == ["foo.py", "bar.py", "gone.py"]


def test_fetch_mr_changed_paths_swallows_network_error(monkeypatch) -> None:
    from agt_equities import author_critic
    from agt_equities import remediation

    def boom(*_, **__):
        raise RuntimeError("network down")

    monkeypatch.setattr(remediation, "_gitlab_request", boom)
    assert author_critic.fetch_mr_changed_paths(99) == []


# ---------------------------------------------------------------------------
# run_mechanical_critic — path whitelist + tier + invariant cleared
# ---------------------------------------------------------------------------

def test_run_mechanical_happy_path_skip_pytest(tmp_path) -> None:
    from agt_equities import author_critic

    incident = {
        "id": 1,
        "incident_key": "ACCT_BALANCE_DRIFT",
        "invariant_id": None,
        "scrutiny_tier": "medium",
    }
    mech = author_critic.run_mechanical_critic(
        incident=incident,
        author_confidence=0.78,
        changed_paths=["agt_equities/some_safe_module.py"],
        skip_pytest=True,
    )
    assert mech.passed is True
    assert mech.failed_checks == []
    assert mech.needs_fixup is False
    assert mech.needs_architect_reason is None
    assert mech.computed_confidence == pytest.approx(0.78)
    assert mech.evidence["path_whitelist"]["passed"] is True
    assert mech.evidence["pytest"]["skipped"] is True


def test_run_mechanical_path_whitelist_hit_forces_architect(tmp_path) -> None:
    from agt_equities import author_critic

    incident = {
        "id": 1,
        "incident_key": "BAD_EDIT",
        "invariant_id": None,
        "scrutiny_tier": "medium",
    }
    mech = author_critic.run_mechanical_critic(
        incident=incident,
        author_confidence=0.95,
        changed_paths=["agt_equities/walker.py", "tests/test_foo.py"],
        skip_pytest=True,
    )
    assert mech.passed is False
    assert "path_whitelist" in mech.failed_checks
    assert mech.computed_confidence == 0.0
    assert mech.needs_architect_reason is not None
    assert "walker.py" in mech.needs_architect_reason
    assert mech.needs_fixup is False


def test_run_mechanical_architect_only_tier_blocks(tmp_path) -> None:
    from agt_equities import author_critic

    incident = {
        "id": 1,
        "incident_key": "ARCH_ONLY",
        "invariant_id": None,
        "scrutiny_tier": "architect_only",
    }
    mech = author_critic.run_mechanical_critic(
        incident=incident,
        author_confidence=0.99,
        changed_paths=["agt_equities/any_file.py"],
        skip_pytest=True,
    )
    assert mech.passed is False
    assert "architect_only_tier" in mech.failed_checks
    assert mech.needs_architect_reason is not None
    assert mech.computed_confidence == 0.0


def test_run_mechanical_invariant_cleared_signals_resolve(
    tmp_path, monkeypatch,
) -> None:
    from agt_equities import author_critic

    # Patch the invariant check to simulate "no longer firing".
    monkeypatch.setattr(
        author_critic,
        "_check_invariant_still_present",
        lambda **kw: (False, {"violations": 0}),
    )
    incident = {
        "id": 1,
        "incident_key": "CLEARED",
        "invariant_id": "I1_SOMETHING",
        "scrutiny_tier": "medium",
    }
    mech = author_critic.run_mechanical_critic(
        incident=incident,
        author_confidence=0.8,
        changed_paths=["agt_equities/any.py"],
        skip_pytest=True,
    )
    # Still a clean mechanical pass — the resolve signal is in evidence.
    assert mech.passed is True
    assert mech.evidence["invariant_still_present"]["still_present"] is False


def test_run_mechanical_invariant_still_firing(
    tmp_path, monkeypatch,
) -> None:
    from agt_equities import author_critic

    monkeypatch.setattr(
        author_critic,
        "_check_invariant_still_present",
        lambda **kw: (True, {"violations": 3}),
    )
    incident = {
        "id": 1,
        "incident_key": "STILL",
        "invariant_id": "I1",
        "scrutiny_tier": "medium",
    }
    mech = author_critic.run_mechanical_critic(
        incident=incident,
        author_confidence=0.7,
        changed_paths=["agt_equities/any.py"],
        skip_pytest=True,
    )
    assert mech.passed is True
    assert mech.evidence["invariant_still_present"]["still_present"] is True


def test_run_mechanical_pytest_fail_triggers_fixup(
    tmp_path, monkeypatch,
) -> None:
    from agt_equities import author_critic

    class FakeProc:
        def __init__(self):
            self.returncode = 1
            self.stdout = (
                "tests/test_new.py::test_thing FAILED\n"
                "1 failed in 0.12s\n"
            )
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        assert "pytest" in cmd
        return FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)

    incident = {
        "id": 1,
        "incident_key": "PYTEST_FAIL",
        "invariant_id": None,
        "scrutiny_tier": "medium",
    }
    mech = author_critic.run_mechanical_critic(
        incident=incident,
        author_confidence=0.9,
        changed_paths=["tests/test_new.py"],
        workdir=tmp_path,
        skip_pytest=False,
    )
    assert mech.passed is False
    assert "pytest" in mech.failed_checks
    assert mech.needs_fixup is True
    assert mech.computed_confidence == pytest.approx(0.3)
    assert "FAILED" in mech.evidence["pytest"]["tail"]


def test_run_mechanical_pytest_timeout_no_fixup_loop_starvation(
    tmp_path, monkeypatch,
) -> None:
    from agt_equities import author_critic

    def timeout_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout_run)

    incident = {
        "id": 1,
        "incident_key": "PYTEST_TIMEOUT",
        "invariant_id": None,
        "scrutiny_tier": "medium",
    }
    mech = author_critic.run_mechanical_critic(
        incident=incident,
        author_confidence=0.9,
        changed_paths=["tests/test_slow.py"],
        workdir=tmp_path,
        pytest_timeout_sec=1,
        skip_pytest=False,
    )
    assert mech.passed is False
    assert "pytest" in mech.failed_checks
    assert mech.evidence["pytest"]["timeout"] is True
    assert mech.needs_fixup is True


def test_run_mechanical_pytest_smoke_when_no_test_files_changed(
    tmp_path, monkeypatch,
) -> None:
    from agt_equities import author_critic

    recorded = {}

    class FakeProc:
        def __init__(self):
            self.returncode = 0
            self.stdout = "1 passed in 0.05s\n"
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)

    incident = {
        "id": 1,
        "incident_key": "NO_TEST_FILES",
        "invariant_id": None,
        "scrutiny_tier": "medium",
    }
    mech = author_critic.run_mechanical_critic(
        incident=incident,
        author_confidence=0.6,
        changed_paths=["agt_equities/some_code.py"],
        workdir=tmp_path,
        skip_pytest=False,
    )
    assert mech.passed is True
    assert mech.evidence["pytest"]["mode"] == "smoke_sprint_a"
    # The smoke-mode invocation includes -m sprint_a, not targeted files.
    assert "-m" in recorded["cmd"]
    assert "sprint_a" in recorded["cmd"]


def test_run_mechanical_validates_author_confidence_range() -> None:
    from agt_equities import author_critic

    with pytest.raises(ValueError):
        author_critic.run_mechanical_critic(
            incident={"scrutiny_tier": "medium", "invariant_id": None},
            author_confidence=1.5,
            changed_paths=[],
            skip_pytest=True,
        )


# ---------------------------------------------------------------------------
# should_escalate_to_llm_critic
# ---------------------------------------------------------------------------

def _mock_passing_mech(author_confidence: float = 0.5):
    from agt_equities.author_critic import MechanicalCriticResult

    return MechanicalCriticResult(
        passed=True,
        failed_checks=[],
        evidence={
            "path_whitelist": {"passed": True, "protected_hits": []},
            "invariant_still_present": {"still_present": True},
        },
        computed_confidence=author_confidence,
        needs_fixup=False,
        needs_architect_reason=None,
    )


def test_escalation_only_on_high_tier_low_conf_and_pass() -> None:
    from agt_equities import author_critic

    mech = _mock_passing_mech(0.5)
    high_incident = {"scrutiny_tier": "high"}
    med_incident = {"scrutiny_tier": "medium"}

    assert author_critic.should_escalate_to_llm_critic(
        incident=high_incident, author_confidence=0.5, mech=mech,
    ) is True
    assert author_critic.should_escalate_to_llm_critic(
        incident=med_incident, author_confidence=0.5, mech=mech,
    ) is False
    assert author_critic.should_escalate_to_llm_critic(
        incident=high_incident, author_confidence=0.9, mech=mech,
    ) is False


def test_escalation_never_on_mech_fail() -> None:
    from agt_equities import author_critic
    from agt_equities.author_critic import MechanicalCriticResult

    failed_mech = MechanicalCriticResult(
        passed=False,
        failed_checks=["path_whitelist"],
        evidence={},
        computed_confidence=0.0,
        needs_architect_reason="hit walker.py",
    )
    assert author_critic.should_escalate_to_llm_critic(
        incident={"scrutiny_tier": "high"},
        author_confidence=0.1,
        mech=failed_mech,
    ) is False


# ---------------------------------------------------------------------------
# record_author_outcome
# ---------------------------------------------------------------------------

def test_record_author_transitions_open_to_authoring(tmp_path) -> None:
    from agt_equities import author_critic, incidents_repo

    db_path = _init_db(tmp_path)
    iid = _seed(db_path, status="open")

    row = author_critic.record_author_outcome(
        iid,
        author_confidence=0.82,
        branch="feat/step6-fix-123",
        mr_iid=99,
        db_path=db_path,
    )
    assert row["status"] == incidents_repo.STATUS_AUTHORING
    assert row["confidence"] == pytest.approx(0.82)
    desired = json.loads(row["desired_state"])
    assert desired["author"]["branch"] == "feat/step6-fix-123"
    assert desired["author"]["mr_iid"] == 99


def test_record_author_idempotent_on_already_authoring(tmp_path) -> None:
    from agt_equities import author_critic, incidents_repo

    db_path = _init_db(tmp_path)
    iid = _seed(db_path, status="open")
    # First call.
    author_critic.record_author_outcome(
        iid, author_confidence=0.5, branch="b1", mr_iid=1, db_path=db_path,
    )
    # Second call updates confidence + branch without an illegal-transition
    # raise.
    row = author_critic.record_author_outcome(
        iid, author_confidence=0.7, branch="b2", mr_iid=2, db_path=db_path,
    )
    assert row["status"] == incidents_repo.STATUS_AUTHORING
    assert row["confidence"] == pytest.approx(0.7)
    desired = json.loads(row["desired_state"])
    assert desired["author"]["branch"] == "b2"
    assert desired["author"]["mr_iid"] == 2


def test_record_author_rejects_bad_inputs(tmp_path) -> None:
    from agt_equities import author_critic

    db_path = _init_db(tmp_path)
    iid = _seed(db_path)

    with pytest.raises(ValueError):
        author_critic.record_author_outcome(
            iid, author_confidence=1.01, branch="b", mr_iid=1, db_path=db_path,
        )
    with pytest.raises(ValueError):
        author_critic.record_author_outcome(
            iid, author_confidence=0.5, branch="", mr_iid=1, db_path=db_path,
        )
    with pytest.raises(ValueError):
        author_critic.record_author_outcome(
            9999, author_confidence=0.5, branch="b", mr_iid=1, db_path=db_path,
        )


# ---------------------------------------------------------------------------
# record_critic_outcome ladder
# ---------------------------------------------------------------------------

def _mk_mech(**overrides):
    from agt_equities.author_critic import MechanicalCriticResult

    defaults = dict(
        passed=True,
        failed_checks=[],
        evidence={
            "path_whitelist": {"passed": True, "protected_hits": []},
            "invariant_still_present": {"still_present": True},
            "pytest": {"skipped": True},
        },
        computed_confidence=0.9,
        needs_fixup=False,
        needs_architect_reason=None,
    )
    defaults.update(overrides)
    return MechanicalCriticResult(**defaults)


def test_critic_dispatch_architect_on_mech_block(tmp_path) -> None:
    from agt_equities import author_critic, incidents_repo

    db_path = _init_db(tmp_path)
    iid = _seed(db_path)
    author_critic.record_author_outcome(
        iid, author_confidence=0.5, branch="b", mr_iid=10, db_path=db_path,
    )
    mech = _mk_mech(
        passed=False,
        failed_checks=["path_whitelist"],
        computed_confidence=0.0,
        needs_architect_reason="hit walker.py",
    )
    row = author_critic.record_critic_outcome(
        iid, mech=mech, mr_iid=10, branch="b", db_path=db_path,
    )
    assert row["status"] == incidents_repo.STATUS_ARCHITECT
    assert row["confidence"] == pytest.approx(0.0)


def test_critic_dispatch_fixup_uses_append_not_strike(tmp_path) -> None:
    from agt_equities import author_critic, incidents_repo

    db_path = _init_db(tmp_path)
    iid = _seed(db_path)
    author_critic.record_author_outcome(
        iid, author_confidence=0.9, branch="b", mr_iid=10, db_path=db_path,
    )
    mech = _mk_mech(
        passed=False,
        failed_checks=["pytest"],
        computed_confidence=0.3,
        needs_fixup=True,
        evidence={
            "path_whitelist": {"passed": True, "protected_hits": []},
            "invariant_still_present": {"still_present": True},
            "pytest": {"returncode": 1, "tail": "1 failed"},
        },
    )
    row = author_critic.record_critic_outcome(
        iid, mech=mech, mr_iid=10, branch="b", db_path=db_path,
    )
    # No strike consumed — status should stay authoring.
    assert row["status"] == incidents_repo.STATUS_AUTHORING
    history = json.loads(row["rejection_history"])
    assert history[-1]["internal"] is True
    assert "pytest failed" in history[-1]["reason"]


def test_critic_dispatch_resolve_when_invariant_cleared(tmp_path) -> None:
    from agt_equities import author_critic, incidents_repo

    db_path = _init_db(tmp_path)
    iid = _seed(db_path)
    author_critic.record_author_outcome(
        iid, author_confidence=0.8, branch="b", mr_iid=10, db_path=db_path,
    )
    mech = _mk_mech(
        evidence={
            "path_whitelist": {"passed": True, "protected_hits": []},
            "invariant_still_present": {"still_present": False},
            "pytest": {"skipped": True},
        },
    )
    row = author_critic.record_critic_outcome(
        iid, mech=mech, mr_iid=10, branch="b", db_path=db_path,
    )
    assert row["status"] == incidents_repo.STATUS_RESOLVED
    assert row["closed_at"]


def test_critic_dispatch_llm_low_confidence_forces_architect(tmp_path) -> None:
    from agt_equities import author_critic, incidents_repo

    db_path = _init_db(tmp_path)
    iid = _seed(db_path, scrutiny_tier="high")
    author_critic.record_author_outcome(
        iid, author_confidence=0.4, branch="b", mr_iid=10, db_path=db_path,
    )
    mech = _mk_mech(computed_confidence=0.4)
    row = author_critic.record_critic_outcome(
        iid,
        mech=mech,
        llm_critic={"confidence": 0.2, "reason": "logic error suspected"},
        mr_iid=10,
        branch="b",
        db_path=db_path,
    )
    assert row["status"] == incidents_repo.STATUS_ARCHITECT
    assert row["confidence"] == pytest.approx(0.2)


def test_critic_dispatch_awaiting_approval_on_clean_pass(tmp_path) -> None:
    from agt_equities import author_critic, incidents_repo

    db_path = _init_db(tmp_path)
    iid = _seed(db_path)
    author_critic.record_author_outcome(
        iid, author_confidence=0.85, branch="b", mr_iid=10, db_path=db_path,
    )
    mech = _mk_mech(computed_confidence=0.85)
    row = author_critic.record_critic_outcome(
        iid, mech=mech, mr_iid=10, branch="b", db_path=db_path,
    )
    assert row["status"] == incidents_repo.STATUS_AWAITING
    assert row["mr_iid"] == 10
    assert row["confidence"] == pytest.approx(0.85)


def test_critic_dispatch_missing_mr_iid_rejects(tmp_path) -> None:
    from agt_equities import author_critic, incidents_repo

    db_path = _init_db(tmp_path)
    iid = _seed(db_path)
    author_critic.record_author_outcome(
        iid, author_confidence=0.8, branch="b", mr_iid=10, db_path=db_path,
    )
    mech = _mk_mech()
    row = author_critic.record_critic_outcome(
        iid, mech=mech, mr_iid=None, branch="b", db_path=db_path,
    )
    assert row["status"] == incidents_repo.STATUS_REJECTED_ONCE


def test_critic_dispatch_generic_reject_consumes_strike(tmp_path) -> None:
    from agt_equities import author_critic, incidents_repo

    db_path = _init_db(tmp_path)
    iid = _seed(db_path)
    author_critic.record_author_outcome(
        iid, author_confidence=0.5, branch="b", mr_iid=10, db_path=db_path,
    )
    mech = _mk_mech(
        passed=False,
        failed_checks=["some_future_check"],
        computed_confidence=0.5,
    )
    row = author_critic.record_critic_outcome(
        iid, mech=mech, mr_iid=10, branch="b", db_path=db_path,
    )
    assert row["status"] == incidents_repo.STATUS_REJECTED_ONCE
    history = json.loads(row["rejection_history"])
    assert any(
        "Mechanical Critic rejected" in e["reason"] for e in history
    )


def test_critic_dispatch_confidence_is_min_of_mech_and_llm(tmp_path) -> None:
    from agt_equities import author_critic

    db_path = _init_db(tmp_path)
    iid = _seed(db_path, scrutiny_tier="high")
    author_critic.record_author_outcome(
        iid, author_confidence=0.8, branch="b", mr_iid=10, db_path=db_path,
    )
    mech = _mk_mech(computed_confidence=0.8)
    row = author_critic.record_critic_outcome(
        iid,
        mech=mech,
        llm_critic={"confidence": 0.55, "reason": "subtle"},
        mr_iid=10,
        branch="b",
        db_path=db_path,
    )
    assert row["confidence"] == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# CLI shim (scripts/author_critic.py)
# ---------------------------------------------------------------------------

def test_cli_record_author_emits_row(tmp_path, capsys) -> None:
    # Import via path so the CLI module is exercised under the live
    # Python interpreter — no subprocess needed.
    import importlib.util

    db_path = _init_db(tmp_path)
    iid = _seed(db_path)

    repo_root = Path(__file__).resolve().parent.parent
    cli_path = repo_root / "scripts" / "author_critic.py"
    spec = importlib.util.spec_from_file_location("cli_mod", cli_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    rc = mod.main([
        "--db-path", db_path,
        "record-author",
        "--incident-id", str(iid),
        "--author-confidence", "0.75",
        "--branch", "feat/test",
        "--mr", "55",
    ])
    assert rc == 0
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["incident"]["status"] == "authoring"
    assert payload["incident"]["confidence"] == pytest.approx(0.75)


def test_cli_critique_emits_verdict(tmp_path, capsys, monkeypatch) -> None:
    import importlib.util
    from agt_equities import author_critic

    db_path = _init_db(tmp_path)
    iid = _seed(db_path)

    # No pytest subprocess spawned thanks to --skip-pytest.
    repo_root = Path(__file__).resolve().parent.parent
    cli_path = repo_root / "scripts" / "author_critic.py"
    spec = importlib.util.spec_from_file_location("cli_mod", cli_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    rc = mod.main([
        "--db-path", db_path,
        "critique",
        "--incident-id", str(iid),
        "--author-confidence", "0.8",
        "--changed-paths", "agt_equities/safe.py",
        "--skip-pytest",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mech"]["passed"] is True
    assert payload["action"] == "awaiting_approval"


def test_cli_critique_unknown_incident_returns_2(
    tmp_path, capsys,
) -> None:
    import importlib.util

    db_path = _init_db(tmp_path)
    repo_root = Path(__file__).resolve().parent.parent
    cli_path = repo_root / "scripts" / "author_critic.py"
    spec = importlib.util.spec_from_file_location("cli_mod", cli_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    rc = mod.main([
        "--db-path", db_path,
        "critique",
        "--incident-id", "9999",
        "--author-confidence", "0.5",
        "--skip-pytest",
    ])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert "error" in payload


def test_cli_record_critic_roundtrip(tmp_path, capsys) -> None:
    import importlib.util
    from agt_equities.author_critic import MechanicalCriticResult

    db_path = _init_db(tmp_path)
    iid = _seed(db_path)

    # Seed authoring state.
    repo_root = Path(__file__).resolve().parent.parent
    cli_path = repo_root / "scripts" / "author_critic.py"
    spec = importlib.util.spec_from_file_location("cli_mod", cli_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    mod.main([
        "--db-path", db_path,
        "record-author",
        "--incident-id", str(iid),
        "--author-confidence", "0.7",
        "--branch", "feat/x",
        "--mr", "12",
    ])
    capsys.readouterr()

    mech = MechanicalCriticResult(
        passed=True, failed_checks=[],
        evidence={
            "path_whitelist": {"passed": True, "protected_hits": []},
            "invariant_still_present": {"still_present": True},
            "pytest": {"skipped": True},
        },
        computed_confidence=0.7,
    )
    mech_file = tmp_path / "mech.json"
    mech_file.write_text(mech.to_json(), encoding="utf-8")

    rc = mod.main([
        "--db-path", db_path,
        "record-critic",
        "--incident-id", str(iid),
        "--mech-file", str(mech_file),
        "--mr", "12",
        "--branch", "feat/x",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["incident"]["status"] == "awaiting_approval"
    assert payload["incident"]["mr_iid"] == 12
