"""
AGT Equities -- boot contract.

Called as the first line of every operational process entrypoint
(telegram_bot.py main, agt_scheduler __main__, dev_cli.py __main__,
scripts/circuit_breaker.py __main__, and any other `if __name__ ==
"__main__"` that touches the DB).

Fails loud at process start if the operational env is incomplete.
Loads the canonical .env file from AGT_ENV_FILE with override=False
so NSSM AppEnvironmentExtra is authoritative over .env values.

Does NOT fail at import time -- tests import agt_equities.* without
setting these env vars and the tripwire fixture injects its own
AGT_DB_PATH value per-test.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

REQUIRED_BOOT_ENV = (
    "AGT_DB_PATH",      # absolute path to operational SQLite DB
    "AGT_ENV_FILE",     # absolute path to the canonical .env
    "AGT_BROKER_MODE",  # "paper" | "live" -- authoritative
)

VALID_BROKER_MODES = ("paper", "live")


def _sha256_file(path: Path) -> str:
    """Return lower-case hex sha256 of path. Used for .env drift
    detection in post-merge diagnostic reports -- NOT for tamper-resistance.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_boot_contract() -> None:
    """Validate operational env at process start.

    Raises SystemExit with a descriptive message if any required env var
    is missing, the .env file does not exist, or AGT_BROKER_MODE is
    invalid. The service cannot proceed in any of those states -- a
    silent fallback would mask an operational misconfiguration.

    Load order:
      1. Validate REQUIRED_BOOT_ENV -- all three must be set.
      2. Load .env from AGT_ENV_FILE with override=False. Env vars
         already set by NSSM AppEnvironmentExtra or the shell win over
         values in the .env file. This is the opposite of the library
         default and is load-bearing: operator-controlled env must
         outrank file-based config.
      3. Validate AGT_BROKER_MODE in VALID_BROKER_MODES.
      4. Validate AGT_DB_PATH parent directory exists (the DB file itself
         may be created on first connection).

    Call once per process, as early as possible in main().
    """
    missing = [k for k in REQUIRED_BOOT_ENV if not os.environ.get(k, "").strip()]
    if missing:
        raise SystemExit(
            f"AGT boot contract violated: missing env vars {missing}. "
            f"Set via NSSM AppEnvironmentExtra (preferred) or AGT_ENV_FILE "
            f"contents. Service cannot start. See docs/adr/ for boot-contract "
            f"rationale."
        )

    env_file = Path(os.environ["AGT_ENV_FILE"])
    if not env_file.is_file():
        raise SystemExit(
            f"AGT boot contract violated: AGT_ENV_FILE points to "
            f"{env_file} which does not exist. Service cannot start."
        )

    # Lazy import to keep this module importable without dotenv installed
    # (e.g., in minimal test environments).
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise SystemExit(
            f"AGT boot contract violated: python-dotenv not installed "
            f"in this environment. {exc}"
        )

    # override=False is load-bearing. NSSM-injected env wins over .env.
    load_dotenv(env_file, override=False)

    # Re-validate after .env load (in case AGT_BROKER_MODE came from the
    # file and not from NSSM).
    broker_mode = os.environ.get("AGT_BROKER_MODE", "").strip().lower()
    if broker_mode not in VALID_BROKER_MODES:
        raise SystemExit(
            f"AGT boot contract violated: AGT_BROKER_MODE="
            f"{broker_mode!r} not in {VALID_BROKER_MODES}. "
            f"Service cannot start."
        )

    db_path = Path(os.environ["AGT_DB_PATH"])
    if not db_path.parent.is_dir():
        raise SystemExit(
            f"AGT boot contract violated: AGT_DB_PATH={db_path} -- "
            f"parent directory {db_path.parent} does not exist. "
            f"Service cannot start."
        )


def diagnostic_boot_snapshot() -> dict:
    """Return a JSON-serializable dict describing the boot-time state.

    Called by the first-boot log line of each service. Does NOT raise --
    safe to call in exception handlers. Used by operators to diagnose
    drift between AGT_ENV_FILE on disk and the values actually loaded.
    """
    snapshot = {
        "pid": os.getpid(),
        "executable": sys.executable,
        "argv": sys.argv,
        "cwd": str(Path.cwd()),
        "required_env_present": {
            k: bool(os.environ.get(k, "").strip()) for k in REQUIRED_BOOT_ENV
        },
    }
    env_file = os.environ.get("AGT_ENV_FILE")
    if env_file and Path(env_file).is_file():
        try:
            snapshot["env_file_sha256"] = _sha256_file(Path(env_file))
        except OSError as exc:
            snapshot["env_file_sha256_error"] = str(exc)
    return snapshot
