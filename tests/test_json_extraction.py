from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from main import extract_first_json_object
from planner import PlannerAgent


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


class JsonExtractionTests(unittest.TestCase):
    def test_extract_first_json_object_repairs_local_present_plan_shape(self) -> None:
        raw = """{
  "thought": "layout planning",
  "action": {
    "type": "present_plan",
    "summary": "Plan to improve layout.",
    "clarification_summary": "General layout request."    "
  },
  "assumptions": [
    "The main request is layout-related."
  ],
  "not_in_scope": [
    "Syntax fixes"
  ],
  "next_steps_preview": [
    "Implement layout improvements."
  ],
  "confirmation_prompt": "Approve?",
  "goals": [
    {
      "goal_id": "goal-1",
      "title": "Improve layout",
      "goal": "Adjust the layout.",
      "reason": "The request is about layout."
    }
  ],
}"""

        parsed = extract_first_json_object(raw)

        self.assertEqual(parsed.get("thought"), "layout planning")
        action = parsed.get("action", {})
        self.assertEqual(action.get("type"), "present_plan")
        self.assertEqual(action.get("summary"), "Plan to improve layout.")
        self.assertEqual(action.get("clarification_summary"), "General layout request.")
        self.assertEqual(action.get("assumptions"), ["The main request is layout-related."])
        self.assertEqual(action.get("not_in_scope"), ["Syntax fixes"])
        self.assertEqual(action.get("next_steps_preview"), ["Implement layout improvements."])
        self.assertEqual(action.get("confirmation_prompt"), "Approve?")
        self.assertEqual(len(action.get("goals", [])), 1)
        self.assertNotIn("assumptions", parsed)
        self.assertNotIn("goals", parsed)

    def test_planner_start_request_accepts_repaired_present_plan_from_local_provider(self) -> None:
        raw = """{
  "thought": "layout planning",
  "action": {
    "type": "present_plan",
    "summary": "Plan to improve layout.",
    "clarification_summary": "General layout request."    "
  },
  "assumptions": [
    "The main request is layout-related."
  ],
  "not_in_scope": [
    "Syntax fixes"
  ],
  "next_steps_preview": [
    "Implement layout improvements."
  ],
  "confirmation_prompt": "Approve?",
  "goals": [
    {
      "goal_id": "goal-1",
      "title": "Improve layout",
      "goal": "Adjust the layout.",
      "reason": "The request is about layout."
    }
  ],
}"""

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
                json_loader=extract_first_json_object,
            )

            message = planner.start_request("Improve the current app's layout")

        self.assertIn("Plan Summary", message)
        self.assertIn("Plan to improve layout.", message)
        self.assertIn("Improve layout", message)
