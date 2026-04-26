"""Sprint 13: loc_gate hardening + operator_interventions CHECK + heartbeat retention."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


def test_loc_gate_annassign_handled():
    import importlib.util, sys
    gate_path = Path(__file__).parent.parent / "scripts" / "precommit_loc_gate.py"
    source = gate_path.read_text(encoding="utf-8")
    assert "ast.AnnAssign" in source
    spec = importlib.util.spec_from_file_location("precommit_loc_gate", gate_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("precommit_loc_gate", mod)
    spec.loader.exec_module(mod)
    assert "VALID_KINDS" in mod.collect_top_level_symbols(
        "VALID_KINDS: frozenset = frozenset({'a'})"
    )
    assert "x" in mod.collect_top_level_symbols("x: int = 5")


def test_loc_gate_id_strings_key():
    import importlib.util, sys
    gate_path = Path(__file__).parent.parent / "scripts" / "precommit_loc_gate.py"
    source = gate_path.read_text(encoding="utf-8")
    assert source.count("id_strings") >= 3
    spec = importlib.util.spec_from_file_location("precommit_loc_gate", gate_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("precommit_loc_gate", mod)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        dispatch_md = Path(td) / "d.md"
        dispatch_md.write_text(
            "# T\n```yaml expected_delta\nfiles:\n  foo.py:\n    added: 1\n"
            "    removed: 0\n    net: 1\n    tolerance: 5\n"
            "    id_strings: [\"foo_job\"]\n```\n", encoding="utf-8"
        )
        file_exp = mod.parse_dispatch_expectation(dispatch_md).files["foo.py"]
        assert file_exp.id_strings == ["foo_job"]


def test_operator_interventions_migration_idempotent(tmp_path, capsys):
    script_path = Path(__file__).parent.parent / "scripts" / "migrate_operator_interventions_kind_check.py"
    source = script_path.read_text(encoding="utf-8")
    for s in ("CHECK", "SKIP", "tx_immediate", "VACUUM INTO", "integrity_check",
              "idx_oi_occurred", "idx_oi_kind_occurred", "idx_oi_target"):
        assert s in source, f"{s!r} missing from migration script"
    db_path = tmp_path / "oi.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE operator_interventions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "occurred_at_utc TEXT NOT NULL, operator_user_id TEXT, kind TEXT NOT NULL, "
                 "target_table TEXT, target_id INTEGER, before_state TEXT, after_state TEXT, "
                 "reason TEXT, notes TEXT)")
    conn.commit(); conn.close()
    import importlib.util
    spec = importlib.util.spec_from_file_location("migrate_oi", script_path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.run(db_path=db_path, dry_run=False)
    assert "DONE" in capsys.readouterr().out
    mod.run(db_path=db_path, dry_run=False)
    assert "SKIP" in capsys.readouterr().out


def test_heartbeat_archive_job_registered():
    source = (Path(__file__).parent.parent / "agt_scheduler.py").read_text(encoding="utf-8")
    assert '"heartbeat_archive"' in source or "'heartbeat_archive'" in source
    assert "daemon_heartbeat_samples_archive" in source
    assert 'day_of_week="sun"' in source or "day_of_week='sun'" in source
    assert 'registered.append("heartbeat_archive")' in source or \
           "registered.append('heartbeat_archive')" in source
