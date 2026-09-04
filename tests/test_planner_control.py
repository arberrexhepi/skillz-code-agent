from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from planner import PlannerAgent
from planner_control import (
    parse_final_summary_response,
    parse_next_goal_guidance_response,
    parse_planner_intake_response,
)


class SequenceModelClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def complete(self, system: str, prompt: str) -> str:
        if not self._responses:
            raise AssertionError("No model responses left")
        return self._responses.pop(0)

    def get_last_metrics(self) -> dict:
        return {}

    def clone(self) -> "SequenceModelClient":
        return SequenceModelClient(list(self._responses))


class PlannerWorkerStub:
    def __init__(self) -> None:
        self.history = []
        self.root = Path.cwd()
        self.on_step_callback = None

    def set_steering(self, prompt: str) -> None:
        return None

    def clear_steering(self) -> None:
        return None

    def set_goal_fact_keys(self, keys: list[str]) -> None:
        return None

    def clear_goal_fact_keys(self) -> None:
        return None

    def configure_discovery_budget(self, mode_key: str, mode_label: str, max_tool_calls: int) -> None:
        return None

    def clear_discovery_budget(self) -> None:
        return None

    def configure_backoff(self, *, enabled: bool, token_limit_k: int = 0) -> dict:
        return {"enabled": enabled, "token_limit_k": token_limit_k}

    def get_backoff_state(self) -> dict:
        return {"enabled": True, "token_limit_k": 0}

    def render_last_usage_summary(self) -> str:
        return ""

    def run_task(self, task: str) -> object:
        raise AssertionError("run_task should not be called during intake parsing")

    def prepare_for_goal(self, preserve_context: bool) -> None:
        return None

    def ensure_issue_for_plan(self, *, original_request: str, plan_summary: str, reuse_issue_id: str = "") -> dict:
        return {"issue_id": reuse_issue_id or "issue-001"}

    def close_active_issue(self, *, note: str = "") -> dict | None:
        return None

    def close_issue(self, issue_id: str, *, note: str = "") -> dict:
        return {"issue_id": issue_id}

    def reopen_issue(self, issue_id: str) -> dict:
        return {"issue_id": issue_id}

    def delete_session(self) -> str:
        return "deleted"


