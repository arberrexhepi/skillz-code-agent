from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution_evidence import repository_observation
from execution_progress import ExecutionProgress, READ_ONLY_RECOVERY_TURNS, RECOVERY_TURN_ALLOWANCE
from live_test_loop import TreeLoopPlannerWorker
from tests.test_live_test_loop import FakeModelClient
from tree_commands import CommandResult


class ExecutionProgressTests(unittest.TestCase):
    def test_fresh_and_evicted_ranges_do_not_renew_delivery_allowance(self):
        progress = ExecutionProgress()
        for index in range(READ_ONLY_RECOVERY_TURNS):
            command = f"read-line-range /repo/App.tsx {index * 10 + 1}-{index * 10 + 10}"
            result = CommandResult(True, "\n".join(f"{n} | source" for n in range(index * 10 + 1, index * 10 + 11)), "read")
            progress.observe_turn([command], [result])
            progress.context_replacements += 1
        self.assertTrue(progress.recovering)
        self.assertEqual(progress.repository_reads, READ_ONLY_RECOVERY_TURNS)
        self.assertEqual(progress.state()["source_lines_returned"]["App.tsx"], 80)
        self.assertIn("does not reset", progress.guidance())

    def test_recovery_has_two_small_reads_not_another_broad_scan(self):
        progress = ExecutionProgress(read_only_turns=READ_ONLY_RECOVERY_TURNS)
        broad = CommandResult(True, "\n".join(f"{n} | source" for n in range(1, 301)), "read")
        command = "read-line-range /repo/App.tsx 1-300"
        self.assertFalse(progress.allow_focused_read(command, repository_observation(command, broad), broad))
        small = CommandResult(True, "1 | required anchor", "read")
        for _ in range(2):
            self.assertTrue(progress.allow_focused_read(command, repository_observation(command, small), small))
        self.assertFalse(progress.allow_focused_read(command, repository_observation(command, small), small))
        search = CommandResult(True, "a.ts:1: hit", "read")
        progress.focused_reads_used = 0
        self.assertFalse(progress.allow_focused_read('grep /repo "hit"', repository_observation('grep /repo "hit"', search), search))

    def test_edits_reset_the_delivery_phase_but_passing_checks_do_not(self):
        progress = ExecutionProgress(read_only_turns=READ_ONLY_RECOVERY_TURNS)
        check = CommandResult(True, "exit_code=0", "mutation", tool_action={"type": "run_check", "kind": "build"})
        progress.observe_turn(["run-check build"], [check])
        self.assertEqual(progress.read_only_turns, READ_ONLY_RECOVERY_TURNS + 1)
        progress.observe_turn(["patch App.tsx"], [CommandResult(False, "anchor mismatch", "mutation")], edit_attempted=True)
        self.assertEqual(progress.read_only_turns, 0)
        self.assertFalse(progress.recovering)

    def test_live_worker_bounds_the_motion_studio_read_eviction_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "App.tsx").write_text("\n".join(f"const value{n} = '{'x' * 65}';" for n in range(1, 2657)))
            ranges = [(1, 300), (301, 600), (1460, 1800), (1801, 2145), (601, 760), (1801, 2250), (2251, 2656)]
            responses = [
                ">>th: One more read, then immediately apply the fix.\n>>pl: Read missing context.\n"
                + f"read-line-range /repo/src/App.tsx {start}-{end}"
                for start, end in (ranges * 8)
            ]
            model = FakeModelClient(responses)
            with patch.dict(os.environ, {"SKILLZ_OBSERVABILITY_PATH": str(root / "trace.md")}):
                worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=48, checkpoint_interval=0, verbose=False)
                worker.loop._conversation_char_budget = 70000  # Force the same working-set churn.
                result = worker.run_task("Repair the Creative Dock in the bounded target")
            expected = READ_ONLY_RECOVERY_TURNS + RECOVERY_TURN_ALLOWANCE
            self.assertEqual(len(model.calls), expected)
            self.assertFalse(result.ok)
            self.assertFalse(worker._has_mutation)
            self.assertIn("execution delivery stalled", result.final_message)
            self.assertIn(f"{expected} execution turns", result.final_message)
            self.assertGreater(worker._execution_delivery.context_replacements, 0)
            self.assertIn("EXECUTION DELIVERY CHECKPOINT", model.calls[4]["prompt"])
            self.assertIn("EXECUTION DELIVERY RECOVERY", model.calls[8]["prompt"])
            self.assertIn("Host-retained context manifest", model.calls[8]["prompt"])
            self.assertIn('"execution_delivery"', (root / "trace.md").read_text())

    def test_read_only_answer_and_a_grounded_edit_remain_possible_in_recovery(self):
        for final in ("finish Inspection findings are ready", "replace-lines note.txt:1-1 updated\nfinish Updated"):
            with self.subTest(final=final), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "note.txt").write_text("original\n")
                responses = ["stat /repo/note.txt"] * READ_ONLY_RECOVERY_TURNS + [final, "cat /repo/note.txt\nfinish Updated"]
                model = FakeModelClient(responses)
                with patch.dict(os.environ, {"SKILLZ_OBSERVABILITY_PATH": str(root / "trace.md")}):
                    worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=14, checkpoint_interval=0, verbose=False)
                    worker._goal_skill_mode_pending = False
                    result = worker.run_task("Inspect or update the note")
                self.assertTrue(result.ok, result.final_message)
                if final.startswith("replace-lines"):
                    self.assertEqual((root / "note.txt").read_text(), "updated\n")
                    self.assertEqual(worker._execution_delivery.read_only_turns, 0)

    def test_failed_edit_diagnostic_remains_visible_during_delivery_recovery(self):
        worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
        worker.discovery_budget = None
        worker._execution_delivery = ExecutionProgress(read_only_turns=READ_ONLY_RECOVERY_TURNS)
        result = CommandResult(False, "original patch mismatch diagnostic", "mutation", tool_action={"type": "patch_file"})
        self.assertFalse(worker._apply_execution_stagnation_guard("patch App.tsx", result))
        self.assertEqual(result.output, "original patch mismatch diagnostic")
        self.assertEqual(worker._execution_delivery.focused_reads_used, 0)

    def test_two_focused_reads_can_ground_an_edit_before_the_recovery_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("original\n")
            model = FakeModelClient(["stat /repo/note.txt"] * READ_ONLY_RECOVERY_TURNS + [
                "cat /repo/note.txt",
                "read-line-range /repo/note.txt 1-1",
                "replace-lines note.txt:1-1 updated",
                "cat /repo/note.txt\nfinish Updated",
            ])
            with patch.dict(os.environ, {"SKILLZ_OBSERVABILITY_PATH": str(root / "trace.md")}):
                worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=14, checkpoint_interval=0, verbose=False)
                result = worker.run_task("Update the note after focused inspection")
            self.assertTrue(result.ok, result.final_message)
            self.assertEqual((root / "note.txt").read_text(), "updated\n")
            self.assertEqual(worker._execution_delivery.read_only_turns, 0)

    def test_discovery_phase_keeps_its_separate_budget(self):
        worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
        worker.discovery_budget = object()
        worker._execution_delivery = ExecutionProgress()
        worker._observe_execution_turn(None)
        self.assertEqual(worker._execution_delivery.read_only_turns, 0)


if __name__ == "__main__":
    unittest.main()
