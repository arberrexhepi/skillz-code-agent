from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from types import MethodType

from issue_facts import IssueFactLedger
from live_test_loop import TreeLoopPlannerWorker
from tree_commands import CommandResult


class DiscoveryRemediationSatisfierTests(unittest.TestCase):
    def _worker_with_remediation(self) -> TreeLoopPlannerWorker:
        worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
        worker._current_task = "Fix surfaced issue"
        worker._discovery_remediation = {
            "task": "Fix surfaced issue",
            "path": "src/app.py",
            "issue_id": "",
            "diagnostic_issue_id": "",
        }
        worker._refresh_loop_steering = MethodType(lambda self: None, worker)
        return worker

    def test_read_line_range_is_allowed_during_discovery_remediation(self):
        worker = self._worker_with_remediation()

        self.assertIsNone(worker._discovery_remediation_blocks_action("read_line_range"))
        self.assertIsNone(worker._discovery_remediation_blocks_action("read-line-range"))
        self.assertIsNone(worker._discovery_remediation_blocks_action("grep"))

    def test_read_line_range_resolves_discovery_remediation_for_target_path(self):
        worker = self._worker_with_remediation()

        self.assertTrue(
            worker._maybe_resolve_discovery_remediation(
                "read_line_range",
                "/repo/src/app.py",
            )
        )
        self.assertIsNone(worker._discovery_remediation)

    def test_grep_resolves_discovery_remediation_for_target_path(self):
        worker = self._worker_with_remediation()

        self.assertTrue(
            worker._maybe_resolve_discovery_remediation(
                "grep",
                "/repo/src/app.py",
            )
        )
        self.assertIsNone(worker._discovery_remediation)

    def test_grep_command_path_is_extracted_for_read_observer(self):
        worker = self._worker_with_remediation()

        self.assertEqual(
            worker._extract_read_path('grep /repo/src/app.py "TODO" limit=20'),
            "src/app.py",
        )

    def test_strategy_run_issue_progress_preserves_namespace_without_fake_path(self):
        worker = self._worker_with_remediation()
        worker._bridge_step_counter = 0

        payload = worker._command_progress_payload(
            "[s2] show-run-issue run-ts1005-deadbeef01",
            CommandResult(ok=True, output="Run Diagnostic", command_type="read"),
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["action_type"], "show_run_issue")
        self.assertEqual(payload["issue_namespace"], "run")
        self.assertEqual(payload["issue_id"], "run-ts1005-deadbeef01")
        self.assertEqual(payload["path"], "")

    def test_read_line_range_does_not_resolve_other_paths(self):
        worker = self._worker_with_remediation()

        self.assertFalse(
            worker._maybe_resolve_discovery_remediation(
                "read_line_range",
                "/repo/src/other.py",
            )
        )
        self.assertIsNotNone(worker._discovery_remediation)

    def test_diagnose_with_issues_is_failure(self):
        worker = self._worker_with_remediation()

        self.assertTrue(
            worker._message_indicates_failure(
                "diagnose",
                "engine=tsc\npath=src/app.tsx\nissues=1\nexit_code=2",
            )
        )
        self.assertFalse(
            worker._message_indicates_failure(
                "diagnose",
                "engine=tsc\npath=src/app.tsx\nissues=0\nexit_code=0",
            )
        )

    def test_mutation_backend_diagnostics_opens_discovery_remediation(self):
        class FakeTree:
            def list_log_issues(self):
                return [
                    {
                        "id": "run-001",
                        "status": "open",
                        "file": "src/app.py",
                        "classification": "typescript",
                        "summary": "Type error",
                    }
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
            worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
            worker.root = root
            worker._current_task = "Fix type error"
            worker._discovery_remediation = None
            worker._patch_resolution = None
            worker._pending_verification = None
            worker._edit_batch_mode = False
            worker._edit_batch_pending = {}
            worker._edit_batch_last_failure = None
            worker._has_mutation = False
            worker._validation_after_mutation = False
            worker._task_satisfied = False
            worker._goal_skill_mode_pending = False
            worker._goal_skill_mode_used = False
            worker._completion_check_pending = False
            worker._completion_check_reason = ""
            worker.issue_ledger = IssueFactLedger.empty()
            worker.active_issue_id = ""
            worker.loop = SimpleNamespace(
                bridge=SimpleNamespace(tree=FakeTree()),
                _same_turn_halt_reason="",
            )
            worker._refresh_loop_steering = MethodType(lambda self: None, worker)
            worker._emit_step_progress = MethodType(lambda self, command, result: None, worker)

            result = CommandResult(
                ok=True,
                output="replaced lines\nbackend_diagnostics engine=tsc path=src/app.py issues=1",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "replace_lines", "path": "src/app.py"},
            )

            worker._observe_command_result("replace-lines /repo/src/app.py:1-1 print('hi')", result)

        self.assertIsNotNone(worker._discovery_remediation)
        self.assertEqual(worker._discovery_remediation["path"], "src/app.py")

    def test_failed_diagnose_after_mutation_reopens_finish_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
            worker.root = root
            worker._current_task = "Fix type error"
            worker._discovery_remediation = None
            worker._patch_resolution = None
            worker._pending_verification = None
            worker._edit_batch_mode = False
            worker._edit_batch_pending = {}
            worker._edit_batch_last_failure = None
            worker._has_mutation = True
            worker._validation_after_mutation = True
            worker._task_satisfied = False
            worker._goal_skill_mode_pending = False
            worker._goal_skill_mode_used = False
            worker._completion_check_pending = False
            worker._completion_check_reason = ""
            worker.issue_ledger = IssueFactLedger.empty()
            worker.active_issue_id = ""
            worker.loop = SimpleNamespace(
                bridge=SimpleNamespace(tree=SimpleNamespace(list_log_issues=lambda: [])),
                _same_turn_halt_reason="",
            )
            worker._refresh_loop_steering = MethodType(lambda self: None, worker)
            worker._emit_step_progress = MethodType(lambda self, command, result: None, worker)

            result = CommandResult(
                ok=False,
                output="diagnose: engine=tsc\npath=src/app.py\nissues=0\nexit_code=2\nstdout:\nother.ts:1:1 error",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "diagnose", "path": "src/app.py"},
            )

            worker._observe_command_result("diagnose /repo/src/app.py", result)

        self.assertFalse(worker._validation_after_mutation)
        self.assertEqual(worker._pending_verification["source"], "diagnose")
        self.assertIn("diagnose failed after mutation", worker._completion_check_reason)


if __name__ == "__main__":
    unittest.main()
