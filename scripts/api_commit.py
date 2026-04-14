"""GitLab REST API commit helper for AGT.

Usage (programmatic, from Architect side or a sandbox python -c):

    from scripts.api_commit import commit_files, ensure_branch, open_mr

    commit_files(
        files=[("agt_equities/foo.py", new_text), ("tests/test_foo.py", t_text)],
        branch="sprint-a/a3-flex-sync-atomic",
        message="A3: flex_sync atomic txn",
    )
    open_mr(
        source_branch="sprint-a/a3-flex-sync-atomic",
        title="Sprint A / A3: flex_sync 4-unit atomic transaction",
        description="...",
    )

Guarantees:
* Every .py file in the action list is `ast.parse`-validated BEFORE the POST.
* Refuses to commit if any file fails to parse, listing the offenders.
* Branch is created from `main` if missing.
* All payloads base64-encoded.
* No local git index touched. Linux-sandbox-mount safe.

Token: read from C:\\AGT_Telegram_Bridge\\.gitlab-token (path resolved from
this file's location so it works from either the local sandbox path
``/sessions/.../mnt/AGT_Telegram_Bridge/scripts/api_commit.py`` or
the Windows path ``C:\\AGT_Telegram_Bridge\\scripts\\api_commit.py``).
"""

from __future__ import annotations

import ast
import base64
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Iterable, Sequence

GITLAB_BASE = "https://gitlab.com/api/v4"
PROJECT_ID = "81096827"
DEFAULT_BRANCH = "main"

REPO_ROOT = Path(__file__).resolve().parent.parent  # AGT_Telegram_Bridge
TOKEN_PATH = REPO_ROOT / ".gitlab-token"


# ---------------------------------------------------------------------------
# Token + low-level HTTP
# ---------------------------------------------------------------------------

def _token() -> str:
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"GitLab token missing at {TOKEN_PATH}") from exc


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{GITLAB_BASE}/projects/{PROJECT_ID}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "PRIVATE-TOKEN": _token(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitLab API {method} {path} failed: HTTP {exc.code}\n{body}"
        ) from exc
    return json.loads(body) if body else {}


# ---------------------------------------------------------------------------
# Pre-commit validation
# ---------------------------------------------------------------------------

def validate_python(files: Sequence[tuple[str, str]]) -> None:
    """Refuse the commit if any .py file fails to parse.

    `files` is a list of ``(repo_relative_path, content_text)`` tuples.
    Raises RuntimeError listing the offenders if any fail.
    """
    failures: list[tuple[str, str]] = []
    for path, content in files:
        if not path.endswith(".py"):
            continue
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            failures.append((path, f"{exc.__class__.__name__}: {exc}"))
    if failures:
        msg = "ast.parse failed; refusing commit:\n" + "\n".join(
            f"  - {p}: {err}" for p, err in failures
        )
        raise RuntimeError(msg)


def _basic_size_sanity(files: Sequence[tuple[str, str]]) -> None:
    """Cheap guard against zero-byte / suspiciously truncated payloads."""
    bad: list[str] = []
    for path, content in files:
        if not content.strip():
            bad.append(f"{path}: empty content")
        elif path.endswith(".py") and len(content) < 16:
            bad.append(f"{path}: < 16 bytes ({len(content)})")
    if bad:
        raise RuntimeError("size sanity failed:\n  " + "\n  ".join(bad))


# ---------------------------------------------------------------------------
# Branch / commit / MR
# ---------------------------------------------------------------------------

def branch_exists(branch: str) -> bool:
    try:
        _request("GET", f"/repository/branches/{urllib_quote(branch)}")
        return True
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return False
        raise


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def ensure_branch(branch: str, ref: str = DEFAULT_BRANCH) -> dict:
    if branch == DEFAULT_BRANCH:
        return {"name": branch, "note": "default branch, not creating"}
    if branch_exists(branch):
        return {"name": branch, "note": "exists"}
    return _request(
        "POST",
        f"/repository/branches?branch={urllib_quote(branch)}&ref={urllib_quote(ref)}",
    )


def _existing_paths(branch: str, paths: Iterable[str]) -> set[str]:
    """Return the subset of paths that already exist on `branch`.

    Used to choose ``create`` vs ``update`` action per file.
    """
    existing: set[str] = set()
    for p in paths:
        try:
            _request(
                "GET",
                f"/repository/files/{urllib_quote(p)}?ref={urllib_quote(branch)}",
            )
            existing.add(p)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                continue
            raise
    return existing


def commit_files(
    files: Sequence[tuple[str, str]],
    branch: str,
    message: str,
    *,
    create_branch_from: str = DEFAULT_BRANCH,
    skip_validation: bool = False,
) -> dict:
    """Commit a batch of files via the GitLab API.

    `files`: sequence of ``(repo_relative_path, content_text)``.
    Branch is auto-created from ``create_branch_from`` if missing.
    All .py files are ast.parse-validated unless ``skip_validation`` is True.
    """
    if not files:
        raise ValueError("commit_files: empty file list")
    if not skip_validation:
        validate_python(files)
        _basic_size_sanity(files)

    ensure_branch(branch, ref=create_branch_from)

    # Determine create vs update per file.
    existing = _existing_paths(branch, [p for p, _ in files])
    actions = []
    for path, content in files:
        action = "update" if path in existing else "create"
        actions.append({
            "action": action,
            "file_path": path,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "encoding": "base64",
        })

    payload = {
        "branch": branch,
        "commit_message": message,
        "actions": actions,
    }
    return _request("POST", "/repository/commits", payload)


def open_mr(
    *,
    source_branch: str,
    title: str,
    description: str = "",
    target_branch: str = DEFAULT_BRANCH,
    remove_source_branch: bool = True,
    squash: bool = False,
) -> dict:
    payload = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
        "remove_source_branch": remove_source_branch,
        "squash": squash,
    }
    return _request("POST", "/merge_requests", payload)


# ---------------------------------------------------------------------------
# CLI: validate-only helper for ad-hoc use.
# ---------------------------------------------------------------------------

def _cli_validate(paths: list[str]) -> int:
    files: list[tuple[str, str]] = []
    for p in paths:
        text = Path(p).read_text(encoding="utf-8")
        rel = p
        files.append((rel, text))
    try:
        validate_python(files)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OK: {len(files)} file(s) parsed clean.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "validate":
        print("usage: api_commit.py validate <file.py> [file.py ...]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(_cli_validate(sys.argv[2:]))
