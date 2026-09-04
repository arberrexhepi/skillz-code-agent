from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

from issue_facts import FACT_TYPE_ARCHITECTURE, FACT_TYPE_GOAL, IssueFactLedger
from main import ActionResult, AgentConfig, AgentStep, WorkingFolderAgent, _handle_bridge_planner_action
from planner import DISCOVERY_MODES, DiscoveryRequest, DiscoveryResult, PlannerAgent, PlannerGoal, PlannerPlan


class FakeModelClient:
    def complete(self, system: str, prompt: str) -> str:
        return '{"thought": "", "action": {"type": "respond", "message": "unused"}}'

    def get_last_metrics(self) -> dict:
        return {}

    def clone(self) -> "FakeModelClient":
        return FakeModelClient()


class RecordingSequenceModelClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._responses:
            raise AssertionError("No model responses left")
        return self._responses.pop(0)

    def get_last_metrics(self) -> dict:
        return {}

    def clone(self) -> "RecordingSequenceModelClient":
        return RecordingSequenceModelClient(list(self._responses))


def make_agent(root: Path) -> WorkingFolderAgent:
    config = AgentConfig(
        provider="local",
        model="test-model",
        root=root,
        tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
        quiet=True,
    )
    return WorkingFolderAgent(FakeModelClient(), config)


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
    def __init__(self, *, ok: bool = True, has_mutation: bool = True) -> None:
        self.history = []
        self.root = Path.cwd()
        self.on_step_callback = None
        self.ok = ok
        self.has_mutation = has_mutation
        self.ensure_calls = []
        self.close_calls = 0
        self.closed_issue_ids = []
        self.reopen_calls = []
        self.persisted_discoveries = []
        self.delete_session_calls = 0
        self.created_issues = []

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

    def render_last_usage_summary(self) -> str:
        return "Usage: unavailable"

    def prepare_for_goal(self, preserve_context: bool) -> None:
        return None

    def ensure_issue_for_plan(self, *, original_request: str, plan_summary: str, reuse_issue_id: str = "") -> dict:
        self.ensure_calls.append(
            {
                "original_request": original_request,
                "plan_summary": plan_summary,
                "reuse_issue_id": reuse_issue_id,
            }
        )
        return {"issue_id": "issue-007", "plan_summary": plan_summary}

    def close_active_issue(self, *, note: str = "") -> dict:
        self.close_calls += 1
        return {"issue_id": "issue-007", "note": note}

    def close_issue(self, issue_id: str, *, note: str = "") -> dict:
        self.closed_issue_ids.append(issue_id)
        return {"issue_id": issue_id, "note": note}

    def reopen_issue(self, issue_id: str) -> dict:
        self.reopen_calls.append(issue_id)
        return {"issue_id": issue_id, "plan_summary": "Reopened work"}

    def persist_issue_discovery(self, issue_id: str, discovery: dict) -> dict:
        self.persisted_discoveries.append({"issue_id": issue_id, "discovery": discovery})
        return {"issue_id": issue_id, "has_last_discovery": bool(discovery)}

    def create_issue(
        self,
        *,
        request_summary: str,
        plan_summary: str = "",
        source: str = "",
        parent_issue_id: str = "",
        source_excerpt: str = "",
        priority: int = 0,
        activate: bool = True,
    ) -> dict:
        issue_id = f"issue-created-{len(self.created_issues) + 1}"
        issue = {
            "issue_id": issue_id,
            "request_summary": request_summary,
            "plan_summary": plan_summary or request_summary,
            "status": "open",
            "source": source,
            "parent_issue_id": parent_issue_id,
            "source_excerpt": source_excerpt,
            "priority": priority,
            "activate": activate,
        }
        self.created_issues.append(issue)
        return issue

    def delete_session(self) -> str:
        self.delete_session_calls += 1
        return "Session deleted. Repo facts and observability were cleared."

    def run_task(self, task: str):
        if self.ok and self.has_mutation:
            self.history = [
                SimpleNamespace(
                    action={"type": "patch_file", "path": "main.py"},
                    result=SimpleNamespace(ok=True, payload={"path": "main.py"}),
                )
            ]
        return SimpleNamespace(
            final_message="worker finished",
            ok=self.ok,
            task_satisfied=self.ok,
            validation_ran=False,
            validation_passed=False,
            touched_paths=[],
            validation=SimpleNamespace(summary=""),
        )


class PlannerBridgeStub:
    def __init__(self) -> None:
        self.session = SimpleNamespace(pending_discovery=None)
        self.closed_issue_ids: list[str] = []
        self.reopened_issue_ids: list[str] = []
        self.cleared = False
        self.deleted = False

    def close_issue(self, issue_id: str) -> str:
        self.closed_issue_ids.append(issue_id)
        return f"Closed issue {issue_id}."

    def reopen_issue(self, issue_id: str) -> str:
        self.reopened_issue_ids.append(issue_id)
        return f"Reopened issue {issue_id}."

    def clear_session(self) -> None:
        self.cleared = True

    def delete_session(self) -> str:
        self.deleted = True
        return "Session deleted. Repo facts and observability were cleared."


class BrokenExportWorkerStub(PlannerWorkerStub):
    def export_runtime_state(self) -> dict:
        raise RuntimeError("boom")


