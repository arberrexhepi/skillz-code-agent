from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from issue_facts import IssueFactLedger
from live_test_loop import TreeLoopPlannerWorker
from main import WorkingFolderAgent, _handle_bridge_planner_action
from planner import PlannerAgent


class IssueCreationTests(unittest.TestCase):
    def handle(self, planner, request):
        return _handle_bridge_planner_action(
            planner=planner, request={"action": "create_issue", **request},
            transcript=[], add_exchange=lambda *args: None,
        )

    def test_both_workers_create_open_issue_without_changing_active_context(self):
        for worker_type in (WorkingFolderAgent, TreeLoopPlannerWorker):
            with self.subTest(worker=worker_type.__name__), tempfile.TemporaryDirectory() as tmp:
                ledger = IssueFactLedger.empty()
                active = ledger.create_issue(request_summary="Current work", activate=True)
                worker = worker_type.__new__(worker_type)
                worker.issue_ledger = ledger
                worker.active_issue_id = active.issue_id
                worker._persist_repo_facts = lambda: (Path(tmp) / "repo_facts.md").write_text(ledger.to_markdown())
                worker._clear_facts = Mock()
                worker._sync_fact_map = Mock()
                worker._active_run_id = 19
                planner = PlannerAgent.__new__(PlannerAgent)
                planner.worker = worker
                current_plan = object()
                planner.session = SimpleNamespace(active_issue_id=active.issue_id, pending_plan=current_plan, executing=True)

                message = self.handle(planner, {"summary": "Next task", "activate": False})

                self.assertIn("Created issue", message)
                self.assertEqual(len(ledger.issues), 2)
                self.assertEqual(ledger.issues[1].status, "open")
                self.assertEqual(ledger.active_issue_id, active.issue_id)
                self.assertEqual(worker.active_issue_id, active.issue_id)
                self.assertEqual(planner.session.active_issue_id, active.issue_id)
                self.assertIs(planner.session.pending_plan, current_plan)
                self.assertTrue(planner.session.executing)
                self.assertEqual(worker._active_run_id, 19)
                worker._clear_facts.assert_not_called()
                self.assertIn("Next task", (Path(tmp) / "repo_facts.md").read_text())

    def test_bridge_preserves_default_activation_and_accepts_nested_false(self):
        for request, expected in [
            ({"summary": "New issue"}, True),
            ({"payload": {"summary": "New issue", "activate": False}}, False),
            ({"summary": "New issue", "activate": True, "payload": {"activate": False}}, True),
        ]:
            with self.subTest(request=request):
                creator = Mock(return_value="Created issue issue-002: New issue")
                self.handle(SimpleNamespace(create_manual_issue=creator), request)
                creator.assert_called_once_with("New issue", activate=expected)

    def test_bridge_does_not_acknowledge_failed_creation_as_success(self):
        for message in ("Issue creation failed: read-only filesystem", "Worker does not support issue creation."):
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.handle(SimpleNamespace(create_manual_issue=lambda *args, **kwargs: message), {"summary": "New issue", "activate": False})

    def test_invalid_activation_and_empty_summary_never_call_creator(self):
        creator = Mock()
        for request in ({"summary": "Issue", "activate": "false"}, {"summary": "  ", "activate": False}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.handle(SimpleNamespace(create_manual_issue=creator), request)
        creator.assert_not_called()

    def test_missing_worker_issue_id_is_a_failure_and_preserves_active_issue(self):
        planner = PlannerAgent.__new__(PlannerAgent)
        planner.worker = SimpleNamespace(create_issue=Mock(return_value={}))
        planner.session = SimpleNamespace(active_issue_id="issue-001")

        with self.assertRaisesRegex(ValueError, "worker returned no issue identifier"):
            self.handle(planner, {"summary": "New issue", "activate": False})

        self.assertEqual(planner.session.active_issue_id, "issue-001")


if __name__ == "__main__":
    unittest.main()
