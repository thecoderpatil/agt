"""Tests for agt_equities.remediation — directive parser + state machine."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from agt_equities import remediation


# ---------------------------------------------------------------------------
# Directive parser
# ---------------------------------------------------------------------------

def test_extract_incidents_parses_canonical_opus_directive(tmp_path: Path) -> None:
    """The canonical 2026-04-16 directive format — 4 critical incidents."""
    directive_text = dedent("""\
        # Weekly Architect Directive

        ## Critical Incidents From Last Review Window (MUST resolve before advancing readiness)

        1. **ORDER_266_LIVE_ACCOUNT** — UBER CC staged on U21971297 (LIVE) on 2026-04-15.
           Paper-mode account filter must be audited for every write path.
        2. **ORDER_310_BELOW_BASIS** — PYPL CC $48 on DUP751005, paper_basis=$48.94.
           CC picker must hard-fail when strike < paper_basis.
        3. **ORPHAN_CHILDREN** — 3 child records stuck status='sent'.
        4. **CAPTURE_RECONCILIATION_BROKEN** — ACCOUNT_LABELS missing from config.

        ## Current Focus
        """)
    p = tmp_path / "directive.md"
    p.write_text(directive_text, encoding="utf-8")

    incidents = remediation.extract_incidents_from_directive(p)
    assert len(incidents) == 4
    ids = [i["incident_id"] for i in incidents]
    assert ids == [
        "ORDER_266_LIVE_ACCOUNT",
        "ORDER_310_BELOW_BASIS",
        "ORPHAN_CHILDREN",
        "CAPTURE_RECONCILIATION_BROKEN",
    ]
    # Summary = first line of the item body
    assert "UBER CC staged on U21971297" in incidents[0]["summary"]
    assert "PYPL CC" in incidents[1]["summary"]


def test_extract_incidents_returns_empty_when_no_section(tmp_path: Path) -> None:
    p = tmp_path / "directive.md"
    p.write_text("# Some Other Doc\n\nNo incidents section here.\n", encoding="utf-8")
    assert remediation.extract_incidents_from_directive(p) == []


def test_extract_incidents_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert remediation.extract_incidents_from_directive(tmp_path / "nope.md") == []


# ---------------------------------------------------------------------------
# DB state machine — use a temp DB so we don't touch agt_desk.db
# ---------------------------------------------------------------------------

def _init_temp_db(tmp_path: Path) -> str:
    """Create a throwaway DB with just the remediation_incidents table."""
    db = tmp_path / "rem_test.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE remediation_incidents (
            incident_id         TEXT PRIMARY KEY,
            first_detected      TEXT NOT NULL,
            directive_source    TEXT,
            fix_authored_at     TEXT,
            mr_iid              INTEGER,
            branch_name         TEXT,
            status              TEXT NOT NULL DEFAULT 'new',
            rejection_reasons   TEXT,
            last_nudged_at      TEXT,
            architect_reason    TEXT,
            updated_at          TEXT
        )
    """)
    conn.commit()
    conn.close()
    return str(db)


def test_register_incident_is_idempotent(tmp_path: Path) -> None:
    db = _init_temp_db(tmp_path)
    row1 = remediation.register_incident(
        "FOO_BAR", directive_source="directive-1", db_path=db,
    )
    row2 = remediation.register_incident(
        "FOO_BAR", directive_source="directive-2", db_path=db,
    )
    assert row1["incident_id"] == "FOO_BAR"
    assert row1["status"] == remediation.STATUS_NEW
    # Re-register returns the existing row unchanged.
    assert row2["first_detected"] == row1["first_detected"]
    assert row2["directive_source"] == "directive-1"  # first registration wins


