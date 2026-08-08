from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from main import BackoffStrategy, BaseModelClient
from issue_facts import CompletedGoalCheckpoint, IssueFactLedger
from live_test_loop import TreeLoopPlannerWorker
from planner import GoalExecutionResult, PlannerAgent, PlannerGoal, PlannerPlan


class HttpError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        request_id: str = "",
        code: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})
        self.request_id = request_id
        if code:
            self.code = code


class FlakyModelClient(BaseModelClient):
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.calls = 0
        self.prompts: list[str] = []
        self.backoff = BackoffStrategy(enabled=False)

    def complete(self, system: str, prompt: str) -> str:
        return self._complete_with_backoff(system, prompt)

    def _do_complete(self, system: str, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        if self.failures:
            raise self.failures.pop(0)
        return "ok"

    def clone(self):
        return self


class NoopWorker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.history = []
        self.on_step_callback = None
        self.closed = False
        self.recorded_goal_ids: list[str] = []

    def ensure_issue_for_plan(self, **kwargs):
        return {"issue_id": "issue-001"}

    def close_active_issue(self, *, note: str = ""):
        self.closed = True
        return {"issue_id": "issue-001", "status": "closed"}

    def record_completed_goal(self, **kwargs):
        goal_id = str((kwargs.get("goal") or {}).get("goal_id", "") or "")
        self.recorded_goal_ids.append(goal_id)
        return {"goal_id": goal_id}


class CheckpointPlanner(PlannerAgent):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.goal_calls: list[tuple[str, bool]] = []
        self.goal_two_attempts = 0

    def _execute_goal_with_worker(self, worker, plan, goal, index):
        self.goal_calls.append((goal.goal_id, goal.preserve_context))
        if goal.goal_id == "goal-2":
            self.goal_two_attempts += 1
            if self.goal_two_attempts == 1:
                return GoalExecutionResult(
                    goal_id=goal.goal_id,
                    title=goal.title,
                    delegated_task=goal.goal,
                    final_message="Worker execution failed: Error code: 500 - internal server error",
                    preserve_context_used=goal.preserve_context,
                    status="failed",
                    failure_retryable=True,
                    failure_status_code=500,
                )
        return GoalExecutionResult(
            goal_id=goal.goal_id,
            title=goal.title,
            delegated_task=goal.goal,
            final_message="done",
            preserve_context_used=goal.preserve_context,
            status="completed",
            task_satisfied=True,
            validation_ran=True,
            validation_passed=True,
            validation_summary="validated",
        )

    def _plan_next_goal_guidance(self, plan, completed_goal, result):
        return ""

    def _synthesize_final_summary(self, plan, results):
        return "Execution complete." if all(item.status == "completed" for item in results) else "Execution paused."


class ModelRetryTests(unittest.TestCase):
    @patch.object(main.time, "sleep")
    @patch.object(main.random, "uniform", return_value=0.0)
    def test_repeated_500_gets_short_retries_then_sixty_second_fresh_retry(self, _jitter, sleep) -> None:
        client = FlakyModelClient(
            [
                HttpError(500, "internal server error"),
                HttpError(500, "internal server error"),
                HttpError(500, "internal server error"),
            ]
        )
        client.set_prompt_cache_key("treeloop:test")

        self.assertEqual(client.complete("system", "prompt"), "ok")
        self.assertEqual(client.calls, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0, 60.0])
        self.assertEqual(client.prompt_cache_key, "treeloop:test:recovery-1")
        self.assertEqual(client.get_last_metrics()["retry"]["attempts"], 4)

    @patch.object(main.time, "sleep")
    def test_retry_after_header_overrides_default_503_cooldown(self, sleep) -> None:
        client = FlakyModelClient(
            [HttpError(503, "service unavailable", headers={"retry-after": "17"})]
        )

        self.assertEqual(client.complete("system", "prompt"), "ok")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [17.0])

    @patch.object(main.time, "sleep")
    def test_recovered_retry_preserves_error_status_code_message_and_request(self, _sleep) -> None:
        client = FlakyModelClient(
            [
                HttpError(
                    500,
                    "internal server error",
                    request_id="meta-request-1",
                    code="server_error",
                )
            ]
        )

        self.assertEqual(client.complete("system", "prompt"), "ok")

        retry = client.get_last_metrics()["retry"]
        self.assertEqual(retry["attempts"], 2)
        self.assertEqual(
            retry["errors"],
            [
                {
                    "attempt": 1,
                    "status_code": 500,
                    "error_code": "server_error",
                    "message": "internal server error",
                    "request_id": "meta-request-1",
                    "exception_type": "HttpError",
                }
            ],
        )

    @patch.object(main.time, "sleep")
    def test_non_transient_400_is_not_retried(self, sleep) -> None:
        client = FlakyModelClient([HttpError(400, "bad request")])

        with self.assertRaises(HttpError):
            client.complete("system", "prompt")

        self.assertEqual(client.calls, 1)
        sleep.assert_not_called()

    @patch.object(main.time, "sleep")
    @patch.object(main.random, "uniform", return_value=0.0)
    def test_message_transcript_final_retry_uses_compacted_context(self, _jitter, sleep) -> None:
        client = FlakyModelClient([
            HttpError(500, "internal server error"),
            HttpError(500, "internal server error"),
            HttpError(500, "internal server error"),
        ])
        client.set_final_retry_message_compactor(
            lambda _messages: [{"role": "user", "content": "fresh compacted context"}]
        )

        self.assertEqual(client.complete_messages("system", [{"role": "user", "content": "prompt"}]), "ok")
        self.assertEqual(client.calls, 4)
        self.assertEqual(client.prompts[-1], "fresh compacted context")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0, 60.0])
        self.assertTrue(client.get_last_metrics()["retry"]["fresh_context_retry"])

    @patch.object(main.time, "sleep")
    @patch.object(main.random, "uniform", return_value=0.0)
    def test_final_failure_exposes_attempts_request_ids_and_rate_headers(self, _jitter, _sleep) -> None:
        failures = [
            HttpError(
                500,
                "internal server error",
                headers={"x-ratelimit-remaining-tokens": str(100 - index)},
                request_id=f"request-{index}",
            )
            for index in range(4)
        ]
        client = FlakyModelClient(failures)

        with self.assertRaises(HttpError) as raised:
            client.complete("system", "prompt")

        self.assertEqual(raised.exception.retry_attempts, 4)
        self.assertEqual(raised.exception.retry_request_ids, [f"request-{index}" for index in range(4)])
        self.assertEqual(raised.exception.retry_delays_s, [1.0, 2.0, 60.0])
        self.assertEqual(client.get_last_error_metrics()["rate_limit_headers"]["x-ratelimit-remaining-tokens"], "97")


