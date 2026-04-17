"""
Pre-commit LOC-delta gate for AGT dispatches.

Blocks `POST /repository/commits` if the staged /tmp/ files diverge from
the expectation block declared in the dispatch markdown. Catches silent
truncation (f4def9f-class regressions) before the patch ships.

Usage:
  python scripts/precommit_loc_gate.py \
    --dispatch reports/<topic>_dispatch_<date>.md \
    --staged /tmp/csp_harvest.py:agt_equities/csp_harvest.py,\
             /tmp/test_csp_harvest.py:tests/test_csp_harvest.py

Exits 0 on pass, nonzero on fail. Fail reason printed to stderr.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # runtime dep — already on prod venv per MR !83 cascade


EXPECTED_DELTA_FENCE_RE = re.compile(
    r"```yaml\s+expected_delta\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class FileExpectation:
    path: str
    added: int
    removed: int
    net: int
    tolerance: int = 10
    required_symbols: list[str] = field(default_factory=list)
    required_sentinels: list[str] = field(default_factory=list)


@dataclass
class ShrinkingClause:
    file: str
    reason: str
    expected_net: int


@dataclass
class Expectation:
    files: dict[str, FileExpectation]
    shrinking: dict[str, ShrinkingClause] = field(default_factory=dict)


class GateError(Exception):
    """Raised when the gate rejects the staged diff."""


def parse_dispatch_expectation(dispatch_path: Path) -> Expectation:
    """Parse the expected_delta YAML block from dispatch markdown."""
    if not dispatch_path.exists():
        raise GateError(f"dispatch file not found: {dispatch_path}")

    text = dispatch_path.read_text(encoding="utf-8")
    m = EXPECTED_DELTA_FENCE_RE.search(text)
    if not m:
        raise GateError(
            f"dispatch missing required ```yaml expected_delta ... ``` block"
        )
    raw = m.group(1)
    try:
        data: dict[str, Any] = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise GateError(f"expected_delta YAML parse failed: {e}")

    if "files" not in data or not isinstance(data["files"], dict):
        raise GateError("expected_delta block missing `files:` mapping")

    files: dict[str, FileExpectation] = {}
    for path, spec in data["files"].items():
        if not isinstance(spec, dict):
            raise GateError(f"file spec for {path} not a mapping")
        files[path] = FileExpectation(
            path=path,
            added=int(spec.get("added", 0)),
            removed=int(spec.get("removed", 0)),
            net=int(spec.get("net", 0)),
            tolerance=int(spec.get("tolerance", 10)),
            required_symbols=list(spec.get("required_symbols", []) or []),
            required_sentinels=list(spec.get("required_sentinels", []) or []),
        )

    shrinking: dict[str, ShrinkingClause] = {}
    for entry in data.get("shrinking", []) or []:
        if not isinstance(entry, dict) or "file" not in entry:
            raise GateError(f"shrinking entry malformed: {entry!r}")
        shrinking[entry["file"]] = ShrinkingClause(
            file=entry["file"],
            reason=str(entry.get("reason", "")),
            expected_net=int(entry.get("expected_net", 0)),
        )

    return Expectation(files=files, shrinking=shrinking)


def diff_stats(old_text: str, new_text: str) -> tuple[int, int]:
    """Return (added, removed) line counts between old and new."""
    old_lines = old_text.splitlines(keepends=False)
    new_lines = new_text.splitlines(keepends=False)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            added += j2 - j1
            removed += i2 - i1
    return added, removed


def collect_top_level_symbols(source: str) -> set[str]:
    """AST walk — return set of top-level def/class names."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
    return names


