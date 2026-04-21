"""Runtime configuration fingerprint for AGT services.

Captures a deterministic snapshot of runtime configuration at service
startup so post-deploy drift is visible in logs. MR 6 — config-loudness
sprint. No DB writes, no network, fail-open on every collector.

The sentinel banner is grep-able from NSSM service logs:

    AGT_CONFIG_FP_BEGIN
    ...
    AGT_CONFIG_FP_END

Two consecutive restarts with the same envelope_hash are bit-identical
at the config layer. A hash delta means something about env vars, .env
file contents, NSSM AppEnvironmentExtra, or the deployed git tip changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

log = logging.getLogger(__name__)

# Env keys surfaced verbatim in the banner (non-secret, operationally useful).
_PLAINTEXT_ENV_KEYS: tuple[str, ...] = (
    "AGT_BROKER_MODE",
    "AGT_PAPER_MODE",
    "AGT_DB_PATH",
    "AGT_STATE_DIR",
    "AGT_SERVICE_NAME",
    "AGT_EXECUTION_ENABLED",
    "AGT_CSP_REQUIRE_APPROVAL",
    "AGT_CC_SUPPRESS_TICKERS",
    "AGT_MODE_PIN",
    "USE_SCHEDULER_DAEMON",
)

# Keys matching any of these are redacted (never logged verbatim).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r".*TOKEN.*",
        r".*SECRET.*",
        r".*PASSWORD.*",
        r".*API_KEY.*",
        r".*CREDENTIAL.*",
    )
)


def _is_secret(key: str) -> bool:
    return any(p.match(key) for p in _SECRET_PATTERNS)


def _is_agt_relevant(key: str) -> bool:
    return key.startswith("AGT_") or key == "USE_SCHEDULER_DAEMON"


def _sha256_short(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _sha256_file(path: Path) -> str | None:
    try:
        return _sha256_short(path.read_bytes())
    except (OSError, FileNotFoundError):
        return None


def _collect_env_snapshot(env: Mapping[str, str]) -> dict[str, str]:
    """Return redacted env snapshot. Secrets replaced with ``<redacted:{hash12}>``."""
    snap: dict[str, str] = {}
    for k, v in env.items():
        if not _is_agt_relevant(k):
            continue
        if _is_secret(k):
            h = _sha256_short(v.encode("utf-8")) if v else "empty"
            snap[k] = f"<redacted:{h}>"
        else:
            snap[k] = v
    return dict(sorted(snap.items()))


def _hash_env_snapshot(snap: Mapping[str, str]) -> str:
    payload = json.dumps(snap, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_short(payload)


_APPENV_LINE = re.compile(
    r"^\s*\S*nssm\S*\s+set\s+\S+\s+AppEnvironmentExtra\s+(.*)$",
    re.MULTILINE | re.IGNORECASE,
)
_APPENV_PAIR = re.compile(r'"([^"=]+)=([^"]*)"')


def _parse_nssm_appenv(dump_text: str) -> dict[str, str]:
    m = _APPENV_LINE.search(dump_text)
    if not m:
        return {}
    pairs = _APPENV_PAIR.findall(m.group(1))
    return dict(sorted(pairs))


def _read_nssm_appenv(service_name: str, *, nssm_path: str = "nssm") -> dict[str, str] | None:
    """Return parsed NSSM ``AppEnvironmentExtra`` for service, or None on any failure.

    Fail-open: unavailable nssm (CI, dev, Linux), timeout, or non-zero exit
    all yield None and a debug log line. Never raises.
    """
    try:
        result = subprocess.run(
            [nssm_path, "dump", service_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        log.debug("runtime_fingerprint: nssm dump unavailable (%s): %s", service_name, e)
        return None
    if result.returncode != 0:
        log.debug(
            "runtime_fingerprint: nssm dump rc=%s stderr=%s",
            result.returncode,
            result.stderr[:200],
        )
        return None
    return _parse_nssm_appenv(result.stdout)


def _git_tip(repo_root: Path) -> str | None:
    head = repo_root / ".git" / "HEAD"
    try:
        ref_line = head.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return None
    if ref_line.startswith("ref: "):
        ref = ref_line[5:]
        try:
            return (repo_root / ".git" / ref).read_text(encoding="utf-8").strip()[:12]
        except (OSError, FileNotFoundError):
            return None
    return ref_line[:12]


@dataclass(frozen=True)
class ConfigFingerprint:
    """Deterministic snapshot of runtime config. All strings, JSON-serializable."""

    captured_at: str
    service_name: str
    process_id: int
    python_version: str
    argv: tuple[str, ...]
    git_tip: str | None
    env_hash: str
    env_plaintext: dict[str, str]
    env_redacted_keys: tuple[str, ...]
    dotenv_hashes: dict[str, str | None]
    nssm_env_hash: str | None
    nssm_env_keys: tuple[str, ...]
    envelope_hash: str

    def to_dict(self) -> dict:
        return asdict(self)


def compute_config_fingerprint(
    *,
    service_name: str,
    env: Mapping[str, str] | None = None,
    dotenv_paths: list[Path] | None = None,
    repo_root: Path | None = None,
    nssm_services: list[str] | None = None,
    nssm_reader: Callable[[str], dict[str, str] | None] | None = None,
) -> ConfigFingerprint:
    """Build a ConfigFingerprint. Pure given inputs. Fail-open on every collector."""
    env = dict(env if env is not None else os.environ)
    dotenv_paths = dotenv_paths or []
    repo_root = repo_root or Path(__file__).resolve().parent.parent
    nssm_services = nssm_services or []
    nssm_reader = nssm_reader or _read_nssm_appenv

    snap = _collect_env_snapshot(env)
    plaintext = {k: v for k, v in snap.items() if k in _PLAINTEXT_ENV_KEYS}
    redacted_keys = tuple(k for k, v in snap.items() if v.startswith("<redacted:"))
    env_hash = _hash_env_snapshot(snap)

    dotenv_hashes: dict[str, str | None] = {}
    for p in dotenv_paths:
        dotenv_hashes[str(p)] = _sha256_file(p)

    nssm_combined: dict[str, str] = {}
    for svc in nssm_services:
        try:
            pairs = nssm_reader(svc) or {}
        except Exception as e:  # fail-open — injected readers must never kill boot
            log.debug("runtime_fingerprint: nssm_reader raised for %s: %s", svc, e)
            pairs = {}
        for k, v in pairs.items():
            tagged_key = f"{svc}:{k}"
            if _is_secret(k):
                h = _sha256_short(v.encode("utf-8")) if v else "empty"
                nssm_combined[tagged_key] = f"<redacted:{h}>"
            else:
                nssm_combined[tagged_key] = v
    nssm_env_hash: str | None = None
    nssm_env_keys: tuple[str, ...] = ()
    if nssm_combined:
        nssm_env_hash = _hash_env_snapshot(nssm_combined)
        nssm_env_keys = tuple(sorted(nssm_combined))

    envelope = {
        "env_hash": env_hash,
        "dotenv_hashes": dotenv_hashes,
        "nssm_env_hash": nssm_env_hash,
        "git_tip": _git_tip(repo_root),
    }
    envelope_hash = _hash_env_snapshot({k: str(v) for k, v in envelope.items()})

    return ConfigFingerprint(
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        service_name=service_name,
        process_id=os.getpid(),
        python_version=sys.version.split()[0],
        argv=tuple(sys.argv),
        git_tip=envelope["git_tip"],
        env_hash=env_hash,
        env_plaintext=plaintext,
        env_redacted_keys=redacted_keys,
        dotenv_hashes=dotenv_hashes,
        nssm_env_hash=nssm_env_hash,
        nssm_env_keys=nssm_env_keys,
        envelope_hash=envelope_hash,
    )


SENTINEL_BEGIN = "AGT_CONFIG_FP_BEGIN"
SENTINEL_END = "AGT_CONFIG_FP_END"


def format_sentinel_banner(fp: ConfigFingerprint) -> str:
    """Multiline ASCII-only banner suitable for INFO logging and grep."""
    lines = [
        SENTINEL_BEGIN,
        f"  envelope_hash: {fp.envelope_hash}",
        f"  service:       {fp.service_name}  pid={fp.process_id}  at={fp.captured_at}",
        f"  python:        {fp.python_version}",
        f"  git_tip:       {fp.git_tip or '<unknown>'}",
        f"  env_hash:      {fp.env_hash}  redacted_keys={len(fp.env_redacted_keys)}",
    ]
    for k, v in fp.env_plaintext.items():
        lines.append(f"  env.{k}={v}")
    for path, h in fp.dotenv_hashes.items():
        lines.append(f"  dotenv.{path}={h or '<missing>'}")
    if fp.nssm_env_hash is not None:
        lines.append(f"  nssm_env_hash: {fp.nssm_env_hash}  keys={len(fp.nssm_env_keys)}")
    else:
        lines.append("  nssm_env_hash: <unavailable>")
    lines.append(SENTINEL_END)
    return "\n".join(lines)


def log_fingerprint(fp: ConfigFingerprint, logger: logging.Logger | None = None) -> None:
    """Emit banner lines at INFO. Never raises."""
    target = logger or log
    try:
        for line in format_sentinel_banner(fp).splitlines():
            target.info(line)
    except Exception as e:  # fail-open — logging must never block boot
        target.warning("runtime_fingerprint: failed to log banner: %s", e)


def capture_and_log(
    *,
    service_name: str,
    dotenv_paths: list[Path] | None = None,
    nssm_services: list[str] | None = None,
    logger: logging.Logger | None = None,
) -> ConfigFingerprint | None:
    """Service-startup entry point. Fail-open; returns None if capture raises."""
    try:
        fp = compute_config_fingerprint(
            service_name=service_name,
            dotenv_paths=dotenv_paths,
            nssm_services=nssm_services,
        )
    except Exception as e:
        (logger or log).warning("runtime_fingerprint: capture failed: %s", e)
        return None
    log_fingerprint(fp, logger=logger)
    return fp