def test_mark_awaiting_flips_state(tmp_path: Path) -> None:
    db = _init_temp_db(tmp_path)
    remediation.register_incident("X", directive_source="d", db_path=db)
    remediation.mark_awaiting("X", mr_iid=42, branch_name="remediation/x", db_path=db)
    row = remediation.get_state("X", db_path=db)
    assert row["status"] == remediation.STATUS_AWAITING
    assert row["mr_iid"] == 42
    assert row["branch_name"] == "remediation/x"
    assert row["fix_authored_at"] is not None


def test_reject_advances_state_machine_three_hops(tmp_path: Path) -> None:
    db = _init_temp_db(tmp_path)
    remediation.register_incident("Y", directive_source="d", db_path=db)
    remediation.mark_awaiting("Y", mr_iid=1, branch_name="remediation/y", db_path=db)

    r1 = remediation.mark_rejected("Y", "wrong approach", db_path=db)
    assert r1["status"] == remediation.STATUS_REJECTED_ONCE

    r2 = remediation.mark_rejected("Y", "still wrong", db_path=db)
    assert r2["status"] == remediation.STATUS_REJECTED_TWICE

    r3 = remediation.mark_rejected("Y", "give up", db_path=db)
    assert r3["status"] == remediation.STATUS_REJECTED_PERM

    # Fourth rejection is a no-op (stays at permanent).
    r4 = remediation.mark_rejected("Y", "already dead", db_path=db)
    assert r4["status"] == remediation.STATUS_REJECTED_PERM

    reasons = json.loads(r4["rejection_reasons"])
    assert len(reasons) == 4
    assert reasons[0]["reason"] == "wrong approach"
    assert reasons[-1]["reason"] == "already dead"


def test_reject_on_unknown_incident_raises(tmp_path: Path) -> None:
    db = _init_temp_db(tmp_path)
    with pytest.raises(ValueError, match="unknown incident"):
        remediation.mark_rejected("DOES_NOT_EXIST", "oops", db_path=db)


def test_merge_transitions_only_from_awaiting(tmp_path: Path) -> None:
    db = _init_temp_db(tmp_path)
    remediation.register_incident("Z", directive_source="d", db_path=db)
    remediation.mark_awaiting("Z", mr_iid=7, branch_name="remediation/z", db_path=db)
    remediation.mark_merged("Z", db_path=db)
    row = remediation.get_state("Z", db_path=db)
    assert row["status"] == remediation.STATUS_MERGED


def test_mark_architect_halts_autonomous_authoring(tmp_path: Path) -> None:
    db = _init_temp_db(tmp_path)
    remediation.register_incident("A", directive_source="d", db_path=db)
    remediation.mark_architect("A", "needs architectural judgment", db_path=db)
    row = remediation.get_state("A", db_path=db)
    assert row["status"] == remediation.STATUS_ARCHITECT
    assert row["architect_reason"] == "needs architectural judgment"


def test_list_awaiting_returns_only_awaiting_rows(tmp_path: Path) -> None:
    db = _init_temp_db(tmp_path)
    for i in range(3):
        remediation.register_incident(f"I{i}", directive_source="d", db_path=db)
    remediation.mark_awaiting("I0", mr_iid=10, branch_name="b0", db_path=db)
    remediation.mark_awaiting("I1", mr_iid=11, branch_name="b1", db_path=db)
    remediation.mark_merged("I1", db_path=db)  # merged — should drop out
    remediation.mark_awaiting("I2", mr_iid=12, branch_name="b2", db_path=db)

    awaiting = remediation.list_awaiting(db_path=db)
    ids = {r["incident_id"] for r in awaiting}
    assert ids == {"I0", "I2"}


def test_record_nudge_updates_timestamp(tmp_path: Path) -> None:
    db = _init_temp_db(tmp_path)
    remediation.register_incident("N", directive_source="d", db_path=db)
    remediation.mark_awaiting("N", mr_iid=1, branch_name="b", db_path=db)
    assert remediation.get_state("N", db_path=db)["last_nudged_at"] is None
    remediation.record_nudge("N", db_path=db)
    assert remediation.get_state("N", db_path=db)["last_nudged_at"] is not None