def evaluate_file(
    exp: FileExpectation,
    old_text: str,
    new_text: str,
    shrinking: ShrinkingClause | None,
) -> list[str]:
    """Return list of failure messages for this file (empty = pass)."""
    failures: list[str] = []
    added, removed = diff_stats(old_text, new_text)
    actual_net = added - removed

    within_tol = abs(actual_net - exp.net) <= exp.tolerance
    undeclared_shrink = actual_net < (exp.net - exp.tolerance)

    if undeclared_shrink and shrinking is None:
        failures.append(
            f"[{exp.path}] undeclared shrinkage: actual_net={actual_net}, "
            f"declared_net={exp.net}, tolerance={exp.tolerance}. "
            "Add a `shrinking:` clause to the dispatch if intentional."
        )
    elif not within_tol and shrinking is None:
        failures.append(
            f"[{exp.path}] delta divergence: actual_net={actual_net}, "
            f"declared_net={exp.net}, tolerance={exp.tolerance} "
            f"(added={added}, removed={removed})"
        )
    elif shrinking is not None:
        # Shrinking clause present — enforce declared expected_net
        if abs(actual_net - shrinking.expected_net) > exp.tolerance:
            failures.append(
                f"[{exp.path}] shrinking clause mismatch: actual_net={actual_net}, "
                f"clause.expected_net={shrinking.expected_net}, "
                f"tolerance={exp.tolerance}"
            )

    # Required symbols
    if exp.path.endswith(".py"):
        symbols = collect_top_level_symbols(new_text)
        missing = [s for s in exp.required_symbols if s not in symbols]
        if missing:
            failures.append(
                f"[{exp.path}] required symbols missing from AST: {missing}"
            )

    # Required sentinels
    missing_sent = [s for s in exp.required_sentinels if s not in new_text]
    if missing_sent:
        failures.append(
            f"[{exp.path}] required sentinels not found in file: {missing_sent}"
        )

    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dispatch", required=True, type=Path)
    ap.add_argument(
        "--staged",
        required=True,
        help="Comma-separated list of staged-path:repo-path pairs. "
             "E.g., /tmp/foo.py:agt_equities/foo.py,/tmp/bar.py:tests/bar.py",
    )
    ap.add_argument(
        "--origin-cache",
        default="/tmp/_gate_origin_cache",
        help="Dir where origin/main raw bytes are pre-fetched per file. "
             "Coder pre-populates this during API commit flow step 2.",
    )
    args = ap.parse_args()

    try:
        exp = parse_dispatch_expectation(args.dispatch)
    except GateError as e:
        print(f"GATE REJECT: {e}", file=sys.stderr)
        return 2

    origin_cache = Path(args.origin_cache)
    staged_pairs: list[tuple[Path, str]] = []
    for tok in args.staged.split(","):
        tok = tok.strip()
        if ":" not in tok:
            print(f"GATE REJECT: staged token malformed: {tok!r}", file=sys.stderr)
            return 2
        staged_path_str, repo_path = tok.split(":", 1)
        staged_pairs.append((Path(staged_path_str.strip()), repo_path.strip()))

    declared_paths = set(exp.files.keys())
    staged_paths = {p for _, p in staged_pairs}

    extra_staged = staged_paths - declared_paths
    missing_staged = declared_paths - staged_paths
    failures: list[str] = []
    if extra_staged:
        failures.append(
            f"GATE REJECT: staged files not declared in dispatch: {sorted(extra_staged)}"
        )
    if missing_staged:
        failures.append(
            f"GATE REJECT: dispatch declared files not staged: {sorted(missing_staged)}"
        )

    for staged_path, repo_path in staged_pairs:
        if repo_path not in exp.files:
            continue  # already flagged above
        file_exp = exp.files[repo_path]
        if not staged_path.exists():
            failures.append(f"GATE REJECT: staged file missing: {staged_path}")
            continue
        new_text = staged_path.read_text(encoding="utf-8")
        origin_blob = origin_cache / repo_path.replace("/", "__")
        old_text = origin_blob.read_text(encoding="utf-8") if origin_blob.exists() else ""
        shrinking = exp.shrinking.get(repo_path)
        failures.extend(evaluate_file(file_exp, old_text, new_text, shrinking))

    if failures:
        print("GATE REJECT:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 3

    print("GATE PASS: all expectations satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