class PlannerRecoveryTests(unittest.TestCase):
    def test_worker_exception_finalizes_observability_before_reraising(self) -> None:
        worker = object.__new__(TreeLoopPlannerWorker)
        worker._reload_repo_facts = lambda: None
        worker._reset_guard_state = lambda **_kwargs: None
        worker._reset_run_observability = lambda _task: None
        worker._refresh_loop_steering = lambda: None
        worker._run_sequence = 0
        worker._bridge_step_counter = 0
        worker._observability_metrics = {}
        worker.loop = SimpleNamespace(
            history=[],
            run=lambda _task: (_ for _ in ()).throw(HttpError(500, "internal server error")),
        )
        finalized: list[str] = []
        worker._flush_observability = lambda message: finalized.append(message)

        with self.assertRaises(HttpError):
            worker.run_task("continue partial work")

        self.assertEqual(finalized, ["Worker execution failed: internal server error"])
        self.assertFalse(worker._task_satisfied)
        self.assertEqual(worker._observability_metrics["outcome"], "failed")

    def test_partial_failure_summary_directs_retry_to_repair_current_diff(self) -> None:
        class PartialFailurePlanner(PlannerAgent):
            def _execute_goal_with_worker(self, worker, plan, goal, index):
                return GoalExecutionResult(
                    goal_id=goal.goal_id,
                    title=goal.title,
                    delegated_task=goal.goal,
                    final_message="Worker execution failed: internal server error",
                    touched_paths=["src/hooks/useTodoOperations.ts", "src/services/todoService.ts"],
                    status="failed",
                    failure_retryable=True,
                    failure_status_code=500,
                    validation_summary="TypeScript syntax error remains.",
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(
                root=str(root),
                provider="meta",
                model="muse-spark-1.2",
                thinking_mode="high",
                verbosity="medium",
                max_parallel_workers=1,
            )
            worker = NoopWorker(root)
            planner = PartialFailurePlanner(object(), config, lambda: worker, json.loads)
            goal = PlannerGoal("goal-1", "Extract service", "Extract service.", "Needed.")
            planner.session.pending_plan = PlannerPlan(
                original_request="Resume",
                summary="Resume partial work",
                goals=[goal],
            )

            message = planner.execute_pending_plan()

            self.assertIn("left partial repository changes", message)
            self.assertIn("repairing the current diff", message)
            self.assertNotIn("dependency-ready", message)
            self.assertTrue(any("do not restart this goal from scratch" in note for note in goal.delegation_notes))

    def test_reduced_paused_plan_repairs_stale_dependency_without_skipping_new_goals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(
                root=str(root),
                provider="meta",
                model="muse-spark-1.2",
                thinking_mode="high",
                verbosity="medium",
                max_parallel_workers=1,
            )
            worker = NoopWorker(root)
            planner = CheckpointPlanner(object(), config, lambda: worker, json.loads)
            planner.goal_two_attempts = 1
            old_summary = "Original four-goal issue plan"
            reduced_plan = PlannerPlan(
                original_request="Resume issue-014",
                summary="Complete the remaining two goals",
                goals=[
                    PlannerGoal(
                        "goal-1",
                        "Extract todoService",
                        "Extract the remaining data service.",
                        "First remaining goal.",
                        depends_on=["goal-2"],
                    ),
                    PlannerGoal(
                        "goal-2",
                        "Accessibility polish",
                        "Complete the remaining accessibility work.",
                        "Second remaining goal.",
                        depends_on=["goal-1"],
                    ),
                ],
            )
            ledger = IssueFactLedger.empty()
            issue = ledger.ensure_issue_open(
                request_summary="Implement issue-014",
                plan_summary=reduced_plan.summary,
            )
            for index in (1, 2):
                ledger.record_completed_goal(
                    issue_id=issue.issue_id,
                    checkpoint=CompletedGoalCheckpoint(
                        goal_id=f"goal-{index}",
                        plan_summary=old_summary,
                        final_message=f"Original goal {index} completed.",
                        original_index=index,
                        total_goal_count=4,
                        source="legacy_observability",
                    ),
                )
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")

            planner.session.active_issue_id = issue.issue_id
            planner.session.execution_paused = True
            planner.session.paused_plan = reduced_plan
            planner.session.paused_failure_reason = "No dependency-ready goals were available."

            before = planner.export_state()["resume_checkpoint"]
            self.assertEqual(before["prior_issue_completed_goal_count"], 2)
            self.assertEqual(before["completed_goal_count"], 0)
            self.assertEqual(before["next_goal_index"], 1)
            self.assertTrue(any("stale dependency goal-2" in item for item in before["dependency_repairs"]))

            message = planner.resume_paused_execution()

            self.assertIn("Execution complete", message)
            self.assertEqual(reduced_plan.goals[0].depends_on, [])
            self.assertEqual(reduced_plan.goals[1].depends_on, ["goal-1"])
            self.assertEqual([goal_id for goal_id, _ in planner.goal_calls], ["goal-1", "goal-2"])
            self.assertTrue(any("stale dependency goal-2" in item for item in reduced_plan.dependency_repairs))

    def test_unknown_dependency_is_blocked_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(
                root=str(root),
                provider="meta",
                model="muse-spark-1.2",
                thinking_mode="high",
                verbosity="medium",
                max_parallel_workers=1,
            )
            worker = NoopWorker(root)
            planner = CheckpointPlanner(object(), config, lambda: worker, json.loads)
            plan = PlannerPlan(
                original_request="Invalid dependency",
                summary="Invalid dependency",
                goals=[
                    PlannerGoal(
                        "goal-1",
                        "Only goal",
                        "Do work.",
                        "Needed.",
                        depends_on=["goal-99"],
                    )
                ],
            )
            planner.session.pending_plan = plan

            message = planner.execute_pending_plan()

            self.assertIn("dependency validation failed before execution", message.lower())
            self.assertIs(planner.session.pending_plan, plan)
            self.assertEqual(planner.goal_calls, [])
            self.assertEqual(
                planner.export_state()["suggested_next_actions"][0]["type"],
                "reject_plan",
            )

    def test_legacy_observability_backfills_only_prior_completed_goals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            ledger = IssueFactLedger.empty()
            issue = ledger.ensure_issue_open(
                request_summary="Proceed with issue-014",
                plan_summary="Implement four sequenced goals",
            )
            worker = object.__new__(TreeLoopPlannerWorker)
            worker.root = root
            worker.issue_ledger = ledger
            worker.active_issue_id = issue.issue_id
            worker._repo_facts_loaded_count = 0
            observability_path = root / "memory_observability.md"
            worker._observability_path = lambda: observability_path
            metrics = {
                "task": (
                    "Planner goal 3/4: Todo service\n\n"
                    "Prior goal results:\n"
                    "- goal-1: [finish: Callbacks complete.]\n"
                    "  planner commentary: next\n"
                    "- goal-2: [finish: Lazy views complete.]\n"
                    "  planner commentary: next\n\n"
                    "Use the discovery findings as the starting point"
                ),
                "root": str(root),
                "usage_accounting": {"current_issue": {"issue_id": issue.issue_id}},
            }
            observability_path.write_text(
                "# Memory Observability\n\n## Run Metrics\n```json\n"
                + json.dumps(metrics)
                + "\n```\n",
                encoding="utf-8",
            )

            imported = worker._import_legacy_observability_checkpoints()

            self.assertEqual(imported, 2)
            persisted = IssueFactLedger.load(root / "repo_facts.md").active_issue()
            self.assertIsNotNone(persisted)
            self.assertEqual([item.goal_id for item in persisted.completed_goals], ["goal-1", "goal-2"])
            self.assertTrue(all(item.source == "legacy_observability" for item in persisted.completed_goals))

            active = worker.issue_ledger.active_issue()
            self.assertIsNotNone(active)
            active.plan_summary = "A newer continuation summary"
            active.completed_goals.append(
                CompletedGoalCheckpoint(
                    goal_id="goal-1",
                    plan_summary=active.plan_summary,
                    final_message="Callbacks complete.",
                    source="legacy_observability",
                )
            )

            self.assertEqual(worker._import_legacy_observability_checkpoints(), 0)
            deduplicated = IssueFactLedger.load(root / "repo_facts.md").active_issue()
            self.assertEqual(len(deduplicated.completed_goals), 2)

    def test_regenerated_plan_recovers_durable_checkpoint_before_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(
                root=str(root),
                provider="meta",
                model="muse-spark-1.2",
                thinking_mode="high",
                verbosity="medium",
                max_parallel_workers=1,
            )
            worker = NoopWorker(root)
            planner = CheckpointPlanner(object(), config, lambda: worker, json.loads)
            plan = PlannerPlan(
                original_request="Proceed with issue-014",
                summary="Implement four sequenced goals",
                goals=[
                    PlannerGoal("goal-1", "Callbacks", "Stabilize callbacks.", "Needed."),
                    PlannerGoal("goal-2", "Lazy views", "Lazy-load views.", "Needed.", depends_on=["goal-1"]),
                    PlannerGoal("goal-3", "Todo service", "Extract todo service.", "Needed.", depends_on=["goal-2"]),
                    PlannerGoal("goal-4", "A11y", "Polish accessibility.", "Needed.", depends_on=["goal-3"]),
                ],
            )
            ledger = IssueFactLedger.empty()
            issue = ledger.ensure_issue_open(
                request_summary=plan.original_request,
                plan_summary=plan.summary,
            )
            for index, goal in enumerate(plan.goals[:2], start=1):
                ledger.record_completed_goal(
                    issue_id=issue.issue_id,
                    checkpoint=CompletedGoalCheckpoint(
                        goal_id=goal.goal_id,
                        title=goal.title,
                        goal_signature=planner._goal_signature(goal),
                        plan_summary=plan.summary,
                        final_message=f"{goal.title} done",
                        original_index=index,
                        total_goal_count=4,
                    ),
                )
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")

            planner.session.active_issue_id = issue.issue_id
            planner.session.pending_plan = plan
            planner.session.pending_checkpoint_results = planner._checkpoint_results_for_plan(plan)
            pending_state = planner.export_state()

            self.assertEqual(
                [item.goal_id for item in planner.session.pending_checkpoint_results],
                ["goal-1", "goal-2"],
            )
            self.assertEqual(pending_state["resume_checkpoint"]["mode"], "durable_continuation")
            self.assertEqual(pending_state["resume_checkpoint"]["next_goal_index"], 3)
            self.assertEqual(pending_state["suggested_next_actions"][0]["label"], "Resume from Goal 3")

            message = planner.execute_pending_plan()

            self.assertIn("Execution complete", message)
            self.assertEqual([goal_id for goal_id, _ in planner.goal_calls], ["goal-3", "goal-4"])

    def test_failed_goal_pauses_and_resume_skips_completed_goals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(
                root=str(root),
                provider="meta",
                model="muse-spark-1.2",
                thinking_mode="high",
                verbosity="medium",
                max_parallel_workers=1,
            )
            worker = NoopWorker(root)
            planner = CheckpointPlanner(object(), config, lambda: worker, json.loads)
            planner.session.pending_plan = PlannerPlan(
                original_request="Implement recovery",
                summary="Implement recovery",
                goals=[
                    PlannerGoal("goal-1", "First", "Complete first.", "Needed."),
                    PlannerGoal("goal-2", "Second", "Complete second.", "Needed.", depends_on=["goal-1"]),
                ],
            )

            first_message = planner.execute_pending_plan()
            paused_state = planner.export_state()

            self.assertIn("Execution paused", first_message)
            self.assertTrue(planner.session.execution_paused)
            self.assertIsNone(planner.session.pending_plan)
            self.assertEqual([item.goal_id for item in planner.session.paused_completed_results], ["goal-1"])
            self.assertEqual(worker.recorded_goal_ids, ["goal-1"])
            self.assertEqual(paused_state["status"], "execution_paused")
            self.assertEqual(paused_state["paused_failed_goal_id"], "goal-2")
            self.assertEqual(paused_state["paused_failure_status_code"], 500)
            self.assertTrue(paused_state["paused_failure_retryable"])
            self.assertEqual(paused_state["resume_checkpoint"]["completed_goal_ids"], ["goal-1"])
            self.assertEqual(paused_state["resume_checkpoint"]["next_goal_index"], 2)
            self.assertEqual(paused_state["resume_checkpoint"]["next_goal"]["goal_id"], "goal-2")
            self.assertFalse(paused_state["resume_checkpoint"]["completed_goals_will_rerun"])
            self.assertEqual(
                paused_state["suggested_next_actions"][0]["label"],
                "Retry Goal 2, Then Continue",
            )
            self.assertEqual(
                paused_state["suggested_next_actions"][0]["type"],
                "retry_failed_goal",
            )
            self.assertNotIn("continue_issue", [action["type"] for action in paused_state["suggested_next_actions"]])
            self.assertNotIn("approve_plan", [action["type"] for action in paused_state["suggested_next_actions"]])

            resumed_message = planner.resume_paused_execution()

            self.assertIn("Execution complete", resumed_message)
            self.assertFalse(planner.session.execution_paused)
            self.assertEqual([goal_id for goal_id, _ in planner.goal_calls], ["goal-1", "goal-2", "goal-2"])
            self.assertTrue(planner.goal_calls[-1][1])
            self.assertEqual(worker.recorded_goal_ids, ["goal-1", "goal-2"])
            self.assertIsNotNone(planner.session.last_completed_plan)
            self.assertEqual(planner.export_state()["status"], "completed")
            self.assertIsNone(planner.export_state()["resume_checkpoint"])

    def test_bridge_retry_action_resumes_without_an_approval_exchange(self) -> None:
        class ResumePlanner:
            def __init__(self) -> None:
                self.calls = 0

            def resume_paused_execution(self) -> str:
                self.calls += 1
                return "resumed"

        planner = ResumePlanner()
        exchanges: list[tuple[str, str]] = []

        message = main._handle_bridge_planner_action(
            planner=planner,
            transcript=[],
            request={"action": "retry_failed_goal"},
            add_exchange=lambda role, content: exchanges.append((role, content)),
        )

        self.assertEqual(message, "resumed")
        self.assertEqual(planner.calls, 1)
        self.assertEqual(exchanges, [("assistant", "resumed")])


if __name__ == "__main__":
    unittest.main()
