"""Tests for strict execution-gate control-plane read variants.

Strict variants raise ControlPlaneUnreadable on DB failure.
Tolerant variant must still pass through (regression guard).

mode_engine.py was retired in ADR-014 (commit cd84487). No mode-strict
tests here -- no callers to migrate.
"""
import pytest
from unittest.mock import patch

from agt_equities.exceptions import AgtSystemFailure, ControlPlaneUnreadable
from agt_equities.execution_gate import (
    ExecutionDisabledError,
    assert_execution_enabled_strict,
)

pytestmark = pytest.mark.sprint_a


def test_control_plane_unreadable_is_agt_system_failure():
    """Exception hierarchy: ControlPlaneUnreadable catchable as AgtSystemFailure + RuntimeError."""
    exc = ControlPlaneUnreadable("test")
    assert isinstance(exc, AgtSystemFailure)
    assert isinstance(exc, RuntimeError)


def test_strict_execution_raises_control_plane_unreadable_on_db_error():
    with patch("agt_equities.execution_gate._db_enabled_strict") as mock_db:
        mock_db.side_effect = ControlPlaneUnreadable("DB read failed")
        with patch("agt_equities.execution_gate._env_enabled", return_value=True):
            with pytest.raises(ControlPlaneUnreadable):
                assert_execution_enabled_strict(in_process_halted=False)


def test_strict_execution_raises_disabled_error_when_db_disabled():
    with patch("agt_equities.execution_gate._db_enabled_strict", return_value=False):
        with patch("agt_equities.execution_gate._env_enabled", return_value=True):
            with pytest.raises(ExecutionDisabledError):
                assert_execution_enabled_strict(in_process_halted=False)


def test_strict_execution_passes_when_all_gates_open():
    with patch("agt_equities.execution_gate._db_enabled_strict", return_value=True):
        with patch("agt_equities.execution_gate._env_enabled", return_value=True):
            assert_execution_enabled_strict(in_process_halted=False)  # must not raise
