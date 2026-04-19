"""Tests for ADR-007 Addendum §2.1 bootstrap assertion."""
from __future__ import annotations

from unittest import mock

import pytest

from agt_equities.invariants import bootstrap as bootstrap_mod
from agt_equities.invariants.bootstrap import (
    SelfHealingBootstrapError,
    assert_canonical_db_path,
)
from agt_equities.runtime import PROD_DB_PATH

pytestmark = pytest.mark.sprint_a


def test_canonical_path_passes() -> None:
    """Canonical path must not raise."""
    assert_canonical_db_path(resolved_path=PROD_DB_PATH)


def test_non_canonical_path_raises(tmp_path) -> None:
    """Non-canonical path must raise SelfHealingBootstrapError."""
    scratch = tmp_path / "scratch.db"
    scratch.touch()
    with pytest.raises(SelfHealingBootstrapError) as exc_info:
        assert_canonical_db_path(resolved_path=str(scratch))
    assert "SELF_HEALING_DB_PATH_MISMATCH" in str(exc_info.value)


def test_override_flag_bypasses(tmp_path) -> None:
    """allow_override=True must silence the raise even on mismatch."""
    scratch = tmp_path / "scratch.db"
    scratch.touch()
    assert_canonical_db_path(
        resolved_path=str(scratch),
        allow_override=True,
    )


def test_telegram_failure_does_not_mask_raise(
    tmp_path, monkeypatch, capsys,
) -> None:
    """Telegram send_telegram_message raising must not suppress the primary raise."""
    scratch = tmp_path / "scratch.db"
    scratch.touch()

    def _boom(*args, **kwargs):
        raise RuntimeError("telegram down")

    with mock.patch(
        "agt_equities.telegram_utils.send_telegram_message",
        side_effect=_boom,
    ):
        with pytest.raises(SelfHealingBootstrapError):
            assert_canonical_db_path(resolved_path=str(scratch))


def test_stderr_captures_message(tmp_path, capsys) -> None:
    """The mismatch message must appear on stderr before the raise."""
    scratch = tmp_path / "scratch.db"
    scratch.touch()
    with pytest.raises(SelfHealingBootstrapError):
        assert_canonical_db_path(resolved_path=str(scratch))
    captured = capsys.readouterr()
    assert "SELF_HEALING_DB_PATH_MISMATCH" in captured.err
