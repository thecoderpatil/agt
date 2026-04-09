"""Execution kill-switch tests — triple gate (env, halt, DB)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestExecutionDisabledByDefault(unittest.TestCase):
    """Env var AGT_EXECUTION_ENABLED unset → execution blocked."""

    def test_blocked_without_env(self):
        # Ensure env var is not set
        os.environ.pop("AGT_EXECUTION_ENABLED", None)
        # Re-import to pick up env change
        from agt_equities.execution_gate import assert_execution_enabled, ExecutionDisabledError
        with self.assertRaises(ExecutionDisabledError) as ctx:
            assert_execution_enabled(in_process_halted=False)
        self.assertIn("env var", str(ctx.exception))


class TestExecutionBlockedWhenHalted(unittest.TestCase):
    """Env enabled but in_process_halted=True → blocked."""

    def test_halted_blocks(self):
        os.environ["AGT_EXECUTION_ENABLED"] = "true"
        try:
            from agt_equities.execution_gate import assert_execution_enabled, ExecutionDisabledError
            with self.assertRaises(ExecutionDisabledError) as ctx:
                assert_execution_enabled(in_process_halted=True)
            self.assertIn("halt", str(ctx.exception).lower())
        finally:
            os.environ.pop("AGT_EXECUTION_ENABLED", None)


class TestExecutionEnabledAllThree(unittest.TestCase):
    """All three gates allow → no exception."""

    def test_all_enabled(self):
        os.environ["AGT_EXECUTION_ENABLED"] = "true"
        try:
            from agt_equities import execution_gate
            from unittest.mock import patch
            with patch.object(execution_gate, "_db_enabled", return_value=True):
                # Should not raise
                execution_gate.assert_execution_enabled(in_process_halted=False)
        finally:
            os.environ.pop("AGT_EXECUTION_ENABLED", None)


class TestDbDisabledBlocks(unittest.TestCase):
    """Env enabled, not halted, but DB disabled → blocked."""

    def test_db_disabled(self):
        os.environ["AGT_EXECUTION_ENABLED"] = "true"
        try:
            from agt_equities import execution_gate
            from agt_equities.execution_gate import ExecutionDisabledError
            from unittest.mock import patch
            with patch.object(execution_gate, "_db_enabled", return_value=False):
                with self.assertRaises(ExecutionDisabledError) as ctx:
                    execution_gate.assert_execution_enabled(in_process_halted=False)
                self.assertIn("DB row", str(ctx.exception))
        finally:
            os.environ.pop("AGT_EXECUTION_ENABLED", None)


class TestExecutionDisabledErrorIsRuntimeError(unittest.TestCase):
    """ExecutionDisabledError must be a RuntimeError subclass."""

    def test_inheritance(self):
        from agt_equities.execution_gate import ExecutionDisabledError
        self.assertTrue(issubclass(ExecutionDisabledError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
