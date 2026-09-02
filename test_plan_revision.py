from copy import deepcopy
import unittest
from unittest.mock import Mock

from bridge_presentation import bridge_exchange
from main import _handle_bridge_planner_action
from planner import PlannerAgent, PlannerGoal, PlannerPlan, PlannerSession


class PlanRevisionTests(unittest.TestCase):
    def setUp(self):
        self.planner = PlannerAgent.__new__(PlannerAgent)
        self.plan = PlannerPlan("Original request", "Original plan", goals=[PlannerGoal("g1", "First goal", "Outcome", "Reason")])
        self.planner.session = PlannerSession(pending_plan=self.plan)
        self.planner.on_plan_callback = Mock()
        self.planner.try_builtin_command = Mock(return_value=None)
        self.planner._handle_intake_turn = Mock(return_value="Revised plan")
        self.planner.execute_pending_plan = Mock(side_effect=AssertionError("Must not execute"))
        self.exchanges = []

    def action(self, action="revise_plan", **extras):
        return _handle_bridge_planner_action(
            planner=self.planner, transcript=self.exchanges,
            request={"action": action, "feedback": "Reduce scope", "expected_plan": self.planner._plan_payload(self.plan), **extras},
            add_exchange=lambda role, text: self.exchanges.append(bridge_exchange(self.planner, role, text)),
        )

    def test_explicit_feedback_cannot_become_an_approval_or_command(self):
        for feedback in ("approve", "/run", "resume", "reject", "/delete-session"):
            with self.subTest(feedback=feedback):
                self.setUp()
                self.assertEqual(self.action(feedback=feedback), "Revised plan")
                self.assertEqual(self.planner.session.plan_revision_feedback, [feedback])
                self.assertEqual(self.planner.session.last_presented_plan, self.plan)
                self.assertTrue(self.planner.session.awaiting_plan_revision)
                self.planner._handle_intake_turn.assert_called_once_with(strict=True)
                self.planner.try_builtin_command.assert_not_called()
                self.planner.execute_pending_plan.assert_not_called()
                self.assertEqual(self.exchanges[0]["presentation"][0]["selection"], "Changes requested")
                self.assertIn(feedback, self.exchanges[0]["content"])

    def test_cli_feedback_uses_the_same_revision_flow(self):
        self.planner.continue_conversation("Keep the current schema")
        self.assertEqual(self.planner.session.plan_revision_feedback, ["Keep the current schema"])
        self.planner._handle_intake_turn.assert_called_once_with(strict=True)

    def test_blank_stale_missing_or_executing_plan_never_mutates(self):
        for extras in ({"feedback": "  "}, {"expected_plan": None}, {"expected_plan": {"summary": "Old plan"}}):
            with self.subTest(extras=extras):
                before = deepcopy(self.planner.session)
                with self.assertRaises(ValueError):
                    self.action(**extras)
                self.assertEqual(self.planner.session, before)
                self.assertEqual(self.exchanges, [])
        for executing, plan in ((True, self.plan), (False, None)):
            self.planner.session.executing = executing
            self.planner.session.pending_plan = plan
            with self.assertRaises(ValueError):
                self.action()
        self.planner._handle_intake_turn.assert_not_called()

    def test_stale_review_cannot_approve_reject_or_resume_another_plan(self):
        for action in ("approve_plan", "reject_plan", "continue_issue"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "plan changed"):
                self.action(action=action, expected_plan={"summary": "Stale"})
        self.planner.execute_pending_plan.assert_not_called()

    def test_paused_plan_can_be_revised_without_resuming_execution(self):
        self.planner.session = PlannerSession(execution_paused=True, paused_plan=self.plan, paused_failed_goal_id="g1")
        self.action()
        self.assertFalse(self.planner.session.execution_paused)
        self.assertIsNone(self.planner.session.paused_plan)
        self.assertEqual(self.planner.session.last_presented_plan, self.plan)
        self.assertEqual(self.exchanges[0]["presentation"][0]["selection"], "Changes requested")

    def test_generation_failure_restores_plan_checkpoint_and_does_not_duplicate_feedback(self):
        for paused in (False, True):
            with self.subTest(paused=paused):
                self.setUp()
                if paused:
                    self.planner.session = PlannerSession(execution_paused=True, paused_plan=self.plan, paused_failed_goal_id="g1", paused_failed_goal_index=0)
                before = deepcopy(self.planner.session)
                self.planner._handle_intake_turn.side_effect = RuntimeError("Network unavailable")
                with self.assertRaisesRegex(RuntimeError, "Network unavailable"):
                    self.action()
                self.assertEqual(self.planner.session, before)
                self.planner._handle_intake_turn.side_effect = None
                self.action()
                self.assertEqual(self.planner.session.plan_revision_feedback, ["Reduce scope"])

    def test_invalid_control_response_keeps_plan_retryable(self):
        self.planner._handle_intake_turn = PlannerAgent._handle_intake_turn.__get__(self.planner)
        self.planner._build_planner_prompt = Mock(return_value="prompt")
        self.planner._planner_system_prompt = Mock(return_value="system")
        self.planner._use_tagged_planner_control = Mock(return_value=False)
        self.planner._planner_complete = Mock(return_value="invalid")
        self.planner._load_planner_intake_payload = Mock(side_effect=ValueError("Malformed response"))
        before = deepcopy(self.planner.session)
        with self.assertRaisesRegex(ValueError, "Plan revision failed"):
            self.action()
        self.assertEqual(self.planner.session, before)

    def test_real_intake_presents_revised_plan_without_execution(self):
        self.planner._handle_intake_turn = PlannerAgent._handle_intake_turn.__get__(self.planner)
        self.planner._build_planner_prompt = Mock(return_value="prompt")
        self.planner._planner_system_prompt = Mock(return_value="system")
        self.planner._use_tagged_planner_control = Mock(return_value=False)
        self.planner._planner_complete = Mock(return_value="model response")
        self.planner._checkpoint_results_for_plan = Mock(return_value=[])
        self.planner._load_repo_facts_payload = Mock(return_value={})
        self.planner._load_planner_intake_payload = Mock(return_value={"action": {
            "type": "present_plan", "summary": "Revised scope", "goals": [
                {"goal_id": "g1", "title": "Smaller goal", "goal": "Only change presentation", "success_signals": ["Existing behavior preserved"]},
            ],
        }})
        output = self.action()
        self.assertIn("Revised scope", output)
        self.assertEqual(self.planner.session.pending_plan.goals[0].title, "Smaller goal")
        self.assertFalse(self.planner.session.awaiting_plan_revision)
        self.assertFalse(self.planner.session.executing)
        self.planner.execute_pending_plan.assert_not_called()
        self.assertEqual(self.exchanges[-1]["presentation"][0]["status"], "offered")


if __name__ == "__main__":
    unittest.main()
