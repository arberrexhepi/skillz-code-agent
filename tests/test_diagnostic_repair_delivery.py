from __future__ import annotations

import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution_progress import ExecutionProgress
from live_test_loop import TreeLoopPlannerWorker
from tests.test_live_test_loop import FakeModelClient
from tree_commands import CommandResult
from tree_loop import TreeLoop


TSC = Path(__file__).resolve().parents[1] / "desktop/node_modules/.bin/tsc"
ISSUE = {"id": "run-test", "file": "src/App.tsx", "line": "121", "column": "15", "code": "TS2304", "message": "Cannot find name 'composerMode'."}


class DiagnosticRepairDeliveryTests(unittest.TestCase):
    def test_check_exit_codes_and_findings_agree_across_raw_and_worker_loops(self):
        for kind in ("run_check", "diagnose", "run_route_check"):
            for output, expected in (("command=tool\nexit_code=1\nissues=0", True),
                                     ("exit_code=0\nissues=1", True), ("exit_code=0\nissues=0", False)):
                with self.subTest(kind=kind, output=output):
                    worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
                    self.assertEqual(TreeLoop._tool_message_indicates_failure(kind, output), expected)
                    self.assertEqual(worker._message_indicates_failure(kind, output), expected)

    def test_custom_dispatch_cannot_report_a_failed_build_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop = TreeLoop(model=FakeModelClient(["run-check build"]), workspace_root=Path(tmp), max_turns=1,
                            verbose=False, tool_dispatcher=lambda result: "command=build\nexit_code=1\nissues=0")
            result = loop.run("Validate")
            self.assertFalse(result.turns[0].results[0].ok)

    def test_new_diagnostic_gets_one_repair_window_but_repeated_check_does_not_renew_it(self):
        progress = ExecutionProgress(read_only_turns=9, focused_reads_used=2)
        self.assertTrue(progress.focus_diagnostic(ISSUE))
        deadline = progress.diagnostic_repair.deadline
        self.assertEqual(deadline, 13)
        progress.read_only_turns = 11
        self.assertFalse(progress.exhausted)
        self.assertFalse(progress.focus_diagnostic({**ISSUE, "id": "run-reingested", "source": "new check"}))
        self.assertEqual(progress.diagnostic_repair.deadline, deadline)
        progress.read_only_turns = 13
        self.assertTrue(progress.exhausted)
        self.assertIn("src/App.tsx:121", progress.stop_reason())

    def test_retained_diagnostic_source_does_not_require_a_duplicate_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = TreeLoopPlannerWorker(model=FakeModelClient([]), root=root, verbose=False)
            worker._observe_command_result("read-line-range /repo/src/App.tsx 121-121", CommandResult(True, "121 | const value = composerMode;", "read"))
            worker.loop.bridge.tree.ingest_diagnostic_content("src/App.tsx(121,15): error TS2304: Cannot find name 'composerMode'.", source_path="check")
            check = CommandResult(True, "command=build\nexit_code=1\nissues=1", "write", tool_action={"type": "run_check", "kind": "build"})
            worker._observe_command_result("run-check build", check)
            self.assertFalse(check.ok)
            self.assertIsNone(worker._discovery_remediation)
            self.assertIsNone(worker._discovery_remediation_blocks_action("replace_lines"))
            self.assertTrue(worker._validation_evidence.failures)

    @unittest.skipUnless(TSC.exists(), "desktop TypeScript compiler is not installed")
    def test_real_failed_typescript_build_can_ground_repair_after_the_read_allowance_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            target = root / "src/App.tsx"
            target.write_text("\n".join(["// existing studio code"] * 120 + ["const value = composerMode;"] + ["// existing studio code"] * 200) + "\n")
            (root / "style.css").write_text("body { margin: 0 }\n")
            model = FakeModelClient(["stat /repo/src/App.tsx"] * 8 + [
                "read-line-range /repo/src/App.tsx 1-5\nread-line-range /repo/style.css 1-1",
                "run-check build",
                "read-line-range /repo/src/App.tsx 113-133",
                "replace-lines src/App.tsx:121-121 const value = 1;",
                "run-check build\nfinish Repaired and compiled",
            ])
            with patch.dict(os.environ, {"SKILLZ_OBSERVABILITY_PATH": str(root / "trace.md")}):
                worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=16, checkpoint_interval=0, verbose=False)
                command = shlex.quote(str(TSC)) + " --noEmit --skipLibCheck --target es2022 src/App.tsx"
                with patch.object(worker.loop, "_detect_check_command", return_value=command):
                    result = worker.run_task("Repair the compiler error using bounded context")
            self.assertTrue(result.ok, result.final_message)
            self.assertTrue(result.validation.passed)
            checks = [item for turn in worker.history for item in turn.results if (item.tool_action or {}).get("type") == "run_check"]
            self.assertEqual([check.ok for check in checks], [False, True])
            self.assertIn("TS2304", checks[0].output)
            self.assertIn("exit_code=0", checks[-1].output)
            repair_prompt = model.calls[10]["prompt"]
            self.assertIn("DIAGNOSTIC REPAIR", repair_prompt)
            self.assertIn("read-line-range /repo/src/App.tsx 113-133", repair_prompt)
            self.assertIn("const value = 1;", target.read_text())

    def test_format_repair_keeps_edit_intent_and_archives_the_rejected_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("original\n")
            rejected = "I will replace original with updated in note.txt now."
            model = FakeModelClient([rejected, 'patch note.txt "original" -> "updated"', "cat /repo/note.txt\nfinish Updated"])
            with patch.dict(os.environ, {"SKILLZ_OBSERVABILITY_PATH": str(root / "trace.md")}):
                worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=3, checkpoint_interval=0, verbose=False)
                result = worker.run_task("Update the note")
            self.assertTrue(result.ok, result.final_message)
            self.assertIn("emit that edit", model.calls[1]["prompt"])
            trace = (root / "trace.md").read_text()
            self.assertIn("Model output format repair", trace)
            self.assertIn(rejected, trace)


if __name__ == "__main__":
    unittest.main()
