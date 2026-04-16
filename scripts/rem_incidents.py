#!/usr/bin/env python3
"""CLI wrapper over agt_equities.remediation.

Invoked by the agt-remediation-weekly scheduled task via bash. The task
prompt does most of the judgment (which fix to write) — this CLI just
handles DB reads/writes + directive parsing so the task can use simple
bash commands instead of embedding Python.

Subcommands:
    extract <directive-path>            — JSON list of incidents from directive
    state <incident-id>                 — JSON of current row (or null)
    awaiting                            — JSON list of rows in awaiting_approval
    register <id> --source <path>       — insert new row (idempotent)
    mark-awaiting <id> --mr <iid> --branch <name>
    approve <id>
    reject <id> --reason <text>
    mark-architect <id> --reason <text>
    nudge <id>

All writes print the resulting row (or {}) as JSON on stdout.

Repo root for DB is inferred from this file's location unless overridden
by --db-path. Run from anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running the CLI from outside the repo without setting PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agt_equities import remediation  # noqa: E402


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=str))
    sys.stdout.write("\n")


def _db_path_from_args(ns: argparse.Namespace) -> str | None:
    return getattr(ns, "db_path", None) or os.environ.get("AGT_DB_PATH")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rem_incidents")
    p.add_argument("--db-path", default=None,
                   help="Override DB path (defaults to agt_desk.db).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_extract = sub.add_parser("extract", help="parse directive -> incidents")
    s_extract.add_argument("directive", help="path to directive markdown")

    s_state = sub.add_parser("state", help="get one incident row")
    s_state.add_argument("incident_id")

    sub.add_parser("awaiting", help="list rows awaiting approval")

    s_register = sub.add_parser("register", help="insert (idempotent)")
    s_register.add_argument("incident_id")
    s_register.add_argument("--source", required=True,
                            help="directive source (path or commit sha)")

    s_mark = sub.add_parser("mark-awaiting", help="MR opened — flip state")
    s_mark.add_argument("incident_id")
    s_mark.add_argument("--mr", type=int, required=True)
    s_mark.add_argument("--branch", required=True)

    s_approve = sub.add_parser("approve", help="mark merged")
    s_approve.add_argument("incident_id")

    s_reject = sub.add_parser("reject", help="advance rejection state machine")
    s_reject.add_argument("incident_id")
    s_reject.add_argument("--reason", required=True)

    s_arch = sub.add_parser("mark-architect", help="escalate to Architect")
    s_arch.add_argument("incident_id")
    s_arch.add_argument("--reason", required=True)

    s_nudge = sub.add_parser("nudge", help="record a re-nudge")
    s_nudge.add_argument("incident_id")

    ns = p.parse_args(argv)
    db_path = _db_path_from_args(ns)

    if ns.cmd == "extract":
        _emit(remediation.extract_incidents_from_directive(ns.directive))
        return 0

    if ns.cmd == "state":
        _emit(remediation.get_state(ns.incident_id, db_path=db_path))
        return 0

    if ns.cmd == "awaiting":
        _emit(remediation.list_awaiting(db_path=db_path))
        return 0

    if ns.cmd == "register":
        row = remediation.register_incident(
            ns.incident_id, directive_source=ns.source, db_path=db_path,
        )
        _emit(row)
        return 0

    if ns.cmd == "mark-awaiting":
        remediation.mark_awaiting(
            ns.incident_id, mr_iid=ns.mr, branch_name=ns.branch, db_path=db_path,
        )
        _emit(remediation.get_state(ns.incident_id, db_path=db_path))
        return 0

    if ns.cmd == "approve":
        remediation.mark_merged(ns.incident_id, db_path=db_path)
        _emit(remediation.get_state(ns.incident_id, db_path=db_path))
        return 0

    if ns.cmd == "reject":
        _emit(remediation.mark_rejected(
            ns.incident_id, ns.reason, db_path=db_path,
        ))
        return 0

    if ns.cmd == "mark-architect":
        remediation.mark_architect(
            ns.incident_id, ns.reason, db_path=db_path,
        )
        _emit(remediation.get_state(ns.incident_id, db_path=db_path))
        return 0

    if ns.cmd == "nudge":
        remediation.record_nudge(ns.incident_id, db_path=db_path)
        _emit(remediation.get_state(ns.incident_id, db_path=db_path))
        return 0

    p.error(f"unknown command: {ns.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
