"""Tests for agt_equities.runtime_fingerprint (MR 6, config-loudness sprint)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agt_equities.runtime_fingerprint import (
    SENTINEL_BEGIN,
    SENTINEL_END,
    _is_secret,
    capture_and_log,
    compute_config_fingerprint,
    format_sentinel_banner,
)

pytestmark = pytest.mark.sprint_a


def test_is_secret_matches_common_patterns():
    assert _is_secret("AGT_GITLAB_TOKEN")
    assert _is_secret("SOME_API_KEY")
    assert _is_secret("DB_PASSWORD")
    assert _is_secret("CLIENT_SECRET")
    assert _is_secret("AWS_CREDENTIALS")
    assert not _is_secret("AGT_BROKER_MODE")
    assert not _is_secret("AGT_DB_PATH")


def test_fingerprint_is_deterministic_for_same_inputs():
    env = {"AGT_BROKER_MODE": "paper", "AGT_DB_PATH": "/tmp/x.db"}
    fp1 = compute_config_fingerprint(service_name="svc", env=env)
    fp2 = compute_config_fingerprint(service_name="svc", env=env)
    assert fp1.envelope_hash == fp2.envelope_hash
    assert fp1.env_hash == fp2.env_hash


def test_env_delta_changes_hash():
    fp_a = compute_config_fingerprint(service_name="svc", env={"AGT_BROKER_MODE": "paper"})
    fp_b = compute_config_fingerprint(service_name="svc", env={"AGT_BROKER_MODE": "live"})
    assert fp_a.env_hash != fp_b.env_hash
    assert fp_a.envelope_hash != fp_b.envelope_hash


def test_secrets_are_redacted_in_banner_and_plaintext():
    env = {
        "AGT_GITLAB_TOKEN": "glpat-real-secret-abc123",
        "AGT_BROKER_MODE": "paper",
    }
    fp = compute_config_fingerprint(service_name="svc", env=env)
    assert "AGT_GITLAB_TOKEN" in fp.env_redacted_keys
    assert "AGT_GITLAB_TOKEN" not in fp.env_plaintext
    banner = format_sentinel_banner(fp)
    assert "glpat-real-secret-abc123" not in banner
    assert "redacted_keys=1" in banner


def test_plaintext_surfaces_only_allowlisted_keys():
    env = {
        "AGT_BROKER_MODE": "paper",
        "AGT_DB_PATH": "/tmp/agt_desk.db",
        "AGT_RANDOM_NEW_VAR": "not-in-allowlist",
    }
    fp = compute_config_fingerprint(service_name="svc", env=env)
    assert fp.env_plaintext["AGT_BROKER_MODE"] == "paper"
    assert fp.env_plaintext["AGT_DB_PATH"] == "/tmp/agt_desk.db"
    assert "AGT_RANDOM_NEW_VAR" not in fp.env_plaintext


def test_non_agt_env_keys_are_ignored():
    env_a = {"AGT_BROKER_MODE": "paper", "PATH": "/usr/bin", "HOME": "/home/yash"}
    env_b = {"AGT_BROKER_MODE": "paper", "PATH": "/different", "HOME": "/elsewhere"}
    fp_a = compute_config_fingerprint(service_name="svc", env=env_a)
    fp_b = compute_config_fingerprint(service_name="svc", env=env_b)
    assert fp_a.env_hash == fp_b.env_hash
    assert "PATH" not in fp_a.env_plaintext


def test_dotenv_hash_is_none_for_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist.env"
    fp = compute_config_fingerprint(
        service_name="svc", env={}, dotenv_paths=[missing]
    )
    assert fp.dotenv_hashes[str(missing)] is None


def test_dotenv_hash_changes_with_file_contents(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AGT_BROKER_MODE=paper\n", encoding="utf-8")
    fp1 = compute_config_fingerprint(
        service_name="svc", env={}, dotenv_paths=[env_file]
    )
    h1 = fp1.dotenv_hashes[str(env_file)]
    assert h1 is not None and len(h1) == 12

    env_file.write_text("AGT_BROKER_MODE=live\n", encoding="utf-8")
    fp2 = compute_config_fingerprint(
        service_name="svc", env={}, dotenv_paths=[env_file]
    )
    assert fp2.dotenv_hashes[str(env_file)] != h1



def test_read_nssm_appenv_importerror_returns_none(monkeypatch):
    """Non-Windows platform: winreg ImportError -> fail-open None."""
    from agt_equities import runtime_fingerprint as rf
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "winreg":
            raise ImportError("simulated non-Windows")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert rf._read_nssm_appenv("agt_bot") is None


def test_read_nssm_appenv_missing_service_returns_none(monkeypatch):
    """Windows but service-key missing: OSError -> fail-open None."""
    from agt_equities import runtime_fingerprint as rf

    fake_winreg = type("FakeWinreg", (), {})()
    fake_winreg.HKEY_LOCAL_MACHINE = 0
    def open_key(*a, **kw):
        raise FileNotFoundError("no such key")
    fake_winreg.OpenKey = open_key

    import sys
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    assert rf._read_nssm_appenv("definitely_not_a_real_service") is None


def test_read_nssm_appenv_parses_multi_sz(monkeypatch):
    """REG_MULTI_SZ list value -> dict[key, val] sorted."""
    from agt_equities import runtime_fingerprint as rf

    class FakeKey:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    fake_winreg = type("FakeWinreg", (), {})()
    fake_winreg.HKEY_LOCAL_MACHINE = 0
    fake_winreg.OpenKey = lambda *a, **kw: FakeKey()
    fake_winreg.QueryValueEx = lambda key, name: (
        ["USE_SCHEDULER_DAEMON=1", "AGT_BROKER_MODE=paper", "AGT_DB_PATH=/tmp/x.db"],
        7,  # REG_MULTI_SZ
    )

    import sys
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    result = rf._read_nssm_appenv("agt_bot")
    assert result == {
        "AGT_BROKER_MODE": "paper",
        "AGT_DB_PATH": "/tmp/x.db",
        "USE_SCHEDULER_DAEMON": "1",
    }

def test_nssm_absent_yields_nssm_hash_none():
    fp = compute_config_fingerprint(service_name="svc", env={}, nssm_services=[])
    assert fp.nssm_env_hash is None
    assert fp.nssm_env_keys == ()


def test_injected_nssm_reader_redacts_tokens():
    def fake_reader(svc_name: str):
        assert svc_name == "agt_bot"
        return {"AGT_BROKER_MODE": "paper", "AGT_GITLAB_TOKEN": "secret-abc"}

    fp = compute_config_fingerprint(
        service_name="svc",
        env={},
        nssm_services=["agt_bot"],
        nssm_reader=fake_reader,
    )
    assert fp.nssm_env_hash is not None
    assert "agt_bot:AGT_BROKER_MODE" in fp.nssm_env_keys
    assert "agt_bot:AGT_GITLAB_TOKEN" in fp.nssm_env_keys


def test_sentinel_banner_contains_begin_end_markers():
    env = {"AGT_BROKER_MODE": "paper"}
    fp = compute_config_fingerprint(service_name="agt_bot", env=env)
    banner = format_sentinel_banner(fp)
    lines = banner.splitlines()
    assert lines[0] == SENTINEL_BEGIN
    assert lines[-1] == SENTINEL_END
    assert "envelope_hash:" in banner
    assert "service:       agt_bot" in banner


def test_capture_and_log_fail_open_when_collector_raises(monkeypatch, caplog):
    def boom(**kwargs):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(
        "agt_equities.runtime_fingerprint.compute_config_fingerprint", boom
    )
    logger = logging.getLogger("test_runtime_fp_fail")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        result = capture_and_log(service_name="svc", logger=logger)
    assert result is None
    assert any("capture failed" in rec.message for rec in caplog.records)
