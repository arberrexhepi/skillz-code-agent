from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bridge_presentation import bridge_exchange
from planner import PlannerAgent, PlannerSession, PlannerPlan, PlannerGoal, DiscoveryRequest, DiscoveryResult, GoalExecutionResult
from tree_loop import TreeLoop
from live_test_loop import TreeLoopPlannerWorker
from test_tree_loop_messages import RecordingMessageModel


class BridgePresentationTests(unittest.TestCase):
    def setUp(self):
        self.planner = PlannerAgent.__new__(PlannerAgent)
        self.planner.session = PlannerSession()
        self.plan = PlannerPlan(original_request="Fix the sidebar", summary="Show ten projects and an expansion control", goals=[PlannerGoal("g1", "Update sidebar", "Show ten projects", "Requested")])

    def test_discovery_offer_and_selection_are_reports_only_in_context(self):
        self.planner.session.pending_discovery = DiscoveryRequest("Locate the sidebar", "Inspect project loading")
        offer = self.planner._render_discovery_offer(self.planner.session.pending_discovery)
        entry = bridge_exchange(self.planner, "assistant", offer)
        self.assertEqual(entry["content"], offer)
        self.assertEqual(entry["presentation"][0]["status"], "offered")
        choice = bridge_exchange(self.planner, "user", "moderate")
        self.assertEqual(choice["presentation"][0]["selection"], "Moderate")
        self.planner.session.pending_discovery = None
        self.assertEqual(bridge_exchange(self.planner, "user", "moderate")["presentation"][0]["kind"], "message")

    def test_combined_discovery_plan_and_conversation_are_not_swallowed(self):
        discovery = DiscoveryResult(mode="quick", delegated_task="Inspect", final_message="Located the sidebar.", ok=True)
        self.planner.session.last_discovery = discovery
        self.planner.session.pending_plan = self.plan
        content = "\n\n".join([self.planner._render_discovery_result(discovery), "There is also a database filtering issue to consider.", self.planner._render_plan(self.plan)])
        entry = bridge_exchange(self.planner, "assistant", content)
        parts = entry["presentation"]
        self.assertEqual([part["kind"] for part in parts], ["workflow", "message", "workflow"])
        self.assertEqual(parts[1]["content"], "There is also a database filtering issue to consider.")
        self.assertEqual("\n\n".join(part["content"] for part in parts), content)
        self.plan.summary = "A later request"
        self.assertNotEqual(parts[2]["plan"]["summary"], self.plan.summary)

    def test_approval_snapshot_and_goals_preserve_final_reply(self):
        self.planner.session.pending_plan = self.plan
        self.assertEqual(bridge_exchange(self.planner, "user", "approve")["presentation"][0]["selection"], "Approved")
        result = GoalExecutionResult(goal_id="g1", title="Update sidebar", delegated_task="Edit", final_message="Implemented and tested.")
        original = self.planner._render_goal_result(1, 1, result)
        result.commentary_for_next_goal = "Guidance added after rendering"
        self.planner.session.pending_plan = None
        self.planner.session.last_completed_plan = self.plan
        self.planner.session.last_completed_results = [result]
        content = original + "\n\nThe sidebar now shows ten projects."
        parts = bridge_exchange(self.planner, "assistant", content)["presentation"]
        self.assertEqual(parts[0]["goals"][0]["final_message"], result.final_message)
        self.assertEqual(parts[1], {"kind": "message", "content": "The sidebar now shows ten projects."})

    def test_failed_report_and_issue_controls_keep_all_original_text(self):
        self.planner.session.last_discovery = DiscoveryResult(mode="quick", delegated_task="Inspect", final_message="Cannot read source", ok=False)
        content = self.planner._render_discovery_result(self.planner.session.last_discovery)
        self.assertEqual(bridge_exchange(self.planner, "assistant", content)["presentation"][0]["status"], "failed")
        for role, text in [("user", "/close-issue issue-062"), ("assistant", "Closed issue issue-062. It will stay out of active context until reopened.")]:
            part = bridge_exchange(self.planner, role, text)["presentation"][0]
            self.assertEqual(part["category"], "issue")
            self.assertEqual(part["content"], text)

    def test_presentation_failure_never_breaks_bridge_output(self):
        entry = bridge_exchange(SimpleNamespace(), "assistant", "Important output")
        self.assertEqual(entry["content"], "Important output")
        self.assertNotIn("presentation", entry)

    def test_beta_thought_is_emitted_before_commands_without_source_payload(self):
        thoughts = []
        observed = []
        model = RecordingMessageModel([">>th: Inspect the repository before making changes.\nwrite /repo/note.md <<<\n>>th: this is file content, not a turn thought\n>>>\nfinish done"])
        with tempfile.TemporaryDirectory() as tmp:
            loop = TreeLoop(model=model, workspace_root=Path(tmp), max_turns=1, verbose=False,
                            model_event_observer=lambda event: (thoughts.append(event), observed.append(event["event"])),
                            command_observer=lambda *args: observed.append("command"))
            loop.run("Write a fixture")
        events = [event for event in thoughts if event["event"] == "turn_thought"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["thought"], "Inspect the repository before making changes.")
        self.assertLess(observed.index("turn_thought"), observed.index("command"))

    def test_beta_forwards_full_thought_without_overwriting_model_usage_state(self):
        worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
        messages = []
        worker.on_step_callback = messages.append
        worker._bridge_step_counter = 4
        worker._llm_activity = {"last_event": "model_call_finish"}
        thought = "A detailed operator-facing status summary. " * 20
        worker._observe_model_event({"event": "turn_thought", "turn": 3, "thought": thought})
        self.assertEqual(messages[0]["thought"], thought)
        self.assertEqual(messages[0]["turn"], 3)
        self.assertEqual(worker._llm_activity["last_event"], "model_call_finish")


if __name__ == "__main__":
    unittest.main()
