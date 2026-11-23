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

    def test_failed_batch_file_verification_still_stops_the_wave(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("Current contents")
            loop = TreeLoop(
                model=FakeModelClient(["batch end\ncat /repo/note.txt", "finish Reported"]),
                workspace_root=root, max_turns=2, verbose=False,
                tool_dispatcher=lambda result: "end_edit_batch: edit batch verification failed for note.txt",
            )
            result = loop.run("Report the batch result")

            close, read = result.turns[0].results
            self.assertFalse(close.ok)
            self.assertEqual(read.command_type, "skipped")
            self.assertIn("skipped after prior command failure", read.output)

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
            (root / "src").mkdir()
            (root / "src/App.tsx").write_text("\n" * 120 + "const value = composerMode;\n")
            worker = TreeLoopPlannerWorker(model=FakeModelClient([]), root=root, verbose=False)
            worker._observe_command_result("read-line-range /repo/src/App.tsx 121-121", CommandResult(True, "121 | const value = composerMode;", "read"))
            worker.loop.bridge.tree.ingest_diagnostic_content("src/App.tsx(121,15): error TS2304: Cannot find name 'composerMode'.", source_path="check")
            check = CommandResult(True, "command=build\nexit_code=1\nissues=1", "write", tool_action={"type": "run_check", "kind": "build"})
            worker._observe_command_result("run-check build", check)
            self.assertFalse(check.ok)
            self.assertIsNone(worker._discovery_remediation)
            self.assertIsNone(worker._discovery_remediation_blocks_action("replace_lines"))
            self.assertTrue(worker._validation_evidence.failures)

    def test_earlier_edit_keeps_inspected_diagnostic_line_available_in_the_same_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app.py"
            target.write_text("header = 1\n\nvalue =\n")
            model = FakeModelClient([
                "read-line-range /repo/app.py 3-3",
                "replace-lines /repo/app.py:1-1 header = 2\n"
                "replace-lines /repo/app.py:3-3 value = 1\ndiagnose /repo/app.py\nfinish Repaired",
            ])
            worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=2, checkpoint_interval=0, verbose=False)

            result = worker.run_task("Repair both parts of app.py")

            self.assertTrue(result.ok, result.final_message)
            self.assertIn("invalid syntax", worker.history[1].results[0].output)
            self.assertTrue(all(item.ok for item in worker.history[1].results))
            self.assertEqual(target.read_text(), "header = 2\n\nvalue = 1\n")

    def test_shifted_diagnostic_line_requires_fresh_source_before_the_next_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app.py"
            target.write_text("header = 1\n\nvalue =\nnext_value = 2\n")
            model = FakeModelClient([
                "read-line-range /repo/app.py 3-4",
                r'mutate {"type":"replace_range","path":"app.py","start_line":1,"end_line":1,"new_text":"header = 2\n# added"}' "\n"
                "replace-lines /repo/app.py:3-3 value = 1\ndiagnose /repo/app.py",
                "read-line-range /repo/app.py 20-20\nreplace-lines /repo/app.py:3-3 value = 1",
                "read-line-range /repo/app.py 4-4\n"
                "replace-lines /repo/app.py:4-4 value = 1\ndiagnose /repo/app.py\nfinish Repaired",
            ])
            worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=4, checkpoint_interval=0, verbose=False)

            result = worker.run_task("Repair the assignment after updating the header")

            self.assertTrue(result.ok, result.final_message)
            self.assertIn("discovery remediation active", worker.history[1].results[1].output)
            self.assertIn("skipped by runtime", worker.history[1].results[2].output)
            self.assertIn("out of bounds", worker.history[2].results[0].output)
            self.assertIn("discovery remediation active", worker.history[2].results[1].output)
            self.assertEqual(target.read_text(), "header = 2\n# added\n\nvalue = 1\nnext_value = 2\n")

    @unittest.skipUnless(TSC.exists(), "desktop TypeScript compiler is not installed")
    def test_batch_diagnostics_leave_queued_reads_available_and_keep_finish_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "src/stage/runtime.ts"
            target.parent.mkdir(parents=True)
            target.write_text("export const value = 1;\n")
            model = FakeModelClient([
                "batch start\nreplace-lines /repo/src/stage/runtime.ts:1-1 export const value = missingValue;\n"
                "batch end\ndiagnose /repo/src/stage/runtime.ts\nwrite /repo/unrelated.txt held\n"
                "read-line-range /repo/src/stage/runtime.ts 1-1\n"
                "list-run-issues\nfinish Premature",
                "finish Still premature",
                "replace-lines /repo/src/stage/runtime.ts:1-1 export const value = 2;\n"
                "diagnose /repo/src/stage/runtime.ts\nfinish Repaired",
            ])
            with patch.dict(os.environ, {
                "PATH": str(TSC.parent) + os.pathsep + os.environ.get("PATH", ""),
                "SKILLZ_OBSERVABILITY_PATH": str(root / "trace.md"),
            }):
                worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=3, checkpoint_interval=0, verbose=False)
                with patch.object(worker.loop, "_exec_diagnose", wraps=worker.loop._exec_diagnose) as diagnose:
                    result = worker.run_task("Repair and verify the stage runtime")

            self.assertTrue(result.ok, result.final_message)
            self.assertEqual(result.final_message, "Repaired")
            _, _, close, reused, write, read, issues, finish = worker.history[0].results
            self.assertFalse(close.ok)
            self.assertIn("backend_diagnostics engine=tsc path=src/stage/runtime.ts issues=1", close.output)
            self.assertIn("TS2304", close.output)
            self.assertEqual(reused.command_type, "reused")
            self.assertFalse(reused.ok)
            self.assertFalse(reused.needs_tool)
            self.assertIn("Reused the failed batch-end diagnostics above for src/stage/runtime.ts", reused.output)
            diagnose.assert_called_once()
            self.assertTrue(read.ok)
            self.assertIn("missingValue", read.output)
            self.assertTrue(issues.ok)
            self.assertIn("TS2304", issues.output)
            self.assertEqual(write.command_type, "skipped")
            self.assertEqual(finish.command_type, "skipped")
            self.assertIn("held after edit-batch diagnostics", finish.output)
            self.assertIn("finish blocked: Validation failed", worker.history[1].results[0].output)
            self.assertFalse((root / "unrelated.txt").exists())
            self.assertEqual(target.read_text(), "export const value = 2;\n")

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
                "replace-lines src/App.tsx:121-121 const value = 1;\nrun-check build\nfinish Repaired and compiled",
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
            self.assertIs(worker.history[-1].results[1], checks[-1])
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
