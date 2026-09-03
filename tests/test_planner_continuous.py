from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from planner import DiscoveryRequest, DiscoveryResult, GoalExecutionResult, PlannerAgent, PlannerPlan
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

    def render_last_usage_summary(self):
        return ""


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


class ImmediateExecutionPlannerForTest(PlannerAgent):
    def _execute_goal_with_worker(self, worker, plan, goal, index):
        return GoalExecutionResult(
            goal_id=goal.goal_id,
            title=goal.title,
            delegated_task=goal.goal,
            final_message="goal done",
            status="completed",
            task_satisfied=True,
            validation_ran=True,
            validation_passed=True,
            validation_summary="validated",
        )


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
    def test_beta_turn_history_detects_nested_workspace_mutation(self):
        mutation_result = SimpleNamespace(ok=True, tool_action={"type": "write_file", "path": "src/app.ts"})
        turn = SimpleNamespace(results=[mutation_result])

        self.assertTrue(PlannerAgent._history_has_successful_workspace_mutation([turn]))

    def test_subscription_skips_optional_between_goal_model_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model = FakeModel([])
            config = SimpleNamespace(
                root=str(root),
                provider="codex-subscription",
                model="gpt-5.6-terra",
                thinking_mode="low",
                verbosity="medium",
                max_parallel_workers=1,
            )
            planner = PlannerAgent(model, config, lambda: FakeWorker(root), json.loads)
            first = PlannerGoal("goal-1", "First", "Complete first.", "Needed.")
            second = PlannerGoal("goal-2", "Second", "Complete second.", "Needed.")
            plan = PlannerPlan(original_request="Complete both", summary="Complete both", goals=[first, second])
            result = GoalExecutionResult(
                goal_id="goal-1",
                title="First",
                delegated_task="Complete first.",
                final_message="Done.",
                status="completed",
                task_satisfied=True,
                validation_ran=True,
                validation_passed=True,
            )

            guidance = planner._plan_next_goal_guidance(plan, first, result)

        self.assertEqual(guidance, "")
        self.assertEqual(model.prompts, [])

    def test_goal_finish_callback_sees_incremental_completed_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model = FakeModel([{"summary": "Both goals completed.", "next_steps": []}])
            config = SimpleNamespace(
                root=str(root),
                provider="codex-subscription",
                model="gpt-5.6-terra",
                thinking_mode="low",
                verbosity="medium",
                max_parallel_workers=1,
            )
            planner = ImmediateExecutionPlannerForTest(model, config, lambda: FakeWorker(root), json.loads)
            planner.session.pending_plan = PlannerPlan(
                original_request="Complete both",
                summary="Complete both",
                goals=[
                    PlannerGoal("goal-1", "First", "Complete first.", "Needed."),
                    PlannerGoal("goal-2", "Second", "Complete second.", "Needed."),
                ],
                not_in_scope=["Unrelated files."],
            )
            completed_counts = []
            planner.on_goal_callback = lambda event, *_args: (
                completed_counts.append(len(planner.session.completed_results)) if event == "goal_finish" else None
            )

            planner.execute_pending_plan()

        self.assertEqual(completed_counts, [1, 2])
        self.assertEqual(planner.session.last_next_steps, [])
        self.assertEqual(planner.export_state()["last_next_steps"], [])

    def test_manual_discovery_handoff_continues_to_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model = FakeModel(
                [
                    {
                        "action": {
                            "type": "respond",
                            "message": "Discovery is complete; pass the findings to the planner.",
                        }
                    },
                    present_plan(
                        "Implement the discovered project workflow.",
                        "Implement the bounded project workflow from discovery.",
                        ["Use the completed discovery findings directly."],
                    ),
                ]
            )
            config = SimpleNamespace(
                root=str(root),
                provider="openai",
                model="test",
                thinking_mode="low",
                verbosity="medium",
                max_parallel_workers=1,
            )
            planner = PlannerAgent(model, config, lambda: FakeWorker(root), json.loads)
            planner.session.latest_request = "Implement projects"
            planner.session.intake_messages = [{"role": "user", "content": "Implement projects"}]
            planner.session.pending_discovery = DiscoveryRequest(
                reason="Inspect project persistence first.",
                prompt="Trace the relevant project workflow.",
                recommended_mode="moderate",
            )
            run_result = SimpleNamespace(
                ok=True,
                final_message="Project persistence and command workflow entry points were identified.",
                task_satisfied=True,
                validation_ran=False,
                validation_passed=True,
                touched_paths=["src/services/projectService.ts"],
                validation=SimpleNamespace(summary="Read-only discovery completed."),
            )
            planner._run_worker_task = lambda **_kwargs: {  # type: ignore[method-assign]
                "run_result": run_result,
                "history_slice": [],
                "elapsed": 0.1,
                "budget_state": {"tool_calls_used": 2, "max_tool_calls": 8},
            }

            message = planner.execute_discovery("moderate")

        self.assertIsNotNone(planner.session.pending_plan)
        self.assertIsNone(planner.session.pending_discovery)
        self.assertIn("Plan", message)
        self.assertIn("Continue to planning now", model.prompts[-1])

    def test_subscription_write_goal_cannot_complete_without_mutation_or_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(
                root=str(root),
                provider="codex-subscription",
                model="gpt-5.6-terra",
                thinking_mode="low",
                verbosity="medium",
                max_parallel_workers=1,
            )
            planner = PlannerAgent(FakeModel([]), config, lambda: FakeWorker(root), json.loads)
            worker = SimpleNamespace(history=[], render_last_usage_summary=lambda: "")
            run_result = SimpleNamespace(
                ok=True,
                final_message="Done.",
                task_satisfied=True,
                validation_ran=False,
                validation_passed=True,
                touched_paths=[],
                validation=SimpleNamespace(summary="No mutating actions required validation."),
            )
            planner._run_worker_task = lambda **_kwargs: {  # type: ignore[method-assign]
                "run_result": run_result,
                "history_slice": [],
                "elapsed": 0.1,
            }
            goal = PlannerGoal(
                goal_id="goal-write",
                title="Implement project modeling",
                goal="Add the required project model.",
                reason="The feature requires repository changes.",
                estimated_scope="write",
            )
            plan = PlannerPlan(original_request="Implement projects", summary="Implement projects", goals=[goal])

            result = planner._execute_goal_with_worker(worker, plan, goal, 1)

        self.assertEqual(result.status, "failed")
        self.assertFalse(result.task_satisfied)
        self.assertFalse(result.validation_passed)
        self.assertIn("without a workspace mutation or validation", result.validation_summary)

    def test_subscription_write_goal_retries_noop_and_accepts_real_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(
                root=str(root),
                provider="codex-subscription",
                model="gpt-5.6-terra",
                thinking_mode="low",
                verbosity="medium",
                max_parallel_workers=1,
            )
            planner = PlannerAgent(FakeModel([]), config, lambda: FakeWorker(root), json.loads)
            recovery_prepares = []
            worker = SimpleNamespace(
                history=[],
                render_last_usage_summary=lambda: "",
                prepare_unverified_completion_recovery=lambda: recovery_prepares.append(True),
            )
            no_op = SimpleNamespace(
                ok=True,
                final_message="Already complete.",
                task_satisfied=True,
                validation_ran=False,
                validation_passed=True,
                touched_paths=[],
                validation=SimpleNamespace(summary="No validation ran."),
            )
            validated = SimpleNamespace(
                ok=True,
                final_message="Validated the existing implementation.",
                task_satisfied=True,
                validation_ran=True,
                validation_passed=True,
                touched_paths=["src/context/TodoContext.tsx"],
                validation=SimpleNamespace(summary="TypeScript diagnostics passed."),
            )
            executions = [
                {"run_result": no_op, "history_slice": [], "elapsed": 0.1},
                {"run_result": validated, "history_slice": [], "elapsed": 0.2},
            ]
            calls = []

            def run_worker_task(**kwargs):
                calls.append(kwargs)
                return executions.pop(0)

            planner._run_worker_task = run_worker_task  # type: ignore[method-assign]
            goal = PlannerGoal(
                goal_id="goal-write",
                title="Complete project integration",
                goal="Finish the project context contract.",
                reason="The partial implementation must be completed.",
                estimated_scope="write",
            )
            plan = PlannerPlan(original_request="Proceed", summary="Complete issue", goals=[goal])

            result = planner._execute_goal_with_worker(worker, plan, goal, 1)

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.validation_ran)
        self.assertTrue(result.validation_passed)
        self.assertEqual(result.duration_s, 0.3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(recovery_prepares, [True])
        self.assertTrue(calls[1]["preserve_context"])
        self.assertIn("previous turn claimed", calls[1]["task"])

    def test_discovery_selection_becomes_running_before_worker_starts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(
                root=str(root),
                provider="codex-subscription",
                model="gpt-5.6-luna",
                thinking_mode="low",
                verbosity="medium",
                max_parallel_workers=1,
            )
            planner = PlannerAgent(FakeModel([]), config, lambda: FakeWorker(root), json.loads)
            planner.session.pending_discovery = DiscoveryRequest(
                reason="Inspect the implementation.",
                prompt="Choose a discovery depth.",
                recommended_mode="quick",
            )
            planner.session.discovery_phase = "pending"
            observed = []
            planner.on_discovery_callback = lambda event, mode: observed.append(
                (event, mode, planner.session.pending_discovery, planner.session.discovery_phase, planner.session.active_discovery_mode)
            )

            def fail_after_observing(**_kwargs):
                self.assertIsNone(planner.session.pending_discovery)
                self.assertEqual(planner.session.discovery_phase, "running")
                self.assertEqual(planner.session.active_discovery_mode, "quick")
                self.assertEqual(planner.export_state()["status"], "discovering")
                raise RuntimeError("stop after lifecycle assertion")

            planner._run_worker_task = fail_after_observing  # type: ignore[method-assign]
            message = planner.execute_discovery("quick")

        self.assertIn("Discovery failed before planning could continue", message)
        self.assertEqual(observed[0], ("discovery_start", "quick", None, "running", "quick"))
        self.assertEqual(observed[-1], ("discovery_finish", "quick", None, "complete", ""))

    def test_plan_execution_marks_discovery_phase_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model = FakeModel([{"summary": "Plan done.", "next_steps": []}])
            config = SimpleNamespace(
                root=str(root),
                provider="openai",
                model="test",
                thinking_mode="low",
                verbosity="medium",
                max_parallel_workers=1,
            )
            planner = ImmediateExecutionPlannerForTest(model, config, lambda: FakeWorker(root), json.loads)
            planner.session.pending_discovery = DiscoveryRequest(
                reason="stale discovery",
                prompt="Inspect before planning.",
                recommended_mode="quick",
            )
            planner.session.discovery_phase = "pending"
            planner.session.pending_plan = PlannerPlan(
                original_request="Fix the issue",
                summary="Fix the issue",
                goals=[
                    PlannerGoal(
                        goal_id="goal-1",
                        title="Fix",
                        goal="Make the fix.",
                        reason="Needed.",
                        estimated_scope="write",
                        success_signals=["validated"],
                    )
                ],
                not_in_scope=["Unrelated changes."],
            )

            planner.execute_pending_plan()

        self.assertIsNone(planner.session.pending_discovery)
        self.assertEqual(planner.session.discovery_phase, "complete")
        self.assertEqual(planner.export_state()["discovery_phase"], "complete")

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

    def test_start_request_lists_multiple_open_issues_instead_of_creating_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = IssueFactLedger.empty()
            first = ledger.create_issue(
                request_summary="First open issue",
                plan_summary="First open issue",
                priority=10,
                activate=False,
            )
            second = ledger.create_issue(
                request_summary="Second open issue",
                plan_summary="Second open issue",
                priority=20,
                activate=False,
            )
            ledger.active_issue_id = ""
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(FakeModel([]), config, lambda: FakeWorker(root), json.loads)

            message = planner.start_request("Continue work")

        self.assertIn("Multiple open issues exist", message)
        self.assertIn(first.issue_id, message)
        self.assertIn(second.issue_id, message)
        self.assertIn("Continue button in the Issues panel", message)
        self.assertEqual(planner.session.pending_plan, None)
        self.assertEqual(len(planner.model.prompts), 0)

    def test_issue_slash_command_activates_issue_and_starts_prompt(self):
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
                request_summary="Other open issue",
                plan_summary="Other open issue",
                priority=50,
                activate=False,
            )
            selected = ledger.create_issue(
                request_summary="Selected open issue",
                plan_summary="Selected open issue",
                priority=10,
                activate=False,
            )
            ledger.active_issue_id = ""
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")
            worker = ActivatingWorker(root, ledger)
            model = FakeModel([
                present_plan(
                    "Continue selected issue.",
                    "Continue the selected open issue.",
                    ["Use the selected issue context."],
                )
            ])
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(model, config, lambda: worker, json.loads)

            message = planner.start_request(f"/{selected.issue_id} continue with this issue")

        self.assertEqual(worker.activated, [selected.issue_id])
        self.assertIn("Plan Summary", message)
        self.assertIn("continue with this issue", model.prompts[0])
        self.assertIn(f'"issue_id": "{selected.issue_id}"', model.prompts[0])

    def test_issue_slash_command_without_prompt_starts_default_resolution_prompt(self):
        class ActivatingWorker(FakeWorker):
            def __init__(self, root: Path, ledger: IssueFactLedger):
                super().__init__(root)
                self.ledger = ledger

            def activate_issue(self, issue_id: str):
                issue = self.ledger.activate_issue(issue_id)
                (self.root / "repo_facts.md").write_text(self.ledger.to_markdown(), encoding="utf-8")
                return issue.summary()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = IssueFactLedger.empty()
            selected = ledger.create_issue(
                request_summary="Improve transfer errors",
                plan_summary="Improve transfer errors",
                priority=10,
                activate=False,
            )
            ledger.active_issue_id = ""
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")
            worker = ActivatingWorker(root, ledger)
            model = FakeModel([
                present_plan(
                    "Improve transfer errors.",
                    "Improve the selected issue.",
                    ["Use the generated issue continuation prompt."],
                )
            ])
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(model, config, lambda: worker, json.loads)

            message = planner.start_request(f"/{selected.issue_id}")

        self.assertIn("Plan Summary", message)
        self.assertIn(f"User has requested resolution of {selected.issue_id}: Improve transfer errors", model.prompts[0])
        self.assertIn(f'"issue_id": "{selected.issue_id}"', model.prompts[0])

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

    def test_prompted_auto_mode_reuses_active_open_issue(self):
        class PromptIssueWorker(FakeWorker):
            def __init__(self, root: Path, ledger: IssueFactLedger):
                super().__init__(root)
                self.ledger = ledger
                self.created = []
                self.activated = []

            def create_issue(self, **kwargs):
                self.created.append(kwargs)
                return super().create_issue(**kwargs)

            def activate_issue(self, issue_id: str):
                issue = self.ledger.activate_issue(issue_id)
                self.activated.append(issue.issue_id)
                (self.root / "repo_facts.md").write_text(self.ledger.to_markdown(), encoding="utf-8")
                return issue.summary()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = IssueFactLedger.empty()
            active = ledger.create_issue(
                request_summary="Continue existing cleanup work",
                plan_summary="Continue existing cleanup work",
                priority=10,
                activate=True,
            )
            ledger.create_issue(
                request_summary="Higher priority but inactive",
                plan_summary="Higher priority but inactive",
                priority=90,
                activate=False,
            )
            ledger.active_issue_id = active.issue_id
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")
            worker = PromptIssueWorker(root, ledger)
            model = FakeModel([
                present_plan(
                    "Continue cleanup work.",
                    "Continue the active cleanup issue.",
                    ["Use the active open issue."],
                )
            ])
            config = SimpleNamespace(root=str(root), provider="openai", model="test", max_parallel_workers=1)
            planner = ContinuousPlannerForTest(model, config, lambda: worker, json.loads)

            message = planner.start_continuous(max_cycles=1, prompt="Do the next thing")

        self.assertIn("Current issue: Continue existing cleanup work", model.prompts[0])
        self.assertEqual(worker.created, [])
        self.assertEqual(worker.activated, [active.issue_id])
        self.assertIn(f"auto-approved plan for {active.issue_id}", message)

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
