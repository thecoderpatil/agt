"""Tests for Sweep-A __file__-anchor override correctness.

Verifies that AGT_DB_PATH and AGT_SCREENER_CACHE_ROOT env vars override
the __file__-anchored defaults. Defense-in-depth against the F.1 bug
class (2026-04-19: 42hr silent split-brain outage caused by
__file__-anchored DB_PATH under NSSM airgap deploy).
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


def _reload(module_name: str):
    """Reload a module so its module-level constants re-evaluate env."""
    import sys
    if module_name in sys.modules:
        mod = sys.modules[module_name]
        importlib.reload(mod)
        return mod
    return importlib.import_module(module_name)


def test_flex_sync_db_path_honors_env_override(monkeypatch) -> None:
    """flex_sync.DB_PATH resolves AGT_DB_PATH env var, not __file__."""
    monkeypatch.setenv("AGT_DB_PATH", r"C:\test\override\agt_desk.db")
    mod = _reload("agt_equities.flex_sync")
    assert Path(mod.DB_PATH) == Path(r"C:\test\override\agt_desk.db")


def test_flex_sync_db_path_default_preserved(monkeypatch) -> None:
    """With AGT_DB_PATH unset, flex_sync.DB_PATH falls back to __file__ anchor."""
    monkeypatch.delenv("AGT_DB_PATH", raising=False)
    mod = _reload("agt_equities.flex_sync")
    assert Path(mod.DB_PATH).name == "agt_desk.db"
    assert "override" not in str(mod.DB_PATH)


def test_screener_cache_root_honors_env_override(monkeypatch) -> None:
    """screener.cache.CACHE_ROOT resolves AGT_SCREENER_CACHE_ROOT, not __file__."""
    monkeypatch.setenv("AGT_SCREENER_CACHE_ROOT", r"C:\test\override\screener_cache")
    mod = _reload("agt_equities.screener.cache")
    assert Path(mod.CACHE_ROOT) == Path(r"C:\test\override\screener_cache")


def test_screener_cache_root_default_preserved(monkeypatch) -> None:
    """With env var unset, CACHE_ROOT falls back to repo-relative default."""
    monkeypatch.delenv("AGT_SCREENER_CACHE_ROOT", raising=False)
    mod = _reload("agt_equities.screener.cache")
    assert Path(mod.CACHE_ROOT).name == "screener"
    assert Path(mod.CACHE_ROOT).parent.name == "agt_desk_cache"
