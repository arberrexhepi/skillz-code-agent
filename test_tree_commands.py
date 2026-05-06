from __future__ import annotations

import unittest

from tree_commands import CommandResult, StrategyStep, _evaluate_placeholder_expression


class PlaceholderTransformTests(unittest.TestCase):
    def test_match_transform_allows_parentheses_inside_regex(self):
        steps = {
            "s1": StrategyStep(
                label="s1",
                commands=["fact demo"],
                results=[CommandResult(ok=True, output="route check failed: 503 service unavailable", command_type="read")],
            )
        }

        value = _evaluate_placeholder_expression(
            "s1.match(/failed: (\\d+) service unavailable/)[1]",
            steps,
        )

        self.assertEqual(value, "503")

    def test_match_transform_allows_escaped_closing_paren_inside_regex(self):
        steps = {
            "s1": StrategyStep(
                label="s1",
                commands=["fact demo"],
                results=[CommandResult(ok=True, output="error: expected call foo)", command_type="read")],
            )
        }

        value = _evaluate_placeholder_expression(
            r"s1.match(/call foo\)/)[0]",
            steps,
        )

        self.assertEqual(value, "call foo)")


if __name__ == "__main__":
    unittest.main()
