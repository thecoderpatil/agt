"""CLI entry point for the ADR-007 safety invariant suite.

Usage:
    python scripts/check_invariants.py                       # default paths
    python scripts/check_invariants.py --yaml path/to.yaml
    python scripts/check_invariants.py --db path/to/db.sqlite --verbose
    python scripts/check_invariants.py --json                # machine-readable

Exit code: number of invariants with at least one violation (0 = all clean).
"""
from __future__ import annotations

import argparse
import json as json_mod
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agt_equities.invariants.runner import run_all  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run ADR-007 safety invariants against agt_desk.db"
    )
    ap.add_argument("--yaml", default=None, help="Path to safety_invariants.yaml")
    ap.add_argument("--db", default=None, help="Path to agt_desk.db")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON to stdout")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print every violation and all evidence")
    ap.add_argument(
        "--allow-override-db-path",
        action="store_true",
        help=(
            "Bypass the canonical DB path assertion (for dev clones). "
            "Production CLI invocations MUST omit this flag."
        ),
    )
    args = ap.parse_args()

    from agt_equities.invariants.bootstrap import assert_canonical_db_path
    from agt_equities import db as agt_db
    target_db_path = args.db or str(agt_db.DB_PATH)
    assert_canonical_db_path(
        resolved_path=target_db_path,
        allow_override=args.allow_override_db_path,
    )

    kwargs = {}
    if args.yaml:
        kwargs["yaml_path"] = args.yaml
    if args.db:
        kwargs["db_path"] = args.db

    try:
        results = run_all(**kwargs)
    except Exception as exc:
        sys.stderr.write(f"FATAL: runner failed: {exc}\n")
        return 127

    if args.json:
        out = {
            inv_id: [
                {**asdict(v), "detected_at": v.detected_at.isoformat()}
                for v in vios
            ]
            for inv_id, vios in results.items()
        }
        print(json_mod.dumps(out, indent=2, default=str))
        return sum(1 for v in results.values() if v)

    print("=" * 70)
    print("AGT SAFETY INVARIANTS")
    print("=" * 70)
    fail_count = 0
    for inv_id, vios in results.items():
        if not vios:
            print(f"  [OK]   {inv_id}")
            continue
        fail_count += 1
        print(f"  [FAIL] {inv_id}  ({len(vios)} violation(s))")
        shown = vios if args.verbose else vios[:3]
        for v in shown:
            print(f"         - [{v.severity}] {v.description}")
            if args.verbose and v.evidence:
                for k, val in v.evidence.items():
                    print(f"             {k}: {val}")
        if not args.verbose and len(vios) > 3:
            print(f"         ... and {len(vios) - 3} more (pass -v to see all)")
    print("-" * 70)
    print(f"  {fail_count}/{len(results)} invariants failing")
    return fail_count


if __name__ == "__main__":
    sys.exit(main())
