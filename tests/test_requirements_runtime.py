"""MR !84 -- dep_drift_check CI job support tests.

Validates that ``requirements-runtime.txt`` parses cleanly and every line
matches PEP-508 package-name-with-specifier form. The real drift detection
happens in the ``dep_drift_check`` GitLab CI job which installs the
manifest in an isolated container and smoke-imports every module the bot
and scheduler load at boot.

These unit tests enforce local sanity before CI burns compute:
    * file is readable
    * non-comment lines parse
    * package names are distinct (no accidental dupe pins)
    * no pathological versions like `pyyaml==` (bare == triggers pip error)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "requirements-runtime.txt"


def _parse_manifest():
    """Return list[(name, spec)] from requirements-runtime.txt."""
    out = []
    for raw in MANIFEST.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_\-.]+)\s*(.*)$", line)
        assert m is not None, f"Could not parse manifest line: {raw!r}"
        out.append((m.group(1).lower(), m.group(2).strip()))
    return out


def test_manifest_exists_and_nonempty():
    assert MANIFEST.is_file(), f"Missing {MANIFEST}"
    entries = _parse_manifest()
    assert len(entries) >= 10, (
        f"Expected >=10 manifest entries, got {len(entries)}. "
        "Did you accidentally truncate the file?"
    )


def test_manifest_no_duplicate_package_names():
    names = [name for name, _ in _parse_manifest()]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert dupes == [], f"Duplicate manifest entries: {dupes}"


def test_manifest_no_empty_version_specifier():
    """Guard against bare ``pkg==`` / ``pkg>=`` (pip errors on those)."""
    bad = []
    for name, spec in _parse_manifest():
        if spec and re.search(r"[=<>!~]\s*$", spec):
            bad.append((name, spec))
    assert bad == [], f"Manifest entries with empty version: {bad}"


def test_manifest_names_look_like_pypi_packages():
    """Package names must be valid PEP-508 distribution names."""
    bad = []
    pattern = re.compile(r"^[a-z0-9]([a-z0-9._\-]*[a-z0-9])?$")
    for name, _ in _parse_manifest():
        if not pattern.match(name):
            bad.append(name)
    assert bad == [], f"Invalid manifest names: {bad}"


def test_manifest_pins_core_production_deps():
    """Hard guard: the runtime imports we KNOW we load at boot are in the
    manifest. Adding a new production import without updating this list is
    caught first by dep_drift_check in CI, but this test ensures we don't
    ship a manifest that was truncated below the safe set."""
    names = {n for n, _ in _parse_manifest()}
    required = {
        "pyyaml",              # agt_equities.invariants.runner
        "ib_async",            # agt_scheduler, ib_conn
        "apscheduler",         # agt_scheduler
        "python-telegram-bot", # telegram_bot
        "python-dotenv",       # telegram_bot
        "anthropic",           # telegram_bot, author_critic
        "finnhub-python",      # telegram_bot
        "yfinance",            # telegram_bot
        "pandas",              # telegram_bot
        "pytz",                # telegram_bot
        "psutil",              # check_no_zombie_bot_process
        "requests",            # scripts/*, GitLab API paths
    }
    missing = sorted(required - names)
    assert missing == [], f"Core deps missing from manifest: {missing}"
