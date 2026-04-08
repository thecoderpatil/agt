"""
Followup #4 regression test — yf_tkr initialization in _stage_dynamic_exit_candidate.

Bug: commit 85b24a6 fixed a latent NameError where yf_tkr was used but never
defined in _stage_dynamic_exit_candidate(). The variable yf_tkr was referenced
at the yf_tkr.option_chain() call, but the preceding yf_tkr = yf.Ticker(ticker)
line was missing (copy-paste gap from _generate_dynamic_exit_payload).

This test verifies the fix holds by:
  T1: Source-level assertion that yf_tkr initialization precedes usage in
      the function body. Catches the exact NameError class of bug regardless
      of mocking complexity.
  T2: AST-level verification that the function body contains an assignment
      to yf_tkr before any attribute access on yf_tkr.
"""

import ast
import inspect
import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestYfTkrInitialization(unittest.TestCase):
    """Regression test for commit 85b24a6 — yf_tkr must be initialized
    before use in _stage_dynamic_exit_candidate."""

    def test_yf_tkr_defined_before_option_chain_call(self):
        """Source-level: yf_tkr = yf.Ticker(ticker) must appear before
        yf_tkr.option_chain in _stage_dynamic_exit_candidate body."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            source = f.read()

        # Extract the function body
        func_start = source.find('async def _stage_dynamic_exit_candidate(')
        self.assertNotEqual(func_start, -1,
                            "_stage_dynamic_exit_candidate not found in telegram_bot.py")

        # Find the next function definition after this one to bound the search
        next_func = source.find('\nasync def ', func_start + 10)
        if next_func == -1:
            next_func = source.find('\ndef ', func_start + 10)
        func_body = source[func_start:next_func] if next_func != -1 else source[func_start:]

        # Verify yf_tkr initialization exists
        init_pos = func_body.find('yf_tkr = yf.Ticker(')
        self.assertNotEqual(init_pos, -1,
                            "yf_tkr = yf.Ticker(...) not found in _stage_dynamic_exit_candidate — "
                            "regression of commit 85b24a6")

        # Verify initialization comes before option_chain usage
        usage_pos = func_body.find('yf_tkr.option_chain')
        self.assertNotEqual(usage_pos, -1,
                            "yf_tkr.option_chain not found in function body")

        self.assertLess(init_pos, usage_pos,
                        "yf_tkr initialization must come BEFORE yf_tkr.option_chain usage — "
                        "NameError regression (commit 85b24a6)")

    def test_yf_tkr_not_used_before_definition_ast(self):
        """AST-level: verify no Name('yf_tkr') Load node appears before
        the assignment node in the function body. Catches any future
        code shuffling that might reintroduce the NameError."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            source = f.read()

        tree = ast.parse(source)

        # Find the function definition
        func_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == '_stage_dynamic_exit_candidate':
                func_node = node
                break

        self.assertIsNotNone(func_node,
                             "_stage_dynamic_exit_candidate not found via AST")

        # Walk the function body in order, tracking yf_tkr assignment vs usage
        assigned_line = None
        first_use_line = None

        for node in ast.walk(func_node):
            # Assignment: yf_tkr = ...
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == 'yf_tkr':
                        if assigned_line is None:
                            assigned_line = node.lineno

            # Attribute access: yf_tkr.something
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == 'yf_tkr':
                    if first_use_line is None or node.lineno < first_use_line:
                        first_use_line = node.lineno

        self.assertIsNotNone(assigned_line,
                             "yf_tkr assignment not found in function AST — "
                             "regression of commit 85b24a6")
        self.assertIsNotNone(first_use_line,
                             "yf_tkr attribute access not found in function AST")
        self.assertLessEqual(assigned_line, first_use_line,
                             f"yf_tkr used at line {first_use_line} before assignment at "
                             f"line {assigned_line} — NameError regression")


if __name__ == "__main__":
    unittest.main()
