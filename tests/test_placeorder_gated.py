"""AST guard: every placeOrder call must be preceded by assert_execution_enabled."""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestPlaceOrderGated(unittest.TestCase):
    """Every ib_conn.placeOrder() call in telegram_bot.py must be preceded
    by assert_execution_enabled() in the same function body.

    Walk strategy:
    1. Parse telegram_bot.py AST
    2. Find all FunctionDef nodes
    3. Within each function, find all Call nodes where func.attr == "placeOrder"
    4. For each placeOrder call, verify there exists a Call to
       "assert_execution_enabled" at an earlier lineno in the same function
    5. Fail if any placeOrder is ungated
    """

    def test_all_placeorder_calls_gated(self):
        bot_path = os.path.join(
            os.path.dirname(__file__), "..", "telegram_bot.py"
        )
        with open(bot_path, encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())

        ungated = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Collect all placeOrder call line numbers in this function
            place_order_lines = []
            assert_lines = []

            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func

                # Match: *.placeOrder(...)
                if isinstance(func, ast.Attribute) and func.attr == "placeOrder":
                    place_order_lines.append(child.lineno)

                # Match: assert_execution_enabled(...)
                if isinstance(func, ast.Name) and func.id == "assert_execution_enabled":
                    assert_lines.append(child.lineno)

            # For each placeOrder, verify an assert_execution_enabled exists
            # at an earlier line in the same function
            for po_line in place_order_lines:
                has_gate = any(al < po_line for al in assert_lines)
                if not has_gate:
                    ungated.append(
                        f"{node.name}() line {po_line}: placeOrder without "
                        f"assert_execution_enabled"
                    )

        self.assertEqual(
            ungated, [],
            f"Ungated placeOrder calls in telegram_bot.py:\n"
            + "\n".join(ungated)
        )

    def test_at_least_three_placeorder_sites(self):
        """Sanity: confirm we find the expected number of placeOrder calls."""
        bot_path = os.path.join(
            os.path.dirname(__file__), "..", "telegram_bot.py"
        )
        with open(bot_path, encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())

        count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "placeOrder":
                    count += 1

        self.assertGreaterEqual(
            count, 3,
            f"Expected at least 3 placeOrder calls, found {count}"
        )


if __name__ == "__main__":
    unittest.main()
