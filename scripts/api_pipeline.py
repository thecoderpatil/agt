"""GitLab REST API pipeline helper for AGT.

Companion to ``scripts/api_commit.py``. Provides the read-side of the CI
loop so the Architect can dogfood its own CI from the Cowork sandbox
without touching the local git index or shelling out to ``glab``.

Public surface
--------------

    wait_for_pipeline(branch, sha=None, timeout_s=600) -> dict
        Poll the latest pipeline for the ref (optionally pinned to sha)
        every 10 s until status is terminal, then return the pipeline
        dict. Raises TimeoutError if the budget expires.

    get_failed_jobs(pipeline_id) -> list[dict]
        Returns the jobs with status='failed' for a pipeline, in the
        order GitLab returns them.

    fetch_job_trace(job_id) -> str
        Returns the full plain-text job trace (the CI runner log).

CLI
---

    python scripts/api_pipeline.py wait <branch> [<sha>]
        Prints the terminal status and web_url of the latest pipeline
        on <branch> (optionally pinned to <sha>), then exits 0 on
        success and 1 on any non-success terminal status. On failure
        it also prints the failed job names + the last 80 lines of the
        first failed job's trace to stderr for quick triage.

Token: read from ``C:\\AGT_Telegram_Bridge\\.gitlab-token`` via the same
``_token()`` pattern as ``api_commit.py``.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

GITLAB_BASE = "https://gitlab.com/api/v4"
PROJECT_ID = "81096827"

REPO_ROOT = Path(__file__).resolve().parent.parent  # AGT_Telegram_Bridge
TOKEN_PATH = REPO_ROOT / ".gitlab-token"

# GitLab pipeline statuses that are terminal — once the API returns one
# of these, the pipeline has stopped moving.
TERMINAL_STATUSES = frozenset(
    {"success", "failed", "canceled", "skipped", "manual"}
)

_POLL_INTERVAL_S = 10


# ---------------------------------------------------------------------------
# Token + low-level HTTP (mirrors api_commit.py)
# ---------------------------------------------------------------------------

def _token() -> str:
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"GitLab token missing at {TOKEN_PATH}") from exc


def _request(
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    raw: bool = False,
) -> Any:
    """HTTP call against the GitLab project API.

    If ``raw`` is True the response body is returned as text (used for
    job traces, which are plain-text, not JSON).
    """
    url = f"{GITLAB_BASE}/projects/{PROJECT_ID}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"PRIVATE-TOKEN": _token()}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if not raw:
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitLab API {method} {path} failed: HTTP {exc.code}\n{err_body}"
        ) from exc
    if raw:
        return body
    return json.loads(body) if body else {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _latest_pipeline(branch: str, sha: str | None) -> dict | None:
    """Return the most recent pipeline dict for (branch, sha) or None."""
    params: list[tuple[str, str]] = [
        ("ref", branch),
        ("per_page", "1"),
        ("order_by", "id"),
        ("sort", "desc"),
    ]
    if sha:
        params.append(("sha", sha))
    qs = urllib.parse.urlencode(params)
    data = _request("GET", f"/pipelines?{qs}")
    if not isinstance(data, list) or not data:
        return None
    return data[0]


def wait_for_pipeline(
    branch: str,
    sha: str | None = None,
    timeout_s: int = 600,
) -> dict:
    """Block until the latest pipeline for (branch, sha) reaches terminal status.

    Polls every 10 s. Returns the final pipeline dict (as the list endpoint
    returns it; call the detail endpoint yourself if you need more fields).

    Raises:
        TimeoutError: ``timeout_s`` elapses before the pipeline reaches
            a terminal status.
        RuntimeError: no pipeline found for (branch, sha) after the first
            grace poll — surfaces misrouted push / rules: rejection cases
            fast instead of burning the whole timeout budget.
    """
    deadline = time.monotonic() + timeout_s
    grace_polls_remaining = 3  # ~30 s grace for the pipeline to appear
    last_status: str | None = None
    while True:
        try:
            pipe = _latest_pipeline(branch, sha)
        except RuntimeError as exc:
            print(f"[api_pipeline] transient API error: {exc}", file=sys.stderr)
            pipe = None

        if pipe is None:
            if grace_polls_remaining <= 0:
                raise RuntimeError(
                    f"No pipeline found for ref={branch!r} sha={sha!r} "
                    f"after grace polls; pipeline may have been rejected "
                    f"by CI rules or never created."
                )
            grace_polls_remaining -= 1
        else:
            status = pipe.get("status")
            if status != last_status:
                print(
                    f"[api_pipeline] pipeline {pipe.get('id')} status={status}",
                    file=sys.stderr,
                )
                last_status = status
            if status in TERMINAL_STATUSES:
                return pipe

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"wait_for_pipeline: timed out after {timeout_s}s "
                f"(ref={branch!r} sha={sha!r}, last_status={last_status!r})"
            )
        time.sleep(_POLL_INTERVAL_S)


def get_failed_jobs(pipeline_id: int) -> list[dict]:
    """Return the jobs with status='failed' for a pipeline."""
    jobs = _request(
        "GET",
        f"/pipelines/{int(pipeline_id)}/jobs?per_page=100",
    )
    if not isinstance(jobs, list):
        return []
    return [j for j in jobs if j.get("status") == "failed"]


def fetch_job_trace(job_id: int) -> str:
    """Return the full plain-text trace for a job."""
    return _request("GET", f"/jobs/{int(job_id)}/trace", raw=True)


# ---------------------------------------------------------------------------
# CLI: self-test + triage helper
# ---------------------------------------------------------------------------

def _cli_wait(argv: list[str]) -> int:
    if not argv:
        print("usage: api_pipeline.py wait <branch> [<sha>]", file=sys.stderr)
        return 2
    branch = argv[0]
    sha = argv[1] if len(argv) > 1 else None
    try:
        pipe = wait_for_pipeline(branch, sha=sha)
    except (TimeoutError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    status = pipe.get("status")
    web_url = pipe.get("web_url", "")
    pipe_id = pipe.get("id")
    print(f"pipeline {pipe_id} status={status} url={web_url}")

    if status == "success":
        return 0

    try:
        failed = get_failed_jobs(int(pipe_id))
    except RuntimeError as exc:
        print(f"(could not list failed jobs: {exc})", file=sys.stderr)
        return 1
    if failed:
        print("failed jobs:", file=sys.stderr)
        for j in failed:
            print(f"  - {j.get('name')} (id={j.get('id')})", file=sys.stderr)
        first = failed[0]
        try:
            trace = fetch_job_trace(int(first["id"]))
        except RuntimeError as exc:
            print(f"(trace fetch failed: {exc})", file=sys.stderr)
        else:
            tail = "\n".join(trace.splitlines()[-80:])
            print(
                f"\n--- last 80 lines of job {first.get('name')} "
                f"({first.get('id')}) ---\n{tail}",
                file=sys.stderr,
            )
    return 1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: api_pipeline.py wait <branch> [<sha>]", file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "wait":
        return _cli_wait(argv[2:])
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