class PlannerControlTests(unittest.TestCase):
    def test_parse_planner_intake_response_for_present_plan_tags(self) -> None:
        raw = """
<planner>
<thought>layout planning</thought>
<action>
<type>present_plan</type>
<summary>Improve the app layout.</summary>
<clarification_summary>Use repo context to address the requested layout work.</clarification_summary>
<assumption>The request is focused on layout structure.</assumption>
<assumption>Recent layout issues are relevant context.</assumption>
<not_in_scope>Syntax repairs unrelated to layout</not_in_scope>
<next_step_preview>Implement the main layout changes.</next_step_preview>
<confirmation_prompt>Approve this plan?</confirmation_prompt>
<goal>
<goal_id>goal-1</goal_id>
<title>Adjust layout</title>
<goal_text>Refine the main layout structure.</goal_text>
<reason>The request is primarily about layout.</reason>
<preserve_context>true</preserve_context>
<parallelizable>false</parallelizable>
<estimated_scope>write</estimated_scope>
<delegation_note>Start with the main layout container.</delegation_note>
<success_signal>The layout is updated without regressions.</success_signal>
<relevant_fact_key>layout</relevant_fact_key>
</goal>
</action>
</planner>
""".strip()

        payload = parse_planner_intake_response(raw)

        action = payload["action"]
        self.assertEqual(action["type"], "present_plan")
        self.assertEqual(action["summary"], "Improve the app layout.")
        self.assertEqual(action["assumptions"], [
            "The request is focused on layout structure.",
            "Recent layout issues are relevant context.",
        ])
        self.assertEqual(len(action["goals"]), 1)
        self.assertEqual(action["goals"][0]["goal_id"], "goal-1")
        self.assertTrue(action["goals"][0]["preserve_context"])
        self.assertEqual(action["goals"][0]["estimated_scope"], "write")

    def test_parse_next_goal_guidance_response_tags(self) -> None:
        raw = """
<guidance>
<commentary>Preserve context because the next goal builds on the same files.</commentary>
<preserve_context>true</preserve_context>
<extra_note>Keep the same worker warm.</extra_note>
</guidance>
""".strip()

        payload = parse_next_goal_guidance_response(raw)

        self.assertEqual(payload["commentary"], "Preserve context because the next goal builds on the same files.")
        self.assertTrue(payload["preserve_context"])
        self.assertEqual(payload["extra_notes"], ["Keep the same worker warm."])

    def test_parse_final_summary_response_tags(self) -> None:
        raw = """
<final_summary>
<summary>Execution completed successfully.</summary>
<next_step>Run a quick UI validation pass.</next_step>
<next_step>Review any responsive layout edge cases.</next_step>
</final_summary>
""".strip()

        payload = parse_final_summary_response(raw)

        self.assertEqual(payload["summary"], "Execution completed successfully.")
        self.assertEqual(
            payload["next_steps"],
            ["Run a quick UI validation pass.", "Review any responsive layout edge cases."],
        )

    def test_parse_planner_intake_response_salvages_orphan_goal_fields(self) -> None:
        raw = """
<planner>
<thought>The user wants UI improvements.</thought>
<action>
<type>present_plan</type>
<summary>The overall plan is to address the identified UI issues.</summary>
<clarification_summary>The request is broad and repo facts suggest layout work.</clarification_summary>
<assumption>The main focus is layout changes.</assumption>
<not_in_scope>Deep redesign of the entire app architecture.</not_in_scope>
<next_step_preview>Implement the two-column layout and button relocation.</next_step_preview>
<depends_on>None</depends_on>
<delegation_note>Fix syntax first, then layout changes.</delegation_note>
<success_signal>Successful compilation after layout fixes.</success_signal>
<relevant_fact_key>issue-01</relevant_fact_key>
</action>
</planner>
""".strip()

        payload = parse_planner_intake_response(raw)

        goals = payload["action"]["goals"]
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["goal_id"], "goal-1")
        self.assertEqual(goals[0]["goal"], "Implement the two-column layout and button relocation.")
        self.assertEqual(goals[0]["delegation_notes"], ["Fix syntax first, then layout changes."])
        self.assertEqual(goals[0]["success_signals"], ["Successful compilation after layout fixes."])
        self.assertEqual(goals[0]["relevant_fact_keys"], ["issue-01"])

    def test_planner_start_request_accepts_local_tagged_plan(self) -> None:
        raw = """
<planner>
<thought>layout planning</thought>
<action>
<type>present_plan</type>
<summary>Improve the app layout.</summary>
<clarification_summary>Use repo context to address the requested layout work.</clarification_summary>
<assumption>The request is focused on layout structure.</assumption>
<not_in_scope>Syntax repairs unrelated to layout</not_in_scope>
<next_step_preview>Implement the main layout changes.</next_step_preview>
<confirmation_prompt>Approve this plan?</confirmation_prompt>
<goal>
<goal_id>goal-1</goal_id>
<title>Adjust layout</title>
<goal_text>Refine the main layout structure.</goal_text>
<reason>The request is primarily about layout.</reason>
<preserve_context>true</preserve_context>
<parallelizable>false</parallelizable>
<estimated_scope>write</estimated_scope>
<delegation_note>Start with the main layout container.</delegation_note>
<success_signal>The layout is updated without regressions.</success_signal>
</goal>
</action>
</planner>
""".strip()

        with tempfile.TemporaryDirectory() as tmpdir:
            planner = PlannerAgent(
                model_client=SequenceModelClient([raw]),
                config=SimpleNamespace(
                    root=Path(tmpdir),
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: PlannerWorkerStub(),
                json_loader=lambda text: {"unexpected": True},
            )

            message = planner.start_request("Improve the current app layout")

        self.assertIn("Plan Summary", message)
        self.assertIn("Improve the app layout.", message)
        self.assertIn("Adjust layout", message)

    def test_planner_start_request_accepts_local_tagged_plan_without_goal_block(self) -> None:
        raw = """
<planner>
<thought>The user wants to improve the UI.</thought>
<action>
<type>present_plan</type>
<summary>The overall plan is to address the identified UI issues.</summary>
<clarification_summary>The request is broad and repo facts suggest layout work.</clarification_summary>
<assumption>The main focus is layout changes.</assumption>
<not_in_scope>Deep redesign of the entire app architecture.</not_in_scope>
<next_step_preview>Implement the two-column layout and button relocation.</next_step_preview>
<delegation_note>Fix syntax first, then layout changes.</delegation_note>
<success_signal>Successful compilation after layout fixes.</success_signal>
<relevant_fact_key>issue-01</relevant_fact_key>
</action>
</planner>
""".strip()

        with tempfile.TemporaryDirectory() as tmpdir:
            planner = PlannerAgent(
                model_client=SequenceModelClient([raw]),
                config=SimpleNamespace(
                    root=Path(tmpdir),
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: PlannerWorkerStub(),
                json_loader=lambda text: {"unexpected": True},
            )

            message = planner.start_request("Improve the current app layout")

        self.assertIn("Plan Summary", message)
        self.assertIn("Implement the two-column layout and button relocation.", message)
        self.assertIn("Fix syntax first, then layout changes.", message)


if __name__ == "__main__":
    unittest.main()
