from __future__ import annotations

from copy import deepcopy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock

from bridge_presentation import bridge_exchange
from discovery_budget import DiscoveryBudget, MAX_EXTENSION_TURNS
from live_test_loop import TreeLoopPlannerWorker
from main import AgentConfig, WorkingFolderAgent, _handle_bridge_planner_action
from planner import DiscoveryRequest, PlannerAgent
from test_planner_continuous import FakeModel, present_plan
from test_tree_loop_messages import RecordingMessageModel
from tree_loop import TreeLoop

REQUEST = {
    "additional_turns": 2,
    "reason": "The two route implementations disagree about the API contract.",
    "proposal": "Read the route caller and its test to identify the active contract.",
    "ambiguities": ["Does the active route use the legacy or new contract?"],
    "findings": "The entry point is note.txt. Two implementations remain plausible; preserve the ë character.",
}


class DiscoveryBudgetTests(unittest.TestCase):
    def test_request_requires_a_reached_limit_and_valid_bounded_details(self):
        budget = DiscoveryBudget("quick", "Quick", 6, max_turns=5)
        with self.assertRaisesRegex(ValueError, "existing discovery budget"):
            budget.request_extension(REQUEST)
        budget.turns_used = 5
        for turns in (0, -1, True, "2", 2.5, MAX_EXTENSION_TURNS + 1):
            with self.subTest(turns=turns), self.assertRaises(ValueError):
                budget.request_extension({**REQUEST, "additional_turns": turns})
        for field in ("reason", "proposal", "findings", "ambiguities"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                budget.request_extension({**REQUEST, field: ""})
        self.assertIsNone(budget.pending_extension)
        request = budget.request_extension(REQUEST)
        with self.assertRaises(ValueError):
            budget.request_extension(REQUEST)
        with self.assertRaises(ValueError):
            budget.accept_extension("stale")
        budget.accept_extension(request["request_id"])
        self.assertEqual(budget.remaining_turns, 2)
        self.assertEqual(budget.max_tool_calls, 8)
        with self.assertRaises(ValueError):
            budget.accept_extension(request["request_id"])

    def test_action_limit_allows_request_before_turn_limit(self):
        budget = DiscoveryBudget("quick", "Quick", 6, tool_calls_used=6, max_turns=20, turns_used=3)
        request = budget.request_extension(REQUEST)
        budget.accept_extension(request["request_id"])
        self.assertEqual((budget.turns_used, budget.max_turns, budget.remaining_turns), (3, 5, 2))
        self.assertEqual((budget.tool_calls_used, budget.max_tool_calls), (6, 8))


class DiscoveryExtensionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "note.txt").write_text("The new contract is active. ë\n", encoding="utf-8")

    def make_planner(self, runtime="beta", extra_responses=None):
        self.config = AgentConfig(provider="fake", model="fake", root=self.root,
                                  tool_script=Path(__file__).parent / "agent_tools.py", max_steps=2, quiet=True)
        if runtime == "stable":
            extension = json.dumps({"action": {"type": "request_discovery_extension", **REQUEST}})
            read = json.dumps({"action": {"type": "read_file", "path": "note.txt"}})
            finish = json.dumps({"action": {"type": "finish", "message": "The caller and test confirm the new contract; plan can proceed."}})
            self.worker_model = RecordingMessageModel([read, extension, *(extra_responses or [read, finish])])
            worker = WorkingFolderAgent(self.worker_model, self.config)
            worker._run_fact_subagent = Mock()
            # Isolate automatic prefetch; the explicit reads still use real tools.
            worker._parallel_discovery_prefetch_payload = Mock(return_value={})
        else:
            extension = "request-discovery-extension " + json.dumps(REQUEST)
            self.worker_model = RecordingMessageModel(["cat /repo/note.txt", extension,
                *(extra_responses or ["cat /repo/note.txt", "finish The caller and test confirm the new contract; plan can proceed."])])
            worker = TreeLoopPlannerWorker(model=self.worker_model, root=self.root, max_turns=2, verbose=False)
        self.model = FakeModel([present_plan("Implement the active contract", "Update the route", ["Validate the contract assumption."])])
        planner = PlannerAgent(self.model, self.config, lambda: worker, json.loads)
        planner.session.latest_request = "Discover the route contract before planning its update."
        planner.session.pending_discovery = DiscoveryRequest("Resolve route ambiguity", "Inspect caller and tests", "quick")
        planner.session.discovery_phase = "pending"
        return planner

    def start(self, planner):
        with redirect_stdout(io.StringIO()):
            response = planner.execute_discovery("quick")
        self.assertIn("Discovery Extension Requested", response)
        self.assertEqual(planner.session.discovery_phase, "awaiting_extension")
        self.assertEqual(planner.export_state()["status"], "awaiting_discovery_extension")
        self.assertEqual(len(self.model.prompts), 0, "Planner must not run while awaiting approval")
        self.assertEqual(len(self.worker_model.calls), 2)
        self.assertIsNotNone(planner.worker.discovery_budget)
        self.assertIn("Discovery extension", bridge_exchange(planner, "assistant", response)["presentation"][0]["title"])
        return deepcopy(planner.session.pending_discovery_extension)

    def decide(self, planner, request_id, accept):
        entries = []
        with redirect_stdout(io.StringIO()):
            message = _handle_bridge_planner_action(planner=planner, transcript=entries,
                request={"action": "approve_discovery_extension" if accept else "decline_discovery_extension", "request_id": request_id},
                add_exchange=lambda role, text: entries.append(bridge_exchange(planner, role, text)))
        return message, entries

    def test_accept_resumes_same_context_and_budget_in_both_runtimes(self):
        for runtime in ("stable", "beta"):
            with self.subTest(runtime=runtime):
                planner = self.make_planner(runtime)
                pending = self.start(planner)
                task = planner.session.last_discovery.delegated_task
                old_history = deepcopy(planner.session.last_discovery.worker_history_summary)
                message, entries = self.decide(planner, pending["request_id"], True)
                result = planner.session.last_discovery
                self.assertIn("Discovery Complete", message)
                self.assertIsNotNone(planner.session.pending_plan)
                self.assertIsNone(planner.session.pending_discovery_extension)
                self.assertIsNone(planner.worker.discovery_budget)
                self.assertEqual(result.delegated_task, task)
                self.assertEqual((result.turns_used, result.turns_max, result.tool_calls_max), (4, 4, 8))
                self.assertEqual(result.worker_history_summary[:len(old_history)], old_history)
                self.assertEqual(result.extension_requests[0]["decision"], "approved")
                self.assertEqual(entries[0]["presentation"][0]["selection"], "Extension approved")
                # The first resumed call retains the original messages and the request.
                resumed = self.worker_model.calls[2]
                self.assertEqual(resumed[0], self.worker_model.calls[0][0])
                self.assertTrue(any("request_discovery_extension" in item["content"] or "request-discovery-extension" in item["content"] for item in resumed))
                self.assertTrue(any("3/4" in item["content"] or '\"turns_used\": 3' in item["content"] for item in resumed))
                self.assertEqual(len(self.worker_model.calls), 4)
                if runtime == "stable":
                    planner.worker._parallel_discovery_prefetch_payload.assert_called_once()
                with self.assertRaises(ValueError):
                    self.decide(planner, pending["request_id"], True)

    def test_decline_passes_ambiguity_and_unperformed_proposal_to_real_planner(self):
        planner = self.make_planner()
        pending = self.start(planner)
        message, entries = self.decide(planner, pending["request_id"], False)
        self.assertIn("Discovery Complete with Ambiguities", message)
        self.assertIsNotNone(planner.session.pending_plan)
        self.assertEqual(planner.session.last_discovery.outcome, "partial")
        self.assertFalse(planner.session.last_discovery.task_satisfied)
        self.assertEqual(len(self.worker_model.calls), 2)
        self.assertIn(REQUEST["ambiguities"][0], self.model.prompts[0])
        self.assertIn(REQUEST["proposal"], self.model.prompts[0])
        self.assertIn("Do not restart discovery", self.model.prompts[0])
        self.assertIn(REQUEST["ambiguities"][0], planner.session.pending_plan.clarification_summary)
        self.assertTrue(any(REQUEST["ambiguities"][0] in note for note in planner.session.pending_plan.goals[0].delegation_notes))
        self.assertIsNone(planner.worker.discovery_budget)
        self.assertEqual(entries[0]["presentation"][0]["selection"], "Plan with current findings")

    def test_stale_decision_has_no_effect_and_missing_worker_context_can_still_be_declined(self):
        planner = self.make_planner()
        pending = self.start(planner)
        before = deepcopy(planner.session)
        with self.assertRaises(ValueError):
            self.decide(planner, "old-request", True)
        self.assertEqual(planner.session, before)
        planner.worker.clear_discovery_budget()
        with self.assertRaisesRegex(ValueError, "context is unavailable"):
            self.decide(planner, pending["request_id"], True)
        self.assertEqual(planner.session.pending_discovery_extension, pending)
        self.decide(planner, pending["request_id"], False)

    def test_cli_approval_and_repeated_request_get_a_new_id(self):
        extension = "request-discovery-extension " + json.dumps({**REQUEST, "additional_turns": 1})
        planner = self.make_planner(extra_responses=["cat /repo/note.txt", extension])
        first = self.start(planner)
        with redirect_stdout(io.StringIO()):
            message = planner.continue_conversation("approve")
        second = planner.session.pending_discovery_extension
        self.assertIn("Discovery Extension Requested", message)
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(second["turns_used"], 4)
        self.assertEqual(len(self.model.prompts), 0)
        with self.assertRaises(ValueError):
            self.decide(planner, first["request_id"], False)
        with redirect_stdout(io.StringIO()):
            planner.continue_conversation("no")
        self.assertEqual([r["decision"] for r in planner.session.last_discovery.extension_requests], ["approved", "declined"])

    def test_approved_turn_count_is_a_hard_limit_even_without_finish(self):
        planner = self.make_planner(extra_responses=["cat /repo/note.txt"] * 3)
        pending = self.start(planner)
        self.decide(planner, pending["request_id"], True)
        self.assertEqual(len(self.worker_model.calls), 4)
        self.assertEqual(planner.session.last_discovery.turns_used, 4)
        self.assertIsNone(planner.session.pending_plan)

    def test_continuous_mode_stops_for_extension_without_auto_approval_or_failure(self):
        planner = self.make_planner()
        self.model.responses.insert(0, {"action": {"type": "offer_discovery", "reason": "Need contract", "prompt": "Inspect routes", "recommended_mode": "quick"}})
        planner._create_or_select_continuous_issue = Mock(return_value={"issue_id": "ISSUE-1", "request_summary": "Update route"})
        planner._select_discovery_mode_for_issue = Mock(return_value="quick")
        with redirect_stdout(io.StringIO()):
            response = planner.start_continuous(max_cycles=2)
        self.assertIn("discovery_extension_pending", response)
        self.assertIsNotNone(planner.session.pending_discovery_extension)
        self.assertEqual(len(self.worker_model.calls), 2)
        self.assertEqual(len(self.model.prompts), 1)
        self.assertFalse(planner.continuous_state.enabled)
        self.assertIsNone(planner.session.pending_plan)
        before = len(self.worker_model.calls)
        planner.start_continuous(max_cycles=2)
        self.assertEqual(len(self.worker_model.calls), before)

    def test_stale_plan_or_depth_controls_cannot_decide_an_extension(self):
        planner = self.make_planner()
        pending = self.start(planner)
        for action in ("approve_plan", "reject_plan", "select_discovery_mode", "skip_discovery"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "extension decision"):
                _handle_bridge_planner_action(planner=planner, transcript=[], request={"action": action}, add_exchange=Mock())
        self.assertEqual(planner.session.pending_discovery_extension, pending)
        self.assertEqual(len(self.worker_model.calls), 2)

    def test_invalid_issue_switch_preserves_pending_discovery(self):
        planner = self.make_planner()
        pending = self.start(planner)
        planner.worker.activate_issue = Mock(side_effect=ValueError("Unknown issue"))
        self.assertIn("Issue activation failed", planner.continue_issue("missing"))
        self.assertEqual(planner.session.pending_discovery_extension, pending)
        self.assertIsNotNone(planner.worker.discovery_budget)

    def test_suggested_action_payload_works_for_extension_clients(self):
        planner = self.make_planner()
        pending = self.start(planner)
        suggestion = planner._suggested_next_actions_payload()[1]
        self.assertEqual(suggestion["payload"], {"request_id": pending["request_id"]})
        with redirect_stdout(io.StringIO()):
            _handle_bridge_planner_action(planner=planner, transcript=[],
                request={"action": suggestion["type"], "payload": suggestion["payload"]}, add_exchange=Mock())
        self.assertEqual(planner.session.last_discovery.outcome, "partial")
        self.assertIsNotNone(planner.session.pending_plan)

    def test_clear_session_discards_extension_and_worker_budget(self):
        planner = self.make_planner()
        self.start(planner)
        planner.clear_session()
        self.assertIsNone(planner.session.pending_discovery_extension)
        self.assertIsNone(planner.worker.discovery_budget)

    def test_tree_request_batch_is_rejected_before_any_file_read(self):
        request = "request-discovery-extension " + json.dumps(REQUEST)
        for batch in (request + "\ncat /repo/note.txt", "s1: " + request + "\ns2: cat /repo/note.txt", "s1: cat /repo/note.txt\ns1 -> s2.a: " + request, "s1: cat /repo/note.txt, " + request):
            with self.subTest(batch=batch):
                model = RecordingMessageModel([batch])
                loop = TreeLoop(model=model, workspace_root=self.root, max_turns=1, verbose=False)
                result = loop.run("Inspect contract")
                self.assertIn("only executable command", result.turns[0].results[0].output)
                self.assertEqual(loop._total_reads, 0)


if __name__ == "__main__":
    unittest.main()
