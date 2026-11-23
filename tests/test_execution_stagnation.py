from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from types import MethodType, SimpleNamespace

from live_test_loop import (
    EXECUTION_EXPLORATION_ACTION_LIMIT,
    TreeLoopPlannerWorker,
)
from tree_commands import CommandResult
from execution_evidence import repository_observation


class ExecutionStagnationGuardTests(unittest.TestCase):
    def _worker(self) -> TreeLoopPlannerWorker:
        worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
        worker.discovery_budget = None
        worker._execution_non_mutating_actions = 0
        worker._execution_empty_searches = 0
        worker._execution_seen_evidence = set()
        worker._execution_stagnation = None
        worker._current_task = "Fix Quick Start"
        worker._patch_resolution = None
        worker._pending_verification = None
        worker._discovery_remediation = None
        worker._goal_skill_mode_pending = False
        worker.loop = SimpleNamespace(_recent_reads=[], _same_turn_halt_reason="")
        worker._refresh_loop_steering = MethodType(lambda self: None, worker)
        worker._emit_step_progress = lambda command, result: None
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

    def test_new_source_reads_do_not_enter_execution_recovery(self):
        worker = self._worker()

        for index in range(EXECUTION_EXPLORATION_ACTION_LIMIT):
            worker._track_execution_progress(
                f"cat /repo/src/file-{index}.ts",
                CommandResult(ok=True, output="export const value = 1", command_type="read"),
            )

        self.assertIsNone(worker._execution_stagnation)
        self.assertEqual(worker._execution_non_mutating_actions, 0)

    def test_repeated_unchanged_reads_enter_execution_recovery(self):
        worker = self._worker()
        for index in range(EXECUTION_EXPLORATION_ACTION_LIMIT + 1):
            worker._track_execution_progress(
                f"[s{index}] cat /repo/src/app.ts",
                CommandResult(ok=True, output="unchanged source", command_type="read"),
            )

        assert worker._execution_stagnation is not None
        self.assertEqual(worker._execution_stagnation["trigger"], "non_mutating_actions")
        self.assertIn("without new evidence", worker._execution_stagnation["reason"])

    def test_recovery_allows_one_focused_read_then_blocks_exploration(self):
        worker = self._worker()
        stop_reasons = []
        worker.loop.request_delivery_attempt = stop_reasons.append
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
        self.assertIn("blocked repeated or empty exploration", search.output)
        self.assertEqual(stop_reasons, [])

        for _ in range(2):
            repeated_search = CommandResult(ok=True, output="(no matches)", command_type="read")
            worker._apply_execution_stagnation_guard('grep /repo/src "other"', repeated_search)

        self.assertEqual(len(stop_reasons), 1)
        self.assertIn("recovery exhausted", stop_reasons[0])

    def test_repeated_focused_read_allowance_remains_bounded(self):
        worker = self._worker()
        command = "cat /repo/src/app.ts"
        result = CommandResult(ok=True, output="source", command_type="read")
        worker._track_execution_progress(command, result)
        worker._execution_stagnation = {"focused_reads_used": 0}
        self.assertFalse(worker._apply_execution_stagnation_guard(command, result))
        self.assertEqual(worker._execution_stagnation["focused_reads_used"], 1)
        self.assertTrue(worker._apply_execution_stagnation_guard(command, result))

    def test_new_evidence_resumes_execution_even_after_read_allowance_consumed(self):
        for command, output, action in [
            ("cat /repo/src/components/WorkspaceQuickStart.tsx", "source", None),
            ("read-line-range /repo/src/App.tsx 14040-14095", "14040: source", None),
            ('find /repo -name "useAssistantConversation*"', "repo/src/assistant/useAssistantConversation.ts", None),
            ('discover {"type":"read_file","path":"/repo/src/app.ts"}', '{"ok":true,"content":"source"}', {"type": "read_file", "path": "/repo/src/app.ts"}),
        ]:
            with self.subTest(command=command):
                worker = self._worker()
                worker._execution_stagnation = {"focused_reads_used": 1, "blocked_actions": 2}
                result = CommandResult(ok=True, output=output, command_type="read", tool_action=action)
                worker._observe_command_result(command, result)
                self.assertTrue(result.ok)
                self.assertIsNone(worker._execution_stagnation)

    def test_failures_keep_the_original_diagnostic(self):
        worker = self._worker()
        worker._execution_stagnation = {"focused_reads_used": 1, "blocked_actions": 0}
        for command, command_type, action in [
            ("patch /repo/src/app.ts old -> new", "write", {"type": "patch_file", "path": "src/app.ts"}),
            ("cat /repo/src/missing.ts", "error", None),
            ("run-check typecheck", "write", {"type": "run_check"}),
        ]:
            with self.subTest(command=command):
                result = CommandResult(ok=False, output="original diagnostic", command_type=command_type, tool_action=action)
                self.assertFalse(worker._apply_execution_stagnation_guard(command, result))
                self.assertFalse(result.ok)
                self.assertEqual(result.output, "original diagnostic")
                self.assertEqual(worker._execution_stagnation["blocked_actions"], 0)

    def test_failed_patch_then_same_source_read_recovers_with_exhausted_allowance(self):
        worker = self._worker()
        command = "[s1] cat /repo/src/components/WorkspaceQuickStart.tsx"
        source = CommandResult(ok=True, output="original source", command_type="read")
        worker._observe_command_result(command, source)
        worker._execution_stagnation = {"focused_reads_used": 1, "blocked_actions": 2}
        failed_patch = CommandResult(
            ok=False, output="patch failed: search text not found", command_type="write",
            tool_action={"type": "patch_file", "path": "src/components/WorkspaceQuickStart.tsx"},
        )
        worker._observe_command_result("patch /repo/src/components/WorkspaceQuickStart.tsx old -> new", failed_patch)
        self.assertEqual(worker._patch_resolution["reason"], "patch failed: search text not found")
        self.assertEqual(worker._execution_stagnation["blocked_actions"], 2)

        worker.loop._same_turn_halt_reason = ""  # Next model turn.
        worker._observe_command_result(command, source)
        self.assertTrue(source.ok)
        self.assertIsNone(worker._patch_resolution)
        self.assertIsNone(worker._execution_stagnation)
        self.assertEqual(worker.loop._same_turn_halt_reason, "")

    def test_empty_search_count_resets_on_new_evidence_not_skill_loading(self):
        worker = self._worker()
        empty = CommandResult(ok=True, output="(no matches)", command_type="read")
        worker._track_execution_progress('grep /repo "missing"', empty)
        worker._track_execution_progress('grep /repo "missing"', empty)
        for _ in range(20):
            worker._track_execution_progress("skill codebase-discovery", CommandResult(ok=True, output="instructions", command_type="read"))
        self.assertIsNone(worker._execution_stagnation)
        self.assertEqual(worker._execution_empty_searches, 2)
        worker._track_execution_progress('grep /repo "found"', CommandResult(ok=True, output="repo/src/app.ts:1: found", command_type="read"))
        worker._track_execution_progress('grep /repo "missing"', empty)
        self.assertIsNone(worker._execution_stagnation)
        self.assertEqual(worker._execution_empty_searches, 1)

    def test_json_discovery_empty_hits_count_even_with_query_metadata(self):
        worker = self._worker()
        for index in range(3):
            action = {"type": "find_symbol_definitions", "path": "/repo", "symbol_name": f"missing{index}"}
            result = CommandResult(ok=True, output=json.dumps({"ok": True, "query": f"missing{index}", "hits": [], "summary": {"count": 0}}), command_type="read", tool_action=action)
            worker._track_execution_progress("discover " + json.dumps(action), result)
        self.assertEqual(worker._execution_stagnation["trigger"], "empty_searches")

    def test_changed_source_is_progress_but_alternating_old_reads_are_not(self):
        worker = self._worker()
        for content in ("old source", "new source"):
            worker._track_execution_progress("cat /repo/src/app.ts", CommandResult(ok=True, output=content, command_type="read"))
            self.assertEqual(worker._execution_non_mutating_actions, 0)
        for index in range(EXECUTION_EXPLORATION_ACTION_LIMIT):
            worker._track_execution_progress("cat /repo/src/app.ts", CommandResult(ok=True, output=("old source", "new source")[index % 2], command_type="read"))
        self.assertIsNotNone(worker._execution_stagnation)

    def test_new_line_ranges_keep_source_inspection_open(self):
        worker = self._worker()
        for index in range(EXECUTION_EXPLORATION_ACTION_LIMIT + 2):
            command = f"read-line-range /repo/src/App.tsx {index + 1}-{index + 1}"
            result = CommandResult(ok=True, output=f"{index + 1} | next source line", command_type="read")
            worker._observe_command_result(command, result)
            self.assertTrue(result.ok)
            self.assertIsNone(worker._execution_stagnation)

    def test_shifted_overlapping_windows_do_not_reset_no_progress(self):
        worker = self._worker()
        for offset in range(EXECUTION_EXPLORATION_ACTION_LIMIT + 2):
            output = "\n".join(f"{line} | source line {line}" for line in range(1 + offset, 101 + offset))
            worker._track_execution_progress(f"read-line-range /repo/src/App.tsx {1 + offset}-{100 + offset}",
                                             CommandResult(ok=True, output=output, command_type="read"))
        self.assertIsNotNone(worker._execution_stagnation)

    def test_changed_line_resets_recovery_inside_an_overlapping_window(self):
        worker = self._worker()
        original = "\n".join(f"{line} | source line {line}" for line in range(1, 101))
        worker._track_execution_progress("cat /repo/src/App.tsx", CommandResult(ok=True, output=original, command_type="read"))
        worker._execution_stagnation = {"focused_reads_used": 1}
        changed = original.replace("50 | source line 50", "50 | repaired source")
        worker._observe_command_result("cat /repo/src/App.tsx", CommandResult(ok=True, output=changed, command_type="read"))
        self.assertIsNone(worker._execution_stagnation)

    def test_context_and_line_reads_share_source_coverage(self):
        worker = self._worker()
        content = "\n".join(f"line {line}" for line in range(1, 101))
        result = CommandResult(ok=True, output=json.dumps({"ok": True, "blocks": [{"start_line": 1, "content": content}]}),
                               command_type="read", tool_action={"type": "read_context", "path": "/repo/src/App.tsx"})
        worker._track_execution_progress('read-context /repo/src/App.tsx "line"', result)
        for offset in range(EXECUTION_EXPLORATION_ACTION_LIMIT):
            output = "\n".join(f"{line} | line {line}" for line in range(1 + offset, 81 + offset))
            worker._track_execution_progress(f"read-line-range /repo/src/App.tsx {1+offset}-{80+offset}",
                                             CommandResult(ok=True, output=output, command_type="read"))
        self.assertIsNotNone(worker._execution_stagnation)

    def test_undelivered_source_survives_recovery_with_no_read_allowance(self):
        from model_result_delivery import prepare_model_result

        worker = self._worker()
        command = "read-line-range /repo/src/App.tsx 1-400"
        result = CommandResult(True, "\n".join(f"{n} | source {n} {'x' * 80}" for n in range(1, 401)), "read")
        prepare_model_result(command, result, limit=5000)
        worker._track_execution_progress(command, result)
        worker._execution_stagnation = {"focused_reads_used": 1, "blocked_actions": 2}
        tail = CommandResult(True, "\n".join(f"{n} | source {n} {'x' * 80}" for n in range(301, 401)), "read")
        worker._observe_command_result("read-line-range /repo/src/App.tsx 301-400", tail)
        self.assertTrue(tail.ok)
        self.assertIsNone(worker._execution_stagnation)

    def test_evicted_source_survives_recovery_but_retained_repeats_do_not(self):
        worker = self._worker()
        old = ("cat /repo/src/old.ts", CommandResult(True, "1 | old source", "read"))
        retained = ("cat /repo/src/current.ts", CommandResult(True, "1 | current source", "read"))
        worker._track_execution_progress(*old)
        worker._track_execution_progress(*retained)
        worker._replace_retained_read_context([retained])
        worker._execution_stagnation = {"focused_reads_used": 1, "blocked_actions": 0}
        self.assertTrue(worker._apply_execution_stagnation_guard(*retained))
        self.assertFalse(retained[1].ok)
        worker._observe_command_result(*old)
        self.assertTrue(old[1].ok)
        self.assertIsNone(worker._execution_stagnation)

    def test_recovery_guidance_targets_repairs_instead_of_novelty(self):
        worker = self._worker()
        worker._edit_batch_mode = False
        worker._edit_batch_pending = {}
        worker._completion_check_pending = False
        worker._execution_stagnation = {"reason": "repeated retained source"}
        steps = " ".join(worker._active_mode_strategy()["steps"])
        self.assertIn("current diagnostics", steps)
        self.assertIn("exact symbol", steps)
        self.assertNotIn("new evidence resumes", steps)

    def test_typed_read_resolves_patch_recovery(self):
        worker = self._worker()
        worker._patch_resolution = {"path": "src/app.ts"}
        worker._execution_stagnation = {"focused_reads_used": 1}
        result = CommandResult(
            ok=True, output='{"ok":true,"content":"source"}', command_type="read",
            tool_action={"type": "read_file", "file_path": "/repo/src/app.ts"},
        )
        worker._observe_command_result("discover {}", result)
        self.assertIsNone(worker._patch_resolution)
        self.assertIsNone(worker._execution_stagnation)

    def test_real_discovery_and_cold_source_reads_survive_empty_search_recovery(self):
        from context_tree import ContextTree
        from tree_commands import TreeCommandParser

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "WorkspaceQuickStart.tsx").write_text("export const WorkspaceQuickStart = () => null;\n")
            (root / "README.md").write_text("Initially indexed metadata\n")
            tree = ContextTree(root)
            tree.index_repo(max_files=1)
            parser = TreeCommandParser(tree)
            worker = self._worker()
            for command in [
                'find /repo -name "missing1*"',
                'find /repo -name "missing2*"',
                'find /repo -name "missing3*"',
                'find /repo -name "WorkspaceQuickStart*"',
                "cat /repo/src/WorkspaceQuickStart.tsx",
            ]:
                result = parser.parse_and_execute(command)
                worker._observe_command_result(command, result)
                self.assertTrue(result.ok, result.output)
            self.assertIsNone(worker._execution_stagnation)

    def test_evidence_ignores_virtual_mounts_and_query_only_changes(self):
        self.assertIsNone(repository_observation("cat /facts/note", CommandResult(ok=True, output="fact", command_type="read")))
        result = CommandResult(ok=True, output="repo/src/app.ts:1: same hit", command_type="read")
        self.assertEqual(
            repository_observation('[s1] grep /repo "one"', result).fingerprint,
            repository_observation('[s9] grep /repo "two"', result).fingerprint,
        )
        self.assertFalse(repository_observation("cat /repo/empty.json", CommandResult(ok=True, output="[]", command_type="read")).empty_search)
        for output in ("[not found: /repo/missing]", "[line range out of bounds: /repo/a:90-100 (2 total lines)]"):
            self.assertIsNone(repository_observation("cat /repo/missing", CommandResult(ok=True, output=output, command_type="read")))

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
