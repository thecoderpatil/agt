"""Tests for Sweep-B resource-path env overrides.

Verifies AGT_ENV_FILE, AGT_INVARIANTS_YAML, AGT_PROMOTION_GATES_CONFIG,
AGT_GITLAB_TOKEN_PATH env vars correctly override their __file__-anchored
defaults. Defense-in-depth for deploy-bundle integrity.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


def _reload(module_name: str):
    import sys
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


def test_config_env_path_honors_override(monkeypatch) -> None:
    monkeypatch.setenv("AGT_ENV_FILE", r"C:\test\override\.env")
    mod = _reload("agt_equities.config")
    assert Path(mod._env_path) == Path(r"C:\test\override\.env")


def test_config_env_path_default_preserved(monkeypatch) -> None:
    monkeypatch.delenv("AGT_ENV_FILE", raising=False)
    mod = _reload("agt_equities.config")
    assert Path(mod._env_path).name == ".env"


def test_invariants_yaml_honors_override(monkeypatch) -> None:
    monkeypatch.setenv("AGT_INVARIANTS_YAML", r"C:\test\override\safety.yaml")
    mod = _reload("agt_equities.invariants.runner")
    assert Path(mod.DEFAULT_YAML_PATH) == Path(r"C:\test\override\safety.yaml")


def test_invariants_yaml_default_preserved(monkeypatch) -> None:
    monkeypatch.delenv("AGT_INVARIANTS_YAML", raising=False)
    mod = _reload("agt_equities.invariants.runner")
    assert Path(mod.DEFAULT_YAML_PATH).name == "safety_invariants.yaml"


def test_promotion_gates_config_honors_override(monkeypatch) -> None:
    monkeypatch.setenv("AGT_PROMOTION_GATES_CONFIG", r"C:\test\override\gates.yaml")
    mod = _reload("agt_equities.promotion_gates")
    assert Path(mod._DEFAULT_CONFIG_PATH) == Path(r"C:\test\override\gates.yaml")


def test_promotion_gates_config_default_preserved(monkeypatch) -> None:
    monkeypatch.delenv("AGT_PROMOTION_GATES_CONFIG", raising=False)
    mod = _reload("agt_equities.promotion_gates")
    assert Path(mod._DEFAULT_CONFIG_PATH).name == "promotion_gates.yaml"


def test_gitlab_token_path_honors_override(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "override_token"
    token_file.write_text("glpat-override-token-value", encoding="utf-8")
    monkeypatch.setenv("AGT_GITLAB_TOKEN_PATH", str(token_file))
    mod = _reload("agt_equities.remediation")
    assert mod._gitlab_token() == "glpat-override-token-value"


def test_gitlab_token_missing_override_raises(monkeypatch) -> None:
    """If override points at non-existent file, RuntimeError surfaces the path."""
    monkeypatch.setenv("AGT_GITLAB_TOKEN_PATH", r"C:\test\nonexistent\token")
    mod = _reload("agt_equities.remediation")
    with pytest.raises(RuntimeError, match="GitLab token missing"):
        mod._gitlab_token()
