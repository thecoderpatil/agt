"""Load safety_invariants.yaml and run registered checks.

Entry points:
    load_invariants(yaml_path) -> list[dict]
    build_context(db_path, ...) -> CheckContext
    run_all(...) -> dict[invariant_id, list[Violation]]

The runner never mutates the DB. It opens a read-only URI connection by
default; callers may inject their own for tests.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError as exc:  # pragma: no cover - pyyaml is required
    raise ImportError(
        "PyYAML required for invariant runner; pip install pyyaml"
    ) from exc

from .checks import CHECK_REGISTRY
from .types import CheckContext, Violation
from agt_equities.config import ACCOUNT_TO_HOUSEHOLD

log = logging.getLogger(__name__)

_DEFAULT_YAML_ANCHOR = Path(__file__).resolve().parent.parent / "safety_invariants.yaml"
DEFAULT_YAML_PATH = Path(os.environ.get("AGT_INVARIANTS_YAML", str(_DEFAULT_YAML_ANCHOR)))
DEFAULT_DB_PATH: str = (
    os.environ.get("AGT_DB_PATH")
    or r"C:\AGT_Telegram_Bridge\agt_desk.db"
)


def load_invariants(yaml_path: str | Path = DEFAULT_YAML_PATH) -> list[dict[str, Any]]:
    """Load and validate the invariant manifest."""
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "invariants" not in data:
        raise ValueError(
            f"Invalid invariant manifest at {yaml_path}: missing 'invariants' key"
        )
    required = {
        "id", "description", "check_fn", "scrutiny_tier",
        "fix_by_sprint", "max_consecutive_violations",
    }
    for entry in data["invariants"]:
        missing = required - set(entry.keys())
        if missing:
            raise ValueError(
                f"Invariant entry {entry.get('id', '?')} missing fields: {missing}"
            )
    return data["invariants"]


def build_context(
    db_path: str = DEFAULT_DB_PATH, now_utc: datetime | None = None
) -> CheckContext:
    """Construct a CheckContext from environment variables + sensible defaults."""
    _bm = os.environ.get("AGT_BROKER_MODE", "").strip().lower()
    paper_mode = (_bm == "paper") if _bm in ("paper", "live") else (os.environ.get("AGT_PAPER_MODE", "0") == "1")
    # E-M-1 (Sprint 3 MR 4): derive live-accounts default from config.ACCOUNT_TO_HOUSEHOLD
    # rather than re-hardcoding. Prior hardcoded literal was a second source of truth
    # that could drift from config.HOUSEHOLD_MAP on any account rename.
    _live_default = ",".join(sorted(ACCOUNT_TO_HOUSEHOLD))
    live_accounts = frozenset(
        a.strip()
        for a in os.environ.get("AGT_LIVE_ACCOUNTS", _live_default).split(",")
        if a.strip()
    )
    # Paper default retained until a canonical paper-household map exists in config.
    # Punt per dispatch: "if add one, scope creeps out of this MR — in that case just
    # leave the paper default and only de-drift the live default".
    paper_raw = os.environ.get(
        "AGT_PAPER_ACCOUNTS",
        "DUP751003:Yash_Household,DUP751004:Yash_Household,DUP751005:Vikram_Household",
    )
    paper_accounts = frozenset(
        p.split(":", 1)[0].strip()
        for p in paper_raw.split(",")
        if p.strip()
    )
    expected_daemons = frozenset(
        d.strip()
        for d in os.environ.get("AGT_EXPECTED_DAEMONS", "agt_bot").split(",")
        if d.strip()
    )
    return CheckContext(
        now_utc=now_utc or datetime.now(tz=timezone.utc),
        db_path=db_path,
        paper_mode=paper_mode,
        live_accounts=live_accounts,
        paper_accounts=paper_accounts,
        expected_daemons=expected_daemons,
    )


def run_all(
    yaml_path: str | Path = DEFAULT_YAML_PATH,
    db_path: str = DEFAULT_DB_PATH,
    ctx: CheckContext | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, list[Violation]]:
    """Run every registered check against conn.

    Returns a dict keyed by invariant_id. An invariant whose check raises is
    recorded as a single degraded Violation rather than aborting the batch -
    Step-4 scheduler heartbeat must never die because one check has a bug.
    """
    manifest = load_invariants(yaml_path)
    if ctx is None:
        ctx = build_context(db_path=db_path)
    _close_conn = False
    if conn is None:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        _close_conn = True
    results: dict[str, list[Violation]] = {}
    try:
        for entry in manifest:
            inv_id = entry["id"]
            fn = CHECK_REGISTRY.get(inv_id)
            if fn is None:
                log.warning(
                    "Invariant %s has no registered check function; skipping",
                    inv_id,
                )
                results[inv_id] = []
                continue
            try:
                results[inv_id] = fn(conn, ctx)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception(
                    "Check %s raised; recording as degraded Violation", inv_id
                )
                results[inv_id] = [Violation(
                    invariant_id=inv_id,
                    description=f"Check raised {type(exc).__name__}: {exc}",
                    severity="medium",
                    evidence={"degraded": True, "error": str(exc)},
                )]
    finally:
        if _close_conn:
            conn.close()
    return results
