from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from planner import DiscoveryResult, GoalExecutionResult, PlannerAgent, PlannerPlan
from planner import PlannerGoal
from issue_facts import IssueFactLedger


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def complete(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("Planner requested more model responses than expected.")
        return json.dumps(self.responses.pop(0))


class FakeWorker:
    root: Path
    history = []
    on_step_callback = None

    def __init__(self, root: Path):
        self.root = root
        self.closed = False

    def create_issue(self, **kwargs):
        return {
            "issue_id": "ISSUE-1",
            "request_summary": kwargs.get("request_summary", ""),
            "plan_summary": kwargs.get("plan_summary", ""),
            "source": kwargs.get("source", ""),
            "source_excerpt": kwargs.get("source_excerpt", ""),
        }

    def close_active_issue(self, *, note: str = ""):
        self.closed = True
        return {"ok": True, "note": note}


class ContinuousPlannerForTest(PlannerAgent):
    def __init__(self, *args, execution_results=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.execution_results = list(execution_results or [])
        self.execution_count = 0

    def execute_pending_plan(self) -> str:
        plan = self.session.pending_plan
        assert plan is not None
        self.execution_count += 1
        if self.execution_results:
            result = self.execution_results.pop(0)
            self.session.last_completed_results = [result]
            self.session.last_presented_plan = plan
            if result.status == "completed" and result.task_satisfied:
                self.session.last_completed_plan = plan
                self.session.pending_plan = None
            else:
                self.session.last_completed_plan = None
                self.session.pending_plan = plan
            return result.final_message
        self.session.last_completed_plan = plan
        self.session.last_presented_plan = plan
        self.session.last_completed_results = [
            GoalExecutionResult(
                goal_id=plan.goals[0].goal_id,
                title=plan.goals[0].title,
                delegated_task="test",
                final_message="done",
                status="completed",
                task_satisfied=True,
            )
        ]
        self.session.pending_plan = None
        return "Executed test plan."

    def execute_discovery(self, mode_key: str) -> str:
        request = self.session.pending_discovery
        self.session.last_discovery = DiscoveryResult(
            mode=mode_key,
            delegated_task="test discovery",
            final_message="Discovery identified the next route and relevant files.",
            reason=str(getattr(request, "reason", "") or ""),
            prompt=str(getattr(request, "prompt", "") or ""),
            ok=True,
            task_satisfied=True,
            validation_ran=False,
            validation_passed=True,
        )
        self.session.pending_discovery = None
        return "Discovery complete.\n\n" + self._handle_intake_turn()


def present_plan(summary: str, goal: str, notes):
    return {
        "action": {
            "type": "present_plan",
            "summary": summary,
            "goals": [
                {
                    "goal_id": "goal-1",
                    "title": "Implement next improvement",
                    "goal": goal,
                    "reason": "It is the next bounded task.",
                    "estimated_scope": "write",
                    "delegation_notes": notes,
                    "success_signals": ["A targeted validation passes."],
                }
            ],
            "not_in_scope": ["Unrelated files."],
            "confirmation_prompt": "Approve this plan to start execution.",
        }
    }


class ContinuousAutoApprovalTests(unittest.TestCase):
    def test_intent_mutation_plan_is_regenerated_instead_of_stopping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("Build the next useful improvement.\n", encoding="utf-8")
            model = FakeModel(
                [
                    present_plan(
                        "Update INTENT.md and implement the next improvement.",
                        "Patch INTENT.md with the selected task.",
                        ["Edit INTENT.md before touching code."],
                    ),
                    present_plan(
                        "Implement the next improvement using INTENT.md as read-only direction.",
                        "Implement a bounded code change chosen from read-only project intent.",
                        ["Read INTENT.md for direction only; do not mutate it."],
                    ),
                ]
            )
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(model, config, lambda: FakeWorker(root), json.loads)

            message = planner.start_continuous(max_cycles=1)

        self.assertIn("requested plan revision", message)
        self.assertIn("auto-approved plan", message)
        self.assertNotIn("auto approval blocked", message)
        self.assertEqual(len(model.prompts), 2)
        self.assertIn("INTENT.md is immutable project direction", model.prompts[1])

    def test_closed_duplicate_issue_is_not_reopened_by_create_issue(self):
        ledger = IssueFactLedger.empty()
        closed = ledger.create_issue(
            request_summary="Rename driver components",
            plan_summary="Rename driver components",
            source="intent",
            activate=True,
        )
        ledger.close_issue(closed.issue_id, note="done")

        duplicate = ledger.find_duplicate_issue(
            request_summary="Rename driver components",
            source="intent",
        )
        created = ledger.create_issue(
            request_summary="Rename driver components",
            plan_summary="Rename driver components",
            source="intent",
            activate=True,
        )

        self.assertIsNone(duplicate)
        self.assertNotEqual(created.issue_id, closed.issue_id)
        self.assertEqual(ledger.get_issue(closed.issue_id).status, "closed")

    def test_manual_issue_creation_activates_new_issue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(FakeModel([]), config, lambda: FakeWorker(root), json.loads)

            message = planner.create_manual_issue("Investigate route health checks")

        self.assertIn("Created issue ISSUE-1: Investigate route health checks", message)
        self.assertEqual(planner.session.active_issue_id, "ISSUE-1")

    def test_auto_mode_activates_open_issue_before_initial_planning(self):
        class ActivatingWorker(FakeWorker):
            def __init__(self, root: Path, ledger: IssueFactLedger):
                super().__init__(root)
                self.ledger = ledger
                self.activated = []

            def activate_issue(self, issue_id: str):
                issue = self.ledger.activate_issue(issue_id)
                self.activated.append(issue.issue_id)
                (self.root / "repo_facts.md").write_text(self.ledger.to_markdown(), encoding="utf-8")
                return issue.summary()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = IssueFactLedger.empty()
            ledger.create_issue(
                request_summary="Lower priority open issue",
                plan_summary="Lower priority open issue",
                priority=10,
                activate=False,
            )
            selected = ledger.create_issue(
                request_summary="Follow up reviewer 503 retry",
                plan_summary="Follow up reviewer 503 retry",
                priority=80,
                activate=False,
            )
            ledger.active_issue_id = ""
            ledger.upsert_fact(
                key="reviewer_retry",
                value="Reviewer 503 failures should get one automatic retry.",
                fact_type="goal",
                source_action="test",
                updated_step=1,
                updated_run_id=1,
                issue_id=selected.issue_id,
            )
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")
            worker = ActivatingWorker(root, ledger)
            model = FakeModel([
                present_plan(
                    "Implement reviewer retry follow-up.",
                    "Use the selected issue context to implement the reviewer retry follow-up.",
                    ["Use repo_facts active issue context."],
                )
            ])
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(model, config, lambda: worker, json.loads)

            planner.start_continuous(max_cycles=1)

        self.assertEqual(worker.activated, [selected.issue_id])
        self.assertIn(f'"issue_id": "{selected.issue_id}"', model.prompts[0])
        self.assertIn("Follow up reviewer 503 retry", model.prompts[0])
        self.assertIn("reviewer_retry", model.prompts[0])

    def test_prompted_auto_mode_creates_initial_issue_from_prompt(self):
        class PromptIssueWorker(FakeWorker):
            def __init__(self, root: Path):
                super().__init__(root)
                self.created = []

            def create_issue(self, **kwargs):
                issue = {
                    "issue_id": f"issue-{len(self.created) + 1:03d}",
                    "request_summary": kwargs.get("request_summary", ""),
                    "plan_summary": kwargs.get("plan_summary", ""),
                    "source": kwargs.get("source", ""),
                    "source_excerpt": kwargs.get("source_excerpt", ""),
                }
                self.created.append(issue)
                return issue

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = PromptIssueWorker(root)
            model = FakeModel([
                present_plan(
                    "Add manual auto-run prompt support.",
                    "Implement support for starting auto mode from an operator prompt.",
                    ["Use the operator prompt as run-level context."],
                )
            ])
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(model, config, lambda: worker, json.loads)

            message = planner.start_continuous(max_cycles=1, prompt="Build manual auto-run prompt support")

        self.assertIn("Auto run prompt: Build manual auto-run prompt support", message)
        self.assertEqual(worker.created[0]["source"], "auto_prompt")
        self.assertEqual(worker.created[0]["request_summary"], "Build manual auto-run prompt support")
        self.assertIn("Auto run prompt: Build manual auto-run prompt support", model.prompts[0])
        self.assertEqual(planner.continuous_state.completed_issue_ids, ["issue-001"])

    def test_intent_guidance_reference_passes_auto_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(FakeModel([]), config, lambda: FakeWorker(root), json.loads)
            planner.continuous_config.auto_approve = True
            plan = PlannerPlan(
                original_request="Auto run",
                summary=(
                    "Standardize domain naming and transition from frontend mock data to a "
                    "backend-driven service state, strictly treating INTENT.md as read-only guidance."
                ),
                goals=[
                    PlannerGoal(
                        goal_id="goal-1",
                        title="Standardize Domain Naming",
                        goal=(
                            "Rename components and files using 'Driver' to 'Service' or 'Server' "
                            "to align with the canonical types and INTENT.md guidance."
                        ),
                        reason="Discovery showed naming drift.",
                        estimated_scope="write",
                        delegation_notes=[
                            "Begin implementing the Kubernetes/Docker integration layer as defined in INTENT.md.",
                        ],
                        success_signals=["Driver-prefixed components are renamed."],
                    )
                ],
                not_in_scope=["INTENT.md edits."],
            )

            decision = planner._auto_approve_plan(plan)

        self.assertTrue(decision.approved)
        self.assertEqual(decision.revision_reasons, [])

    def test_review_retries_transient_service_unavailable_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("Build the next useful improvement.\n", encoding="utf-8")
            failed = GoalExecutionResult(
                goal_id="goal-1",
                title="Implement next improvement",
                delegated_task="test",
                final_message="503 service unavailable while validating route",
                status="failed",
                task_satisfied=False,
                validation_ran=True,
                validation_passed=False,
                validation_summary="503 service unavailable",
            )
            passed = GoalExecutionResult(
                goal_id="goal-1",
                title="Implement next improvement",
                delegated_task="test",
                final_message="done",
                status="completed",
                task_satisfied=True,
                validation_ran=True,
                validation_passed=True,
            )
            model = FakeModel(
                [
                    present_plan(
                        "Implement the next improvement.",
                        "Implement a bounded code change.",
                        ["Read INTENT.md for immutable direction only."],
                    ),
                ]
            )
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(
                model,
                config,
                lambda: FakeWorker(root),
                json.loads,
                execution_results=[failed, passed],
            )

            message = planner.start_continuous(max_cycles=1)

        self.assertIn("retrying once", message)
        self.assertIn("Continuous mode stopped: max_cycles_reached", message)
        self.assertEqual(planner.execution_count, 2)

    def test_auto_mode_converts_planner_clarification_to_discovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("Build the next useful improvement.\n", encoding="utf-8")
            model = FakeModel(
                [
                    {
                        "action": {
                            "type": "ask_clarification",
                            "question": "Which route should be prioritized before execution?",
                        }
                    },
                    present_plan(
                        "Implement the route discovery selected.",
                        "Implement a bounded code change informed by discovery.",
                        ["Use discovery findings directly."],
                    ),
                ]
            )
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(model, config, lambda: FakeWorker(root), json.loads)

            message = planner.start_continuous(max_cycles=1)

        self.assertIn("converted planner clarification into", message)
        self.assertIn("auto-approved plan", message)
        self.assertNotIn("planning_failed", message)

    def test_auto_mode_self_prompts_when_discovery_completes_without_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("Build the next useful improvement.\n", encoding="utf-8")
            model = FakeModel(
                [
                    {
                        "action": {
                            "type": "offer_discovery",
                            "reason": "Need repo evidence before choosing the next improvement.",
                            "prompt": "Check whether driver naming cleanup is still needed.",
                            "recommended_mode": "moderate",
                        }
                    },
                    {
                        "action": {
                            "type": "respond",
                            "message": (
                                "Discovery complete. No remaining driver literals found; "
                                "that migration is already reflected in the current state."
                            ),
                        }
                    },
                    present_plan(
                        "Pivot to the next bounded service-state improvement.",
                        "Implement the next service-state backend/frontend integration step.",
                        ["Discovery showed the naming cleanup branch is already complete."],
                    ),
                ]
            )
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(model, config, lambda: FakeWorker(root), json.loads)

            message = planner.start_continuous(max_cycles=1)

        self.assertIn("continued planning after discovery", message)
        self.assertIn("auto-approved plan", message)
        self.assertNotIn("planning_failed", message)
        self.assertIn("Auto mode discovery is complete", model.prompts[-1])

    def test_planning_failed_stop_reason_includes_non_clarification_response(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("Build the next useful improvement.\n", encoding="utf-8")
            model = FakeModel(
                [
                    {
                        "action": {
                            "type": "respond",
                            "message": "No safe plan could be generated from the available context.",
                        }
                    }
                ]
            )
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(model, config, lambda: FakeWorker(root), json.loads)

            message = planner.start_continuous(max_cycles=1)

        self.assertIn("planning_failed: No safe plan could be generated from the available context.", message)
        self.assertEqual(
            planner.continuous_state.latest_planning_failure,
            "No safe plan could be generated from the available context.",
        )


if __name__ == "__main__":
    unittest.main()