class IssueScopedFactsTests(unittest.TestCase):
    def test_legacy_flat_repo_facts_migrate_into_schema_v2(self) -> None:
        legacy_markdown = """# Repo Facts

```json
[
  {
    "key": "entrypoint",
    "value": "planner.py owns discovery mode",
    "source_action": "set_fact",
    "updated_step": 2,
    "updated_run_id": 1
  }
]
```
"""

        ledger = IssueFactLedger.from_markdown(legacy_markdown)

        self.assertEqual(ledger.schema_version, 2)
        self.assertEqual(ledger.total_fact_count(), 1)
        self.assertEqual(ledger.active_issue_id, "")
        self.assertEqual(ledger.issues[0].issue_id, "legacy-architecture")
        self.assertEqual(ledger.issues[0].facts[0].fact_type, FACT_TYPE_ARCHITECTURE)

    def test_goal_worker_set_fact_requires_fact_type_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = make_agent(Path(tmpdir))

            result = agent._execute_action(
                {
                    "type": "set_fact",
                    "key": "entrypoint",
                    "value": "planner.py owns discovery mode",
                }
            )

            self.assertFalse(result.ok)
            self.assertIn("fact_type", str(result.payload.get("error", "")))
            self.assertIn("next_hint", result.payload)

    def test_goal_worker_set_fact_duplicate_returns_repair_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = make_agent(Path(tmpdir))
            first = agent._execute_action(
                {
                    "type": "set_fact",
                    "key": "entrypoint",
                    "value": "planner.py owns discovery mode",
                    "fact_type": FACT_TYPE_GOAL,
                }
            )
            self.assertTrue(first.ok)

            second = agent._execute_action(
                {
                    "type": "set_fact",
                    "key": "entrypoint",
                    "value": "main.py owns the stable runtime",
                    "fact_type": FACT_TYPE_GOAL,
                }
            )

            self.assertFalse(second.ok)
            self.assertEqual(second.name, "set_fact")
            self.assertIn("update_fact", str(second.payload.get("next_hint", "")))
            record = agent.issue_ledger.find_fact("entrypoint", fact_type=FACT_TYPE_GOAL)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.value, "planner.py owns discovery mode")

    def test_goal_worker_malformed_fact_returns_repair_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = make_agent(Path(tmpdir))

            result = agent._execute_action(
                {
                    "type": "set_fact",
                    "key": "entrypoint",
                    "value": "",
                    "fact_type": FACT_TYPE_GOAL,
                }
            )

            self.assertFalse(result.ok)
            self.assertIn("next_hint", result.payload)
            self.assertIsNone(agent.issue_ledger.find_fact("entrypoint"))

    def test_goal_worker_update_unknown_fact_returns_repair_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = make_agent(Path(tmpdir))

            result = agent._execute_action(
                {
                    "type": "update_fact",
                    "key": "entrypoint",
                    "value": "planner.py owns discovery mode",
                    "fact_type": FACT_TYPE_ARCHITECTURE,
                }
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.name, "update_fact")
            self.assertIn("set_fact", str(result.payload.get("next_hint", "")))
            self.assertIsNone(agent.issue_ledger.find_fact("entrypoint", fact_type=FACT_TYPE_ARCHITECTURE))

    def test_goal_worker_parse_error_enters_output_format_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Dashboard.tsx").write_text("export default function Dashboard() { return null; }\n", encoding="utf-8")
            model = RecordingSequenceModelClient(
                [
                    '{"thought":"Verify Dashboard before editing.","action":{"type":"read_file","path":"Dashboard.tsx"}}',
                    "I will update `Dashboard.tsx` to include the External Integrations section.",
                    '{"thought":"Recover by finishing after the format correction.","action":{"type":"finish","message":"Stopped after format recovery."}}',
                ]
            )
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                max_steps=3,
                quiet=True,
            )
            agent = WorkingFolderAgent(model, config)

            result = agent.run_task("Update dashboard integrations")

            self.assertTrue(result.ok)
            self.assertEqual(agent.history[1].action.get("type"), "output_format_error")
            self.assertEqual(agent.history[1].result.name, "parse_error")
            self.assertIsNone(agent.output_format_recovery)
            self.assertGreaterEqual(len(model.prompts), 3)
            recovery_prompt = model.prompts[2]
            self.assertIn('"output_format_recovery"', recovery_prompt)
            self.assertIn("Do not repeat prose plans or markdown", recovery_prompt)
            self.assertIn("Produce exactly one JSON object", recovery_prompt)

    def test_close_active_issue_records_lifecycle_note(self) -> None:
        ledger = IssueFactLedger()

        opened = ledger.ensure_issue_open(request_summary="Recover stale issue", plan_summary="Manual close flow")
        closed = ledger.close_active_issue(note="Closed manually from the VS Code Issues panel.")

        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertEqual(closed.issue_id, opened.issue_id)
        self.assertEqual(closed.status, "closed")
        self.assertEqual(closed.lifecycle_notes, ["Closed manually from the VS Code Issues panel."])

    def test_repo_facts_auto_compacts_older_closed_issue_facts(self) -> None:
        ledger = IssueFactLedger()
        issue_ids: list[str] = []
        for index in range(4):
            issue = ledger.ensure_issue_open(
                request_summary=f"Request {index}",
                plan_summary=f"Plan {index}",
            )
            issue_ids.append(issue.issue_id)
            ledger.upsert_fact(
                key=f"detail_{index}",
                value=f"Long historical detail {index}",
                fact_type=FACT_TYPE_GOAL,
                source_action="set_fact",
                updated_step=index + 1,
                updated_run_id=1,
                issue_id=issue.issue_id,
                task_summary=f"Task {index}",
            )
            ledger.close_issue(issue.issue_id, note=f"Closed note {index}")

        payload = ledger.to_payload()
        issues = {item["issue_id"]: item for item in payload["issues"]}

        self.assertEqual(issues[issue_ids[0]]["facts"], [])
        self.assertTrue(issues[issue_ids[1]]["facts"])
        self.assertTrue(issues[issue_ids[2]]["facts"])
        self.assertTrue(issues[issue_ids[3]]["facts"])

    def test_stable_observability_streams_partial_snapshots_and_compacts_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            agent = WorkingFolderAgent(FakeModelClient(), config)
            observability_path = root / "memory_observability.test.md"

            with mock.patch.object(agent, "_observability_targets", return_value=[observability_path]):
                agent._reset_run_observability("Demo task")
                for index in range(30):
                    agent._append_observability_block(f"block-{index}\n")

            content = observability_path.read_text(encoding="utf-8")
            self.assertIn("Run in progress.", content)
            self.assertIn("Auto-compacted observability trace", content)
            self.assertIn("block-29", content)
            self.assertNotIn("block-0", content)

    def test_stable_delete_session_clears_runtime_and_deletes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            agent = WorkingFolderAgent(FakeModelClient(), config)
            observability_path = root / "memory_observability.test.md"

            agent.ensure_issue_for_plan(original_request="Repair planner", plan_summary="Track active issue")
            agent._set_fact_record(
                "planner_entrypoint",
                "planner.py owns discovery mode",
                source_action="set_fact",
                fact_type=FACT_TYPE_ARCHITECTURE,
            )
            with mock.patch.object(agent, "_observability_targets", return_value=[observability_path]):
                observability_path.write_text("trace", encoding="utf-8")
                repo_facts_path = agent._repo_facts_path()
                repo_facts_path.write_text(agent._serialize_repo_facts_markdown(), encoding="utf-8")

                message = agent.delete_session()

            self.assertEqual(message, "Session deleted. Repo facts and observability were cleared.")
            self.assertFalse(repo_facts_path.exists())
            self.assertFalse(observability_path.exists())
            self.assertEqual(agent.issue_ledger.total_fact_count(), 0)
            self.assertEqual(agent.fact_map, {})
            self.assertEqual(agent.history, [])

    def test_stable_runtime_loads_bundled_skills_into_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            agent = WorkingFolderAgent(FakeModelClient(), config)

            state = agent.export_runtime_state()
            available_skills = state.get("available_skills", [])
            discovery_skill = next((item for item in available_skills if item.get("name") == "codebase-discovery"), None)
            diagnostics_skill = next((item for item in available_skills if item.get("name") == "codebase-diagnostics"), None)
            mutation_skill = next((item for item in available_skills if item.get("name") == "codebase-mutation"), None)

            self.assertIsNotNone(discovery_skill)
            self.assertEqual(discovery_skill["category"], "general")
            self.assertEqual(discovery_skill["priority"], 0)
            self.assertEqual(discovery_skill["tags"], [])
            self.assertEqual(discovery_skill["modes"], ["fast", "standard", "deep"])

            self.assertIsNotNone(diagnostics_skill)
            self.assertEqual(diagnostics_skill["modes"], ["syntax_and_structure", "semantics_and_types", "integration_and_runtime", "governance_and_risk", "aggregate"])

            self.assertIsNotNone(mutation_skill)
            self.assertEqual(mutation_skill["modes"], ["surgical_text", "structural_code", "filesystem", "coordinated_batch"])

    def test_stable_runtime_skill_action_returns_markdown_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            agent = WorkingFolderAgent(FakeModelClient(), config)

            result = agent._execute_action({"type": "skill", "name": "codebase-diagnostics"})

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["category"], "general")
            self.assertEqual(result.payload["priority"], 0)
            self.assertEqual(result.payload["tags"], [])
            self.assertEqual(result.payload["modes"], ["syntax_and_structure", "semantics_and_types", "integration_and_runtime", "governance_and_risk", "aggregate"])
            self.assertIn(">[global: diagnostics_rules]", result.payload["content"])

    def test_stable_runtime_skill_action_supports_mode_contract_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            agent = WorkingFolderAgent(FakeModelClient(), config)

            result = agent._execute_action({"type": "skill", "name": "codebase-discovery", "mode": "standard"})

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["mode"], "standard")
            self.assertIn("available_modes", result.payload["content"])
            self.assertIn("mode: standard", result.payload["content"])
            self.assertIn("find_symbol_definitions", result.payload["content"])
            self.assertNotIn("[deep]", result.payload["content"])

    def test_stable_runtime_mutation_skill_returns_markdown_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            agent = WorkingFolderAgent(FakeModelClient(), config)

            result = agent._execute_action({"type": "skill", "name": "codebase-mutation", "mode": "filesystem"})

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["category"], "general")
            self.assertEqual(result.payload["priority"], 0)
            self.assertEqual(result.payload["tags"], [])
            self.assertEqual(result.payload["mode"], "filesystem")
            self.assertIn("create_file", result.payload["content"])
            self.assertIn("fill_template", result.payload["content"])

    def test_planner_discovery_prompt_mentions_skill_loading(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = SimpleNamespace(root=root, provider="local", model="test-model", thinking_mode="medium", verbosity="medium")
            planner = PlannerAgent(
                FakeModelClient(),
                config,
                worker_factory=lambda: PlannerWorkerStub(),
                json_loader=lambda text: {},
            )
            planner.session.latest_request = "Investigate how discovery should choose safe edit targets"
            request = DiscoveryRequest(
                reason="Need repo-level investigation before planning.",
                prompt="Inspect the relevant architecture and constraints.",
                recommended_mode="moderate",
            )

            steering = planner._build_discovery_steering(request, DISCOVERY_MODES["moderate"])
            task = planner._build_discovery_task(request, DISCOVERY_MODES["moderate"])

            self.assertIn("inspect available skills once", steering)
            self.assertIn("skill <name> mode=<mode>", steering)
            self.assertIn("check whether a bundled or workspace skill contract is relevant", task)
            self.assertIn("skill <name> mode=<mode>", task)

    def test_stable_runtime_system_prompt_prefers_list_files_for_directory_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            agent = WorkingFolderAgent(FakeModelClient(), config)

            system = agent._system_prompt()

            self.assertIn("`list_files` before `read_file`", system)
            self.assertIn("directory or the next question is about repo/file topology", system)

    def test_worker_context_hides_closed_goal_facts_until_issue_is_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            agent = WorkingFolderAgent(FakeModelClient(), config)

            issue = agent.ensure_issue_for_plan(original_request="Fix planner facts", plan_summary="Implement issue facts")
            issue_id = str(issue.get("issue_id", "") or "")

            agent._set_fact_record(
                "planner_entrypoint",
                "planner.py owns discovery mode",
                source_action="set_fact",
                fact_type=FACT_TYPE_ARCHITECTURE,
            )
            agent._set_fact_record(
                "active_issue_note",
                "Issue-local constraint for the current repair",
                source_action="set_fact",
                fact_type=FACT_TYPE_GOAL,
            )
            agent.close_active_issue()
            agent._clear_facts()

            self.assertIn("planner_entrypoint", agent.fact_map)
            self.assertNotIn("active_issue_note", agent.fact_map)

            agent.reopen_issue(issue_id)
            agent._clear_facts()

            self.assertIn("planner_entrypoint", agent.fact_map)
            self.assertIn("active_issue_note", agent.fact_map)
            self.assertEqual(agent.fact_map["active_issue_note"].fact_type, FACT_TYPE_GOAL)

    def test_planner_opens_and_closes_issue_on_successful_execution(self) -> None:
        worker = PlannerWorkerStub(ok=True)
        planner = PlannerAgent(
            model_client=FakeModelClient(),
            config=SimpleNamespace(
                root=Path.cwd(),
                provider="local",
                model="test-model",
                thinking_mode="medium",
                verbosity="medium",
                max_parallel_workers=1,
            ),
            worker_factory=lambda: worker,
            json_loader=lambda text: {},
        )
        planner.worker = worker
        planner.session.pending_plan = PlannerPlan(
            original_request="Implement issue facts",
            summary="Ship issue-scoped fact ledger",
            goals=[
                PlannerGoal(
                    goal_id="goal-1",
                    title="Implement ledger",
                    goal="Update runtime persistence and prompts.",
                    reason="Required for issue-scoped durable facts.",
                )
            ],
        )

        message = planner.execute_pending_plan()

        self.assertIn("issue-007", message)
        self.assertEqual(len(worker.ensure_calls), 1)
        self.assertEqual(worker.close_calls, 1)
        self.assertEqual(planner.session.active_issue_id, "")

    def test_planner_leaves_issue_open_when_execution_fails(self) -> None:
        worker = PlannerWorkerStub(ok=False)
        planner = PlannerAgent(
            model_client=FakeModelClient(),
            config=SimpleNamespace(
                root=Path.cwd(),
                provider="local",
                model="test-model",
                thinking_mode="medium",
                verbosity="medium",
                max_parallel_workers=1,
            ),
            worker_factory=lambda: worker,
            json_loader=lambda text: {},
        )
        planner.worker = worker
        planner.session.pending_plan = PlannerPlan(
            original_request="Implement issue facts",
            summary="Ship issue-scoped fact ledger",
            goals=[
                PlannerGoal(
                    goal_id="goal-1",
                    title="Implement ledger",
                    goal="Update runtime persistence and prompts.",
                    reason="Required for issue-scoped durable facts.",
                )
            ],
        )

        planner.execute_pending_plan()

        self.assertEqual(len(worker.ensure_calls), 1)
        self.assertEqual(worker.close_calls, 0)
        self.assertEqual(planner.session.active_issue_id, "issue-007")

    def test_failed_multi_goal_plan_surfaces_retry_state_not_dependency_deadlock(self) -> None:
        worker = PlannerWorkerStub(ok=False)
        planner = PlannerAgent(
            model_client=FakeModelClient(),
            config=SimpleNamespace(
                root=Path.cwd(),
                provider="local",
                model="test-model",
                thinking_mode="medium",
                verbosity="medium",
                max_parallel_workers=1,
            ),
            worker_factory=lambda: worker,
            json_loader=lambda text: {},
        )
        planner.worker = worker
        planner.session.pending_plan = PlannerPlan(
            original_request="Rename Timesheet",
            summary="Rename Timesheet to Shift Clock",
            goals=[
                PlannerGoal(
                    goal_id="goal-1",
                    title="Rename Screen Component",
                    goal="Rename the primary screen component.",
                    reason="Needed before dependents update imports.",
                ),
                PlannerGoal(
                    goal_id="goal-2",
                    title="Update Navigation and Routing",
                    goal="Update labels and routes.",
                    reason="Depends on the renamed screen.",
                    depends_on=["goal-1"],
                ),
            ],
        )

        message = planner.execute_pending_plan()
        state = planner.export_state()

        self.assertIn("failed goal: Rename Screen Component", message)
        self.assertNotIn("no dependency-ready goals", message)
        self.assertTrue(state["execution_paused"])
        self.assertIsNotNone(state["paused_plan"])
        self.assertEqual(state["suggested_next_actions"][0]["type"], "retry_failed_goal")

    def test_write_goal_without_mutation_does_not_auto_complete(self) -> None:
        worker = PlannerWorkerStub(ok=True, has_mutation=False)
        planner = PlannerAgent(
            model_client=FakeModelClient(),
            config=SimpleNamespace(
                root=Path.cwd(),
                provider="codex-subscription",
                model="test-model",
                thinking_mode="medium",
                verbosity="medium",
                max_parallel_workers=1,
            ),
            worker_factory=lambda: worker,
            json_loader=lambda text: {},
        )
        planner.worker = worker
        planner.session.pending_plan = PlannerPlan(
            original_request="Update app behavior",
            summary="Make two write changes",
            goals=[
                PlannerGoal(
                    goal_id="goal-1",
                    title="Update first file",
                    goal="Patch the first implementation file.",
                    reason="Required by the request.",
                    estimated_scope="write",
                ),
                PlannerGoal(
                    goal_id="goal-2",
                    title="Update second file",
                    goal="Patch the second implementation file.",
                    reason="Depends on the first change.",
                    depends_on=["goal-1"],
                    estimated_scope="write",
                ),
            ],
        )

        message = planner.execute_pending_plan()
        state = planner.export_state()

        self.assertIn("failed goal: Update first file", message)
        self.assertIn("without performing or validating", message)
        self.assertIsNotNone(planner.session.paused_plan)
        self.assertTrue(state["execution_paused"])
        self.assertEqual(len(planner.session.last_completed_results), 1)
        self.assertEqual(planner.session.last_completed_results[0].status, "failed")

    def test_strategy_prefixed_mutation_command_counts_as_mutation_evidence(self) -> None:
        planner = PlannerAgent(
            model_client=FakeModelClient(),
            config=SimpleNamespace(
                root=Path.cwd(),
                provider="local",
                model="test-model",
                thinking_mode="medium",
                verbosity="medium",
                max_parallel_workers=1,
            ),
            worker_factory=lambda: PlannerWorkerStub(),
            json_loader=lambda text: {},
        )
        steps = [SimpleNamespace(results=[SimpleNamespace(
            tool_action={"type": "patch_file", "path": "components/App.tsx"},
            ok=True,
        )])]

        self.assertTrue(planner._history_has_successful_workspace_mutation(steps))

    def test_planner_export_state_surfaces_reopen_issue_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            worker_agent = WorkingFolderAgent(FakeModelClient(), config)
            issue = worker_agent.ensure_issue_for_plan(original_request="Repair planner", plan_summary="Initial repair")
            issue_id = str(issue.get("issue_id", "") or "")
            worker_agent._set_fact_record(
                "planner_entrypoint",
                "planner.py owns discovery mode",
                source_action="set_fact",
                fact_type=FACT_TYPE_ARCHITECTURE,
            )
            worker_agent.close_active_issue()

            planner = PlannerAgent(
                model_client=FakeModelClient(),
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: worker_agent,
                json_loader=lambda text: {},
            )
            planner.worker = worker_agent

            state = planner.export_state()

            actions = state.get("suggested_next_actions", [])
            reopen_actions = [item for item in actions if item.get("type") == "reopen_issue"]
            self.assertTrue(reopen_actions)
            self.assertEqual(reopen_actions[0].get("issue_id"), issue_id)
            issue_state = state.get("issue_state", {})
            self.assertEqual(issue_state.get("schema_version"), 2)
            self.assertTrue(issue_state.get("reopenable_issues"))

    def test_bridge_planner_action_reopen_issue_reads_issue_id_from_payload(self) -> None:
        planner = PlannerBridgeStub()
        transcript = [{"role": "assistant", "content": "Earlier output"}]
        exchanges: list[tuple[str, str]] = []

        message = _handle_bridge_planner_action(
            planner=planner,
            transcript=transcript,
            request={"action": "reopen_issue", "payload": {"issue_id": "issue-021"}},
            add_exchange=lambda role, content: exchanges.append((role, content)),
        )

        self.assertEqual(message, "Reopened issue issue-021.")
        self.assertEqual(planner.reopened_issue_ids, ["issue-021"])
        self.assertEqual(exchanges, [("assistant", "Reopened issue issue-021.")])

    def test_bridge_planner_action_close_issue_reads_issue_id_from_payload(self) -> None:
        planner = PlannerBridgeStub()
        transcript = [{"role": "assistant", "content": "Earlier output"}]
        exchanges: list[tuple[str, str]] = []

        message = _handle_bridge_planner_action(
            planner=planner,
            transcript=transcript,
            request={"action": "close_issue", "payload": {"issue_id": "issue-021"}},
            add_exchange=lambda role, content: exchanges.append((role, content)),
        )

        self.assertEqual(message, "Closed issue issue-021.")
        self.assertEqual(planner.closed_issue_ids, ["issue-021"])
        self.assertEqual(exchanges, [("assistant", "Closed issue issue-021.")])

    def test_bridge_planner_action_delete_session_clears_transcript(self) -> None:
        planner = PlannerBridgeStub()
        transcript = [{"role": "assistant", "content": "Earlier output"}]
        exchanges: list[tuple[str, str]] = []

        message = _handle_bridge_planner_action(
            planner=planner,
            transcript=transcript,
            request={"action": "delete_session"},
            add_exchange=lambda role, content: exchanges.append((role, content)),
        )

        self.assertEqual(message, "Session deleted. Repo facts and observability were cleared.")
        self.assertTrue(planner.deleted)
        self.assertEqual(transcript, [])
        self.assertEqual(exchanges, [("assistant", "Session deleted. Repo facts and observability were cleared.")])

    def test_planner_export_state_surfaces_delete_session_action_when_issue_state_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            worker_agent = WorkingFolderAgent(FakeModelClient(), config)
            worker_agent.ensure_issue_for_plan(original_request="Repair planner", plan_summary="Initial repair")
            worker_agent.close_active_issue()

            planner = PlannerAgent(
                model_client=FakeModelClient(),
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: worker_agent,
                json_loader=lambda text: {},
            )
            planner.worker = worker_agent

            actions = planner.export_state().get("suggested_next_actions", [])
            delete_actions = [item for item in actions if item.get("type") == "delete_session"]

            self.assertTrue(delete_actions)
            self.assertTrue(delete_actions[0].get("requires_confirmation"))

    def test_try_builtin_command_closes_issue_from_submit_prompt(self) -> None:
        worker = PlannerWorkerStub(ok=True)
        planner = PlannerAgent(
            model_client=FakeModelClient(),
            config=SimpleNamespace(
                root=Path.cwd(),
                provider="local",
                model="test-model",
                thinking_mode="medium",
                verbosity="medium",
                max_parallel_workers=1,
            ),
            worker_factory=lambda: worker,
            json_loader=lambda text: {},
        )
        planner.worker = worker
        planner.session.active_issue_id = "issue-005"

        message = planner.start_request("/close-issue issue-005")

        self.assertEqual(message, "Closed issue issue-005. It will stay out of active context until reopened.")
        self.assertEqual(worker.closed_issue_ids, ["issue-005"])
        self.assertEqual(planner.session.active_issue_id, "")

    def test_discovery_rehydrates_preexisting_active_issue_before_plan_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = IssueFactLedger.empty()
            issue = ledger.ensure_issue_open(
                request_summary="Repair discovery follow-up",
                plan_summary="Resume existing issue after discovery",
            )
            active_issue_id = issue.issue_id
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")

            worker = PlannerWorkerStub(ok=True)
            model = SequenceModelClient(
                [
                    '{"thought": "", "action": {"type": "offer_discovery", "reason": "Need discovery", "prompt": "Choose discovery depth.", "recommended_mode": "quick"}}',
                    '{"thought": "", "action": {"type": "present_plan", "summary": "Resume issue work", "goals": [{"goal_id": "goal-1", "title": "Implement fix", "goal": "Apply the discovered change.", "reason": "Discovery identified the target."}]}}',
                    '{"summary": "Completed the resumed issue work.", "next_steps": []}',
                ]
            )
            planner = PlannerAgent(
                model_client=model,
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: worker,
                json_loader=json.loads,
            )
            planner.worker = worker

            offer = planner.start_request("Fix the existing issue")

            self.assertIn("discovery", offer.lower())
            self.assertEqual(planner.session.active_issue_id, active_issue_id)

            discovery_message = planner.execute_discovery("quick")

            self.assertIn("Resume issue work", discovery_message)
            self.assertIsNotNone(planner.session.pending_plan)
            self.assertEqual(planner.session.active_issue_id, active_issue_id)

            planner.execute_pending_plan()

            self.assertEqual(len(worker.ensure_calls), 1)
            self.assertEqual(worker.ensure_calls[0]["reuse_issue_id"], active_issue_id)

    def test_execute_discovery_keeps_fresh_result_in_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = IssueFactLedger.empty()
            issue = ledger.ensure_issue_open(
                request_summary="Fix Kanban clipping",
                plan_summary="Repair Kanban board card clipping",
            )
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")

            worker = PlannerWorkerStub(ok=True)
            worker.root = root
            planner = PlannerAgent(
                model_client=SequenceModelClient(
                    [
                        '{"thought": "", "action": {"type": "present_plan", "summary": "Use discovery", "goals": [{"goal_id": "goal-1", "title": "Fix clipping", "goal": "Use ProjectKanban findings.", "reason": "Discovery identified ProjectKanban.tsx."}]}}',
                    ]
                ),
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: worker,
                json_loader=json.loads,
            )
            planner.worker = worker
            planner.session.active_issue_id = issue.issue_id
            planner.session.pending_discovery = DiscoveryRequest(
                reason="Need Kanban context",
                prompt="Choose discovery",
                recommended_mode="quick",
            )

            planner.execute_discovery("quick")

            self.assertEqual(worker.persisted_discoveries, [])
            self.assertIsNotNone(planner.session.last_discovery)
            assert planner.session.last_discovery is not None
            self.assertEqual(planner.session.last_discovery.mode, "quick")
            self.assertIn("worker finished", planner.session.last_discovery.final_message)
            self.assertIsNotNone(planner.session.pending_plan)

    def test_reopened_issue_starts_with_fresh_session_discovery_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = IssueFactLedger.empty()
            issue = ledger.ensure_issue_open(
                request_summary="Fix Kanban clipping",
                plan_summary="Repair Kanban board card clipping",
            )
            ledger.close_issue(issue.issue_id, note="Closed before follow-up")
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")

            worker = PlannerWorkerStub(ok=True)
            model = RecordingSequenceModelClient(
                [
                    '{"thought": "", "action": {"type": "present_plan", "summary": "Plan reopened issue", "goals": [{"goal_id": "goal-1", "title": "Fix clipping", "goal": "Repair the reported clipping.", "reason": "The reopened issue remains active.", "not_in_scope": ["Unrelated dashboard pages"]}]}}',
                ]
            )
            planner = PlannerAgent(
                model_client=model,
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: worker,
                json_loader=json.loads,
            )
            planner.worker = worker

            reopen_message = planner.reopen_issue(issue.issue_id)
            response = planner.start_request("Kanban cards are still clipping")

            self.assertIn("Reopened issue", reopen_message)
            self.assertIsNone(planner.session.last_discovery)
            self.assertIn("Plan reopened issue", response)
            prompt = model.prompts[0]
            payload = json.loads(prompt)
            self.assertEqual(payload["request"], "Kanban cards are still clipping")
            self.assertNotIn("last_discovery", payload)

    def test_planner_prompt_includes_detailed_discovery_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            planner = PlannerAgent(
                model_client=FakeModelClient(),
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: PlannerWorkerStub(),
                json_loader=json.loads,
            )
            planner.session.latest_request = "Fix the discovery handoff"
            planner.session.intake_messages = [{"role": "user", "content": "Fix the discovery handoff"}]
            planner.session.last_discovery = DiscoveryResult(
                mode="moderate",
                delegated_task="Inspect the likely planner entrypoints",
                final_message="Discovery found the planner handoff path.",
                ok=True,
                worker_history_summary=[
                    {"action_type": "read_file", "path": "planner.py", "summary": "Discovery handoff only passes final_message into planning context."},
                    {"action_type": "read_file", "path": "main.py", "summary": "Stable and beta both share the same planner JSON loader."},
                ],
                touched_paths=["planner.py", "main.py"],
            )

            payload = json.loads(planner._build_planner_prompt())

            self.assertEqual(
                payload["last_discovery_findings"][:3],
                [
                    "Discovery found the planner handoff path.",
                    "read_file | planner.py | Discovery handoff only passes final_message into planning context.",
                    "read_file | main.py | Stable and beta both share the same planner JSON loader.",
                ],
            )
            self.assertEqual(
                payload["last_discovery"]["detailed_findings"][:2],
                [
                    "Discovery found the planner handoff path.",
                    "read_file | planner.py | Discovery handoff only passes final_message into planning context.",
                ],
            )

    def test_planner_prompt_includes_project_intent_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("# Project Intent\n\nShip continuous agent mode.\n", encoding="utf-8")
            planner = PlannerAgent(
                model_client=FakeModelClient(),
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: PlannerWorkerStub(),
                json_loader=json.loads,
            )
            planner.session.latest_request = "Continue project work"
            planner.session.intake_messages = [{"role": "user", "content": "Continue project work"}]

            payload = json.loads(planner._build_planner_prompt())

            self.assertTrue(payload["project_intent"]["present"])
            self.assertTrue(payload["project_intent"]["immutable"])
            self.assertIn("Ship continuous agent mode", payload["project_intent"]["content"])

    def test_worker_blocks_intent_mutations_but_allows_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("# Intent\nDo not mutate this file.\n", encoding="utf-8")
            agent = make_agent(root)

            read_result = agent._execute_action({"type": "read_file", "path": "INTENT.md"})
            write_result = agent._execute_action({"type": "write_file", "path": "INTENT.md", "content": "changed"})
            patch_result = agent._execute_action({"type": "patch_file", "path": "/repo/INTENT.md", "search": "Intent", "replace": "Changed"})
            batch_result = agent._execute_action({
                "type": "batch_mutate",
                "operations": [{"type": "replace_snippet", "path": "./INTENT.md", "search": "Intent", "replace": "Changed"}],
            })

            self.assertTrue(read_result.ok)
            for result in [write_result, patch_result, batch_result]:
                self.assertFalse(result.ok)
                self.assertEqual(result.payload.get("code"), "PROTECTED_PATH")
                self.assertEqual(result.payload.get("protected_paths"), ["INTENT.md"])

    def test_continuous_mode_auto_approves_reviews_and_closes_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("# Continuous Agent\n\nImplement the next safe improvement.\n", encoding="utf-8")
            worker = PlannerWorkerStub(ok=True)
            model = SequenceModelClient(
                [
                    json.dumps({
                        "thought": "",
                        "action": {
                            "type": "present_plan",
                            "summary": "Implement the next safe improvement.",
                            "not_in_scope": ["INTENT.md"],
                            "goals": [
                                {
                                    "goal_id": "goal-1",
                                    "title": "Implement safe improvement",
                                    "goal": "Apply the bounded project improvement.",
                                    "reason": "INTENT.md identifies this as the next project direction.",
                                    "estimated_scope": "write",
                                    "success_signals": ["Worker reports the bounded improvement is complete."],
                                }
                            ],
                        },
                    }),
                    json.dumps({"summary": "Completed the safe improvement.", "next_steps": ["Document the continuous mode rollout checklist."]}),
                ]
            )
            planner = PlannerAgent(
                model_client=model,
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: worker,
                json_loader=json.loads,
            )
            planner.worker = worker

            message = planner.start_continuous(max_cycles=1)

            self.assertIn("auto-approved", message)
            self.assertEqual(worker.close_calls, 1)
            self.assertEqual(planner.continuous_state.latest_review_decision, "accepted")
            self.assertEqual(planner.continuous_state.stop_reason, "max_cycles_reached")
            self.assertTrue(worker.created_issues)
            self.assertEqual(worker.created_issues[0]["source"], "intent")
            self.assertEqual(worker.created_issues[-1]["source"], "next_step")
            self.assertEqual(
                planner.continuous_state.created_followup_issues[-1]["plan_summary"],
                "Document the continuous mode rollout checklist.",
            )

    def test_continuous_mode_runs_followup_issue_on_next_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("# Continuous Agent\n\nImplement the next safe improvement.\n", encoding="utf-8")
            worker = PlannerWorkerStub(ok=True)
            model = SequenceModelClient(
                [
                    json.dumps({
                        "thought": "",
                        "action": {
                            "type": "present_plan",
                            "summary": "Implement the next safe improvement.",
                            "not_in_scope": ["INTENT.md"],
                            "goals": [
                                {
                                    "goal_id": "goal-1",
                                    "title": "Implement safe improvement",
                                    "goal": "Apply the bounded project improvement.",
                                    "reason": "Project intent identifies this as the next direction.",
                                    "estimated_scope": "write",
                                    "success_signals": ["Worker reports the bounded improvement is complete."],
                                }
                            ],
                        },
                    }),
                    json.dumps({"summary": "Completed the safe improvement.", "next_steps": ["Document the continuous mode rollout checklist."]}),
                    json.dumps({
                        "thought": "",
                        "action": {
                            "type": "present_plan",
                            "summary": "Document the continuous mode rollout checklist.",
                            "not_in_scope": ["INTENT.md"],
                            "goals": [
                                {
                                    "goal_id": "goal-1",
                                    "title": "Document rollout checklist",
                                    "goal": "Document the continuous mode rollout checklist.",
                                    "reason": "Follow-up issue from the prior accepted cycle.",
                                    "estimated_scope": "write",
                                    "success_signals": ["Worker reports the checklist is documented."],
                                }
                            ],
                        },
                    }),
                    json.dumps({"summary": "Documented the rollout checklist.", "next_steps": []}),
                ]
            )
            planner = PlannerAgent(
                model_client=model,
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: worker,
                json_loader=json.loads,
            )
            planner.worker = worker

            message = planner.start_continuous(max_cycles=2)

            self.assertIn("Cycle 1: auto-approved", message)
            self.assertIn("Cycle 2: auto-approved", message)
            self.assertEqual(worker.close_calls, 2)
            self.assertEqual(planner.continuous_state.max_cycles, 2)
            self.assertEqual(planner.continuous_state.cycle, 2)

    def test_continuous_mode_builtin_command_starts_with_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("# Continuous Agent\n\nImplement the next safe improvement.\n", encoding="utf-8")
            worker = PlannerWorkerStub(ok=True)
            planner = PlannerAgent(
                model_client=FakeModelClient(),
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: worker,
                json_loader=json.loads,
            )
            planner.worker = worker

            with mock.patch.object(planner, "start_continuous", return_value="started") as starter:
                message = planner.start_request("/start-continuous 3")

            self.assertEqual(message, "started")
            starter.assert_called_once_with(max_cycles=3, prompt="")

    def test_continuous_mode_uses_intent_body_as_read_only_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("# Continuous Agent\n\nImplement the next safe improvement.\n", encoding="utf-8")
            worker = PlannerWorkerStub(ok=True)
            model = RecordingSequenceModelClient(
                [
                    json.dumps({
                        "thought": "",
                        "action": {
                            "type": "present_plan",
                            "summary": "Implement the next safe improvement.",
                            "not_in_scope": ["INTENT.md"],
                            "goals": [
                                {
                                    "goal_id": "goal-1",
                                    "title": "Implement safe improvement",
                                    "goal": "Apply the bounded project improvement.",
                                    "reason": "Project intent identifies this as the next direction.",
                                    "estimated_scope": "write",
                                    "success_signals": ["Worker reports the bounded improvement is complete."],
                                }
                            ],
                        },
                    }),
                    json.dumps({"summary": "Completed the safe improvement.", "next_steps": []}),
                ]
            )
            planner = PlannerAgent(
                model_client=model,
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: worker,
                json_loader=json.loads,
            )
            planner.worker = worker

            message = planner.start_continuous(max_cycles=1)

            self.assertIn("auto-approved", message)
            self.assertEqual(worker.created_issues[0]["request_summary"], "Implement the next safe improvement.")
            self.assertIn("Current candidate: Implement the next safe improvement.", model.prompts[0])
            self.assertIn("Use that source only as read-only guidance; do not edit it.", model.prompts[0])
            self.assertNotIn('"request": "Continuous Agent"', model.prompts[0])

    def test_continuous_mode_blocks_unsafe_auto_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INTENT.md").write_text("# Continuous Agent\n\nImplement the next safe improvement.\n", encoding="utf-8")
            worker = PlannerWorkerStub(ok=True)
            model = SequenceModelClient(
                [
                    json.dumps({
                        "thought": "",
                        "action": {
                            "type": "present_plan",
                            "summary": "Unsafe intent edit",
                            "not_in_scope": [],
                            "goals": [
                                {
                                    "goal_id": "goal-1",
                                    "title": "Edit intent",
                                    "goal": "Modify INTENT.md",
                                    "reason": "Unsafe.",
                                    "estimated_scope": "write",
                                }
                            ],
                        },
                    }),
                ]
            )
            planner = PlannerAgent(
                model_client=model,
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="medium",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: worker,
                json_loader=json.loads,
            )
            planner.worker = worker

            message = planner.start_continuous(max_cycles=1)

            self.assertIn("auto approval blocked", message)
            self.assertEqual(worker.close_calls, 0)
            self.assertIn("auto_approval_blocked", planner.continuous_state.stop_reason)

    def test_invalid_patch_request_surfaces_recovery_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            agent = WorkingFolderAgent(FakeModelClient(), config)

            result = agent._handle_patch_file_action({"type": "patch_file", "path": "src/main.py", "search": "", "replace": "new text"})

            self.assertFalse(result.ok)
            self.assertEqual(result.payload.get("code"), "BAD_REQUEST")
            self.assertEqual(result.payload.get("next_hint"), "read_file")
            suggestions = result.payload.get("suggested_next_actions", [])
            suggestion_types = [item.get("type") for item in suggestions if isinstance(item, dict)]
            self.assertIn("read_file", suggestion_types)
            self.assertIn("write_file", suggestion_types)

    def test_idempotent_patch_results_do_not_count_as_repeated_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AgentConfig(
                provider="local",
                model="test-model",
                root=root,
                tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                quiet=True,
            )
            agent = WorkingFolderAgent(FakeModelClient(), config)
            agent._active_run_id = 1
            path = "src/components/TodoItem.test.tsx"

            for step_num in (1, 2):
                agent.history.append(
                    AgentStep(
                        step=step_num,
                        thought="Patch appears already applied.",
                        action={
                            "type": "patch_file",
                            "path": path,
                            "search": "import React from 'react';",
                            "replace": "",
                        },
                        result=ActionResult(
                            ok=True,
                            name="patch_file",
                            payload={"status": "already_applied", "replacements": 0},
                        ),
                        elapsed_s=0.1,
                        run_id=1,
                    )
                )

            self.assertEqual(agent._recent_successful_mutations_for_path(path), [])
            self.assertIsNone(agent._repeated_mutation_guard_result("patch_file", path))

    def test_planner_export_state_survives_worker_export_failure(self) -> None:
        worker = BrokenExportWorkerStub(ok=True)
        planner = PlannerAgent(
            model_client=FakeModelClient(),
            config=SimpleNamespace(
                root=Path.cwd(),
                provider="local",
                model="test-model",
                thinking_mode="medium",
                verbosity="medium",
                max_parallel_workers=1,
            ),
            worker_factory=lambda: worker,
            json_loader=lambda text: {},
        )
        planner.worker = worker

        state = planner.export_state()

        worker_state = state.get("worker_state", {})
        self.assertIsInstance(worker_state, dict)
        self.assertIn("bridge_warning", worker_state)


if __name__ == "__main__":
    unittest.main()
