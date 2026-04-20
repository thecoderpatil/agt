"""MR 1 boot contract tests.

Validates assert_boot_contract() fails loud on every required env var
absent, present-but-invalid, and present-and-valid. These tests cannot
use the tripwire fixture because they DIRECTLY exercise the env-var
resolution that the tripwire patches -- exempt them.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agt_equities.boot import assert_boot_contract, REQUIRED_BOOT_ENV


pytestmark = [pytest.mark.agt_tripwire_exempt, pytest.mark.sprint_a]


def _clear_env(monkeypatch):
    for k in REQUIRED_BOOT_ENV:
        monkeypatch.delenv(k, raising=False)


def test_boot_contract_fails_on_missing_db_path(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGT_ENV_FILE", str(tmp_path / ".env"))
    (tmp_path / ".env").write_text("")
    monkeypatch.setenv("AGT_BROKER_MODE", "paper")
    with pytest.raises(SystemExit) as exc:
        assert_boot_contract()
    assert "AGT_DB_PATH" in str(exc.value)


def test_boot_contract_fails_on_missing_env_file(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGT_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("AGT_ENV_FILE", str(tmp_path / "does_not_exist.env"))
    monkeypatch.setenv("AGT_BROKER_MODE", "paper")
    with pytest.raises(SystemExit) as exc:
        assert_boot_contract()
    assert "does_not_exist.env" in str(exc.value)


def test_boot_contract_fails_on_invalid_broker_mode(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGT_DB_PATH", str(tmp_path / "db.sqlite"))
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setenv("AGT_ENV_FILE", str(env))
    monkeypatch.setenv("AGT_BROKER_MODE", "sandbox")
    with pytest.raises(SystemExit) as exc:
        assert_boot_contract()
    assert "AGT_BROKER_MODE" in str(exc.value)


def test_boot_contract_fails_on_missing_db_parent(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGT_DB_PATH", str(tmp_path / "nonexistent_dir" / "db.sqlite"))
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setenv("AGT_ENV_FILE", str(env))
    monkeypatch.setenv("AGT_BROKER_MODE", "paper")
    with pytest.raises(SystemExit) as exc:
        assert_boot_contract()
    assert "parent directory" in str(exc.value)


def test_boot_contract_passes_on_valid_env(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGT_DB_PATH", str(tmp_path / "db.sqlite"))
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setenv("AGT_ENV_FILE", str(env))
    monkeypatch.setenv("AGT_BROKER_MODE", "paper")
    assert_boot_contract()   # must not raise


def test_boot_contract_passes_on_live_mode(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGT_DB_PATH", str(tmp_path / "db.sqlite"))
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setenv("AGT_ENV_FILE", str(env))
    monkeypatch.setenv("AGT_BROKER_MODE", "live")
    assert_boot_contract()   # must not raise
