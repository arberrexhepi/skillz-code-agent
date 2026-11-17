from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from live_test_loop import TreeLoopPlannerWorker
from tests.test_live_test_loop import FakeModelClient
from tree_commands import CommandResult
from validation_evidence import ValidationEvidence


def check(ok: bool, kind: str = "diagnose", scope: str = "broken.py") -> CommandResult:
    field = {"diagnose": "path", "run_check": "kind", "run_shell": "command"}[kind]
    return CommandResult(ok, "issues=0" if ok else "check failed: unresolved error", "mutation",
                         tool_action={"type": kind, field: scope})


class ValidationEvidenceTests(unittest.TestCase):
    def test_read_only_failed_diagnostic_stays_failed_through_inspection_and_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.py").write_text("def broken(:\n    pass\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["diagnose /repo/broken.py", "cat /repo/broken.py", "finish done"]),
                root=root, max_turns=3, checkpoint_interval=0, verbose=False,
            )
            result = worker.run_task("Inspect the failing source")
            self.assertFalse(worker._has_mutation)
            self.assertFalse(result.ok)
            self.assertFalse(result.validation.passed)
            self.assertIn("Validation failed", result.validation.summary)
            self.assertIn("broken.py", result.validation.summary)
            self.assertNotIn("No mutating actions", result.validation.summary)
            self.assertIn("finish blocked", worker.history[-1].results[-1].output)
            self.assertIsNone(worker._discovery_remediation)

    def test_matching_check_clears_failure_but_reads_and_unrelated_checks_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = TreeLoopPlannerWorker(model=FakeModelClient([]), root=Path(tmp), verbose=False)
            worker._observe_command_result("diagnose /repo/broken.py", check(False))
            strategy = worker._active_mode_strategy()
            self.assertEqual(strategy["mode"], "failed_validation")
            self.assertIn("exact missing context", " ".join(strategy["steps"]))
            self.assertEqual(worker.runtime_suggested_next_actions()[0]["type"], "diagnose")
            worker._observe_command_result("cat /repo/broken.py", CommandResult(True, "1 | def broken(:", "read"))
            worker._observe_command_result("diagnose other.py", check(True, scope="other.py"))
            self.assertFalse(worker._finalize_validation(SimpleNamespace(finished=False)).passed)
            worker._observe_command_result("diagnose /repo/broken.py", check(True, scope="/repo/broken.py"))
            self.assertTrue(worker._finalize_validation(SimpleNamespace(finished=True)).passed)

    def test_diff_and_shell_success_cannot_erase_a_failed_typecheck(self):
        evidence = ValidationEvidence()
        evidence.observe(check(False, "run_check", "typecheck"), has_mutation=True)
        evidence.observe(CommandResult(True, "clean diff", "mutation", tool_action={"type": "show_diff"}), has_mutation=True)
        evidence.observe(check(True, "run_shell", "pwd"), has_mutation=True)
        self.assertTrue(evidence.failures)
        evidence.observe(check(True, "run_check", "typecheck"), has_mutation=True)
        self.assertFalse(evidence.failures)

    def test_exploratory_shell_failure_is_not_a_validation_failure_before_edits(self):
        evidence = ValidationEvidence()
        evidence.observe(check(False, "run_shell", "rg missing"))
        self.assertFalse(evidence.failures)

    def test_snapshot_is_independent_for_proposal_deferrals(self):
        evidence = ValidationEvidence()
        snapshot = evidence.copy()
        evidence.observe(check(False))
        self.assertFalse(snapshot.failures)
        self.assertTrue(evidence.failures)

    def test_passing_a_route_on_another_server_does_not_clear_the_failure(self):
        evidence = ValidationEvidence()
        failed = CommandResult(False, "runtime error", "mutation", tool_action={
            "type": "run_route_check", "target": "/studio", "base_url": "http://localhost:3000"})
        evidence.observe(failed)
        passed = CommandResult(True, "clean", "mutation", tool_action={
            "type": "run_route_check", "target": "/studio", "base_url": "http://localhost:4000"})
        evidence.observe(passed)
        self.assertTrue(evidence.failures)


if __name__ == "__main__":
    unittest.main()
