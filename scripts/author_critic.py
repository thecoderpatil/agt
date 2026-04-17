#!/usr/bin/env python3
"""CLI shim for ADR-007 Step 6 Author/Critic pipeline.

Invoked by the ``agt-remediation-weekly`` scheduled task — the task
prompt does the LLM authoring, this CLI does the mechanical Critic +
DB write-back so the task can stay bash-friendly.

Subcommands:

    record-author   --incident-id N --branch B --mr IID \
                    --author-confidence F
        Transition the incident to ``authoring``, stash branch/MR metadata,
        write ``author_confidence`` into ``incidents.confidence``.

    critique        --incident-id N --author-confidence F \
                    [--mr IID] [--changed-paths p1,p2] \
                    [--workdir DIR] [--skip-pytest]
        Run the mechanical Critic and print a JSON verdict + the suggested
        next action. Does NOT mutate DB state — call ``record-critic`` to
        persist the decision.

    record-critic   --incident-id N --mech-file PATH \
                    [--llm-confidence F] [--llm-reason TXT] \
                    [--mr IID] [--branch B]
        Persist the Critic's dispatch decision (awaiting_approval,
        rejected, needs_architect, resolved, or internal-fixup).

Every command supports ``--db-path`` or ``AGT_DB_PATH`` for DB override.
Output is JSON on stdout; non-zero exit codes indicate a bad invocation
(unknown incident id, bad args).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agt_equities import author_critic, incidents_repo  # noqa: E402


def _emit(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, default=str, indent=2, sort_keys=True))
    sys.stdout.write("\n")


def _db_from_args(ns: argparse.Namespace) -> str | None:
    return getattr(ns, "db_path", None) or os.environ.get("AGT_DB_PATH")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def cmd_record_author(ns: argparse.Namespace) -> int:
    row = author_critic.record_author_outcome(
        int(ns.incident_id),
        author_confidence=float(ns.author_confidence),
        branch=ns.branch,
        mr_iid=int(ns.mr),
        db_path=_db_from_args(ns),
    )
    _emit({"incident": row})
    return 0


def cmd_critique(ns: argparse.Namespace) -> int:
    db_path = _db_from_args(ns)
    incident = incidents_repo.get(int(ns.incident_id), db_path=db_path)
    if incident is None:
        _emit({"error": f"unknown incident id {ns.incident_id}"})
        return 2
    changed: list[str] | None = None
    if ns.changed_paths:
        changed = [p.strip() for p in ns.changed_paths.split(",") if p.strip()]
    mech = author_critic.run_mechanical_critic(
        incident=incident,
        author_confidence=float(ns.author_confidence),
        changed_paths=changed,
        mr_iid=int(ns.mr) if ns.mr is not None else None,
        workdir=ns.workdir,
        db_path=db_path,
        skip_pytest=bool(ns.skip_pytest),
    )
    needs_llm = author_critic.should_escalate_to_llm_critic(
        incident=incident,
        author_confidence=float(ns.author_confidence),
        mech=mech,
    )
    action = _derive_action(mech, needs_llm=needs_llm)
    _emit({
        "incident_id": int(ns.incident_id),
        "mech": json.loads(mech.to_json()),
        "needs_llm_critic": needs_llm,
        "action": action,
    })
    return 0


def cmd_record_critic(ns: argparse.Namespace) -> int:
    mech_blob = Path(ns.mech_file).read_text(encoding="utf-8")
    mech = author_critic.MechanicalCriticResult.from_json(mech_blob)
    llm: dict[str, Any] | None = None
    if ns.llm_confidence is not None or ns.llm_reason:
        llm = {
            "confidence": (
                float(ns.llm_confidence) if ns.llm_confidence is not None else 0.0
            ),
            "reason": ns.llm_reason or "",
        }
    row = author_critic.record_critic_outcome(
        int(ns.incident_id),
        mech=mech,
        llm_critic=llm,
        mr_iid=int(ns.mr) if ns.mr is not None else None,
        branch=ns.branch,
        db_path=_db_from_args(ns),
    )
    _emit({"incident": row})
    return 0


# ---------------------------------------------------------------------------
# Action derivation
# ---------------------------------------------------------------------------

def _derive_action(
    mech: author_critic.MechanicalCriticResult,
    *,
    needs_llm: bool,
) -> str:
    if mech.needs_architect_reason:
        return "needs_architect"
    if mech.needs_fixup:
        return "needs_fixup"
    inv = (mech.evidence.get("invariant_still_present") or {})
    still_present = bool(inv.get("still_present", True))
    if mech.passed and not still_present:
        return "resolve"
    if needs_llm:
        return "needs_llm_critic"
    if mech.passed:
        return "awaiting_approval"
    return "reject"


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="author_critic")
    p.add_argument("--db-path", default=None, dest="db_path")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser(
        "record-author",
        help="record Author's MR + confidence, flip to authoring",
    )
    a.add_argument(
        "--incident-id", type=int, required=True, dest="incident_id",
    )
    a.add_argument(
        "--author-confidence", type=float, required=True,
        dest="author_confidence",
    )
    a.add_argument("--branch", required=True)
    a.add_argument("--mr", type=int, required=True)

    c = sub.add_parser(
        "critique",
        help="run mechanical Critic, emit JSON verdict + action",
    )
    c.add_argument(
        "--incident-id", type=int, required=True, dest="incident_id",
    )
    c.add_argument(
        "--author-confidence", type=float, required=True,
        dest="author_confidence",
    )
    c.add_argument("--mr", type=int, default=None)
    c.add_argument(
        "--changed-paths", default=None, dest="changed_paths",
        help="Comma-separated paths; if omitted and --mr is set, "
             "fetched from GitLab.",
    )
    c.add_argument(
        "--workdir", default=None,
        help="Feature branch checkout for pytest. Omit to skip pytest.",
    )
    c.add_argument(
        "--skip-pytest", action="store_true", dest="skip_pytest",
    )

    r = sub.add_parser(
        "record-critic",
        help="persist Critic dispatch (awaiting/rejected/resolve/needs_architect)",
    )
    r.add_argument(
        "--incident-id", type=int, required=True, dest="incident_id",
    )
    r.add_argument(
        "--mech-file", required=True, dest="mech_file",
        help="Path to serialized MechanicalCriticResult JSON.",
    )
    r.add_argument(
        "--llm-confidence", type=float, default=None, dest="llm_confidence",
    )
    r.add_argument("--llm-reason", default=None, dest="llm_reason")
    r.add_argument("--mr", type=int, default=None)
    r.add_argument("--branch", default=None)
    return p


_HANDLERS = {
    "record-author": cmd_record_author,
    "critique": cmd_critique,
    "record-critic": cmd_record_critic,
}


def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    ns = p.parse_args(argv)
    handler = _HANDLERS.get(ns.cmd)
    if handler is None:
        p.error(f"unknown command: {ns.cmd}")
        return 2
    return int(handler(ns) or 0)


if __name__ == "__main__":
    sys.exit(main())
