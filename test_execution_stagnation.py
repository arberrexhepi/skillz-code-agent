from __future__ import annotations

import unittest
from types import MethodType, SimpleNamespace

from live_test_loop import (
    EXECUTION_EXPLORATION_ACTION_LIMIT,
    TreeLoopPlannerWorker,
)
from tree_commands import CommandResult


class ExecutionStagnationGuardTests(unittest.TestCase):
    def _worker(self) -> TreeLoopPlannerWorker:
        worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
        worker.discovery_budget = None
        worker._execution_non_mutating_actions = 0
        worker._execution_empty_searches = 0
        worker._execution_stagnation = None
        worker.loop = SimpleNamespace(_recent_reads=[], _same_turn_halt_reason="")
        worker._refresh_loop_steering = MethodType(lambda self: None, worker)
        return worker

    def test_three_empty_searches_enter_execution_recovery(self):
        worker = self._worker()

        for index in range(3):
            worker._track_execution_progress(
                f'grep /repo/src "missing-{index}"',
                CommandResult(ok=True, output="(no matches)", command_type="read"),
            )

        self.assertIsNotNone(worker._execution_stagnation)
        assert worker._execution_stagnation is not None
        self.assertEqual(worker._execution_stagnation["trigger"], "empty_searches")
        self.assertIn("execution recovery active", worker.loop._same_turn_halt_reason)

    def test_sustained_reads_enter_execution_recovery_without_empty_results(self):
        worker = self._worker()

        for index in range(EXECUTION_EXPLORATION_ACTION_LIMIT):
            worker._track_execution_progress(
                f"cat /repo/src/file-{index}.ts",
                CommandResult(ok=True, output="export const value = 1", command_type="read"),
            )

        self.assertIsNotNone(worker._execution_stagnation)
        assert worker._execution_stagnation is not None
        self.assertEqual(worker._execution_stagnation["trigger"], "non_mutating_actions")

    def test_recovery_allows_one_focused_read_then_blocks_exploration(self):
        worker = self._worker()
        stop_reasons = []
        worker.loop.request_stop = stop_reasons.append
        worker._execution_stagnation = {
            "reason": "test",
            "focused_reads_used": 0,
            "blocked_actions": 0,
        }

        first_read = CommandResult(ok=True, output="source", command_type="read")
        self.assertFalse(worker._apply_execution_stagnation_guard("cat /repo/src/app.ts", first_read))
        self.assertTrue(first_read.ok)

        search = CommandResult(ok=True, output="(no matches)", command_type="read")
        self.assertTrue(worker._apply_execution_stagnation_guard('grep /repo/src "other"', search))
        self.assertFalse(search.ok)
        self.assertIn("blocked more exploration", search.output)
        self.assertEqual(stop_reasons, [])

        for _ in range(2):
            repeated_search = CommandResult(ok=True, output="(no matches)", command_type="read")
            worker._apply_execution_stagnation_guard('grep /repo/src "other"', repeated_search)

        self.assertEqual(len(stop_reasons), 1)
        self.assertIn("recovery exhausted", stop_reasons[0])

    def test_successful_mutation_clears_recovery_state(self):
        worker = self._worker()
        worker._execution_non_mutating_actions = 20
        worker._execution_empty_searches = 4
        worker._execution_stagnation = {"reason": "test"}

        worker._track_execution_progress(
            "patch /repo/src/app.ts old -> new",
            CommandResult(
                ok=True,
                output="patched",
                command_type="write",
                needs_tool=True,
                tool_action={"type": "patch_file", "path": "src/app.ts"},
            ),
        )

        self.assertIsNone(worker._execution_stagnation)
        self.assertEqual(worker._execution_non_mutating_actions, 0)
        self.assertEqual(worker._execution_empty_searches, 0)

    def test_discovery_budget_disables_execution_guard(self):
        worker = self._worker()
        worker.discovery_budget = SimpleNamespace()

        for _ in range(EXECUTION_EXPLORATION_ACTION_LIMIT + 2):
            worker._track_execution_progress(
                'grep /repo/src "missing"',
                CommandResult(ok=True, output="(no matches)", command_type="read"),
            )

        self.assertIsNone(worker._execution_stagnation)


if __name__ == "__main__":
    unittest.main()
