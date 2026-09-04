from __future__ import annotations

import io
import json
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path
import subprocess
from types import SimpleNamespace

from issue_facts import IssueFactLedger
from live_test_loop import TreeLoopPlannerWorker, _normalize_cli_argv, main, parse_args, print_worker_status
from planner import PlannerAgent, PlannerGoal, PlannerPlan
from tree_commands import CommandResult


class FakeModelClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self._index = 0

    def complete(self, system: str, prompt: str) -> str:
        self.calls.append({"system": system, "prompt": prompt})
        if self._index >= len(self.responses):
            return "finish done"
        response = self.responses[self._index]
        self._index += 1
        return response

    def get_last_metrics(self) -> dict:
        return {"usage": {"input_tokens": 10, "output_tokens": 5}}

    def clone(self) -> "FakeModelClient":
        return FakeModelClient(self.responses[self._index:])


class SharedCursorFakeModelClient:
    def __init__(self, responses: list[str], shared: dict | None = None) -> None:
        self._shared = shared or {"responses": list(responses), "index": 0, "calls": []}

    def complete(self, system: str, prompt: str) -> str:
        self._shared["calls"].append({"system": system, "prompt": prompt})
        index = int(self._shared["index"])
        responses = self._shared["responses"]
        if index >= len(responses):
            return "finish done"
        response = responses[index]
        self._shared["index"] = index + 1
        return response

    def get_last_metrics(self) -> dict:
        return {"usage": {"input_tokens": 10, "output_tokens": 5}}

    def clone(self) -> "SharedCursorFakeModelClient":
        return SharedCursorFakeModelClient([], shared=self._shared)


class LiveTestLoopCliTests(unittest.TestCase):
    def test_normalize_cli_argv_rewrites_unicode_dash_prefixes(self) -> None:
        argv = ["--provider", "anthropic", "—model", "Claude-opus-4-6", "–thinking-mode", "medium"]

        normalized = _normalize_cli_argv(argv)

        self.assertEqual(
            normalized,
            ["--provider", "anthropic", "--model", "Claude-opus-4-6", "--thinking-mode", "medium"],
        )

    def test_parse_args_accepts_unicode_dash_options(self) -> None:
        args = parse_args(["--provider", "anthropic", "—model", "Claude-opus-4-6"])

        self.assertEqual(args.provider, "anthropic")
        self.assertEqual(args.model, "Claude-opus-4-6")

    def test_parse_args_defaults_to_planner_mode(self) -> None:
        args = parse_args([])

        self.assertFalse(args.worker_mode)

    def test_parse_args_accepts_worker_mode(self) -> None:
        args = parse_args(["--worker-mode"])

        self.assertTrue(args.worker_mode)

    def test_parse_args_accepts_tools_flag_for_extension_compatibility(self) -> None:
        args = parse_args(["--tools", "/tmp/agent_tools.py"])

        self.assertEqual(args.tools, "/tmp/agent_tools.py")

    def test_main_planner_mode_passes_tool_script_and_max_steps_to_banner_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            with mock.patch("live_test_loop.refresh_runtime_provider_catalog_once"):
                with mock.patch("live_test_loop.create_model_client", return_value=FakeModelClient(["finish done"])):
                    with mock.patch("live_test_loop.interactive_planner_loop") as mock_loop:
                        exit_code = main(["--root", str(root), "--turns", "17"])

            self.assertEqual(exit_code, 0)
            planner = mock_loop.call_args.args[0]
            self.assertEqual(planner.config.max_steps, 17)
            self.assertTrue(str(planner.config.tool_script).endswith("agent_tools.py"))

    def test_main_extension_bridge_mode_initializes_without_missing_config_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stdin_payload = json.dumps({"id": "1", "type": "initialize"}) + "\n"

            with mock.patch("live_test_loop.refresh_runtime_provider_catalog_once"):
                with mock.patch("live_test_loop.create_model_client", return_value=FakeModelClient(["finish done"])):
                    with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
                        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                            exit_code = main(["--root", str(root), "--turns", "19", "--extension-bridge"])

            self.assertEqual(exit_code, 0)
            lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
            self.assertEqual(lines[0]["message"], "initialized")
            planner_state = lines[0]["state"]["planner"]
            runtime_config = planner_state.get("runtime_config", {})
            self.assertEqual(runtime_config.get("model"), "gemini-2.5-flash")
            worker_state = planner_state.get("worker_state", {})
            self.assertIsInstance(worker_state, dict)

    def test_main_extension_bridge_mode_accepts_tools_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stdin_payload = json.dumps({"id": "1", "type": "initialize"}) + "\n"

            with mock.patch("live_test_loop.refresh_runtime_provider_catalog_once"):
                with mock.patch("live_test_loop.create_model_client", return_value=FakeModelClient(["finish done"])):
                    with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
                        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                            exit_code = main([
                                "--root", str(root),
                                "--turns", "19",
                                "--tools", str(root / "agent_tools.py"),
                                "--extension-bridge",
                            ])

            self.assertEqual(exit_code, 0)
            lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
            self.assertEqual(lines[0]["message"], "initialized")


class TreeLoopPlannerWorkerTests(unittest.TestCase):
    def test_run_task_returns_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["cat /repo/main.py\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.run_task("Inspect the project")

            self.assertTrue(result.ok)
            self.assertTrue(result.task_satisfied)
            self.assertEqual(result.final_message, "done")
            self.assertIn("main.py", result.touched_paths)
            self.assertTrue(worker.history)

    def test_beta_observability_streams_partial_snapshots_and_compacts_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            observability_path = root / "memory_observability.test.md"

            with mock.patch.object(worker, "_observability_path", return_value=observability_path):
                worker._reset_run_observability("Inspect repo")
                for index in range(30):
                    worker._append_observability_command(
                        f"cat /repo/file_{index}.ts",
                        CommandResult(ok=True, output=f"output-{index}", command_type="read"),
                    )

            content = observability_path.read_text(encoding="utf-8")
            self.assertIn("Run in progress.", content)
            self.assertIn("Auto-compacted observability trace", content)
            self.assertIn("output-29", content)
            self.assertNotIn("output-0", content)

    def test_run_task_supports_diagnose_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = Path(root, "TodoContext.tsx")
            target.write_text("export const value = 1;\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["diagnose /repo/TodoContext.tsx\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            with mock.patch.object(worker.loop, "_exec_diagnose", return_value="engine=tsc\npath=TodoContext.tsx\nissues=0\nexit_code=0") as mock_diagnose:
                result = worker.run_task("Diagnose TodoContext.tsx")

            self.assertTrue(result.ok)
            mock_diagnose.assert_called_once()
            outputs = [item.output for item in worker.history[0].results]
            self.assertTrue(any("issues=0" in output for output in outputs))

    def test_playground_os_can_discover_and_invoke_testing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["skill\nskill codebase-diagnostics mode=aggregate\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.run_task("Inspect testing guidance skills")

            self.assertTrue(result.ok)
            self.assertTrue(worker.history)
            first_turn_outputs = [item.output for item in worker.history[0].results]
            self.assertTrue(any("codebase-diagnostics" in output for output in first_turn_outputs))
            self.assertTrue(any("mode: aggregate" in output for output in first_turn_outputs))
            self.assertTrue(any("project_problems" in output for output in first_turn_outputs))

    def test_playground_os_can_discover_and_invoke_bibbity_boop_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["skill\nskill codebase-mutation mode=surgical_text\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.run_task("Inspect robot voice testing skills")

            self.assertTrue(result.ok)
            self.assertTrue(worker.history)
            first_turn_outputs = [item.output for item in worker.history[0].results]
            self.assertTrue(any("codebase-mutation" in output for output in first_turn_outputs))
            self.assertTrue(any("mode: surgical_text" in output for output in first_turn_outputs))
            self.assertTrue(any("replace_range" in output for output in first_turn_outputs))

    def test_bibbity_boop_skill_can_carry_style_into_later_turn_thoughts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            model = FakeModelClient(
                [
                    "skill codebase-mutation mode=surgical_text",
                    """ >>th: Use the surgical_text mutation contract and inspect file state before any edit.
cat /repo/main.py
finish done""",
                ]
            )
            worker = TreeLoopPlannerWorker(
                model=model,
                root=root,
                max_turns=4,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.run_task("Load the robot voice skill and continue the run")

            self.assertTrue(result.ok)
            self.assertGreaterEqual(len(worker.history), 2)
            self.assertIn("surgical_text mutation contract", worker.history[1].thought)
            self.assertGreaterEqual(len(model.calls), 2)
            self.assertIn("mode: surgical_text", model.calls[1]["prompt"])
            self.assertIn("replace_range", model.calls[1]["prompt"])

    def test_goal_start_skill_mode_is_prompted_once_per_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            model = FakeModelClient([
                "skill\nskill codebase-discovery mode=standard",
                "skill codebase-discovery mode=standard",
                "cat /repo/main.py\nfinish done",
            ])
            worker = TreeLoopPlannerWorker(
                model=model,
                root=root,
                max_turns=4,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            worker.prepare_for_goal(preserve_context=False)
            result = worker.run_task("Use skills if they help, then inspect the file")

            self.assertTrue(result.ok)
            self.assertGreaterEqual(len(model.calls), 2)
            self.assertIn("GOAL-START SKILL MODE", model.calls[0]["prompt"])
            self.assertIn("discover and load relevant Playground OS skills once", model.calls[0]["prompt"])
            self.assertIn("Prefer starting with `skill` to inspect the catalog before any direct `skill <name>` load", model.calls[0]["prompt"])
            first_turn_outputs = [item.output for item in worker.history[0].results]
            self.assertTrue(any("Available skills:" in output for output in first_turn_outputs))
            self.assertTrue(any("codebase-discovery" in output for output in first_turn_outputs))
            self.assertTrue(any("mode: standard" in output for output in first_turn_outputs))
            self.assertNotIn("GOAL-START SKILL MODE", model.calls[1]["prompt"])
            state = worker.export_runtime_state()
            self.assertEqual(state["goal_start_skill_mode"]["pending"], False)
            self.assertEqual(state["goal_start_skill_mode"]["used"], True)

    def test_goal_start_skill_mode_resets_for_next_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            model = FakeModelClient([
                "finish done",
                "finish done",
            ])
            worker = TreeLoopPlannerWorker(
                model=model,
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            worker.prepare_for_goal(preserve_context=False)
            first_result = worker.run_task("First planner goal")
            self.assertTrue(first_result.ok)
            self.assertIn("GOAL-START SKILL MODE", model.calls[0]["prompt"])

            worker.prepare_for_goal(preserve_context=False)
            second_result = worker.run_task("Second planner goal")

            self.assertTrue(second_result.ok)
            self.assertGreaterEqual(len(model.calls), 2)
            self.assertIn("GOAL-START SKILL MODE", model.calls[1]["prompt"])

    def test_prepare_for_goal_clears_history_without_context_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["cat /repo/main.py\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            worker.run_task("Inspect the project")

            worker.prepare_for_goal(preserve_context=False)

            self.assertEqual(worker.history, [])
            self.assertEqual(worker.loop.history, [])

    def test_set_fact_persists_to_repo_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["fact demo/architecture/entrypoint beta-worker\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.run_task("Record an architecture fact")

            self.assertTrue(result.ok)
            repo_facts_path = root / "repo_facts.md"
            self.assertTrue(repo_facts_path.exists())
            ledger = IssueFactLedger.load(repo_facts_path)
            record = ledger.find_fact("entrypoint")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.value, "beta-worker")

    def test_finish_uses_host_auto_validation_after_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            subprocess.run(["git", "add", "main.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["write /repo/main.py print('bye')\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.run_task("Update main.py")

            self.assertTrue(result.ok)
            self.assertTrue(result.validation_passed)
            self.assertEqual(result.validation.summary, "A concrete validation step succeeded after mutation.")
            self.assertIsNotNone(worker._latest_review)

    def test_explicit_review_changes_command_updates_latest_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            subprocess.run(["git", "add", "main.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
            Path(root, "main.py").write_text("print('bye')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["review-changes main.py limit=10\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.run_task("Review current changes")

            self.assertTrue(result.ok)
            latest_review = worker.latest_review_state()
            self.assertIsNotNone(latest_review)
            assert latest_review is not None
            self.assertEqual(latest_review["action_type"], "review_changes")

    def test_finish_block_exposes_validation_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["write /repo/main.py print('bye')\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.run_task("Update main.py without validation")

            self.assertFalse(result.ok)
            suggestions = worker.runtime_suggested_next_actions()
            suggestion_types = {str(item.get("type", "")) for item in suggestions}
            self.assertIn("read_file", suggestion_types)
            self.assertIn("show_diff", suggestion_types)
            self.assertIn("review_changes", suggestion_types)

    def test_export_runtime_state_includes_worker_status_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            worker._latest_review = {"action_type": "review_changes", "summary": "Reviewed 1 changed file.", "path": "main.py"}

            state = worker.export_runtime_state()

            self.assertIn("runtime_config", state)
            self.assertIn("runtime_capabilities", state)
            self.assertIn("repo_facts_status_lines", state)
            self.assertIn("latest_review", state)
            self.assertIn("patch_resolution", state)
            self.assertIn("edit_batch", state)
            self.assertIn("active_mode_strategy", state)
            self.assertIn("available_skills", state)
            self.assertIn("suggested_next_actions", state)
            self.assertEqual(state["latest_review"]["action_type"], "review_changes")
            self.assertTrue(state["runtime_capabilities"]["host_enforced_edit_batch"])
            skill_names = {str(item.get("name", "")) for item in state["available_skills"] if isinstance(item, dict)}
            self.assertIn("codebase-discovery", skill_names)

    def test_patch_resolution_steering_includes_inner_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            worker._enter_patch_resolution(
                action_type="replace_lines",
                path="main.py",
                reason="replace_lines: expected target lines missing",
            )

            steering = worker._compose_steering()
            strategy = worker.export_runtime_state()["active_mode_strategy"]

            self.assertIn("Choose the recovery strategy path", steering)
            self.assertIn("Do not return numbered prose steps", steering)
            self.assertEqual(strategy["mode"], "patch_resolution")
            self.assertIn(["s1: cat /repo/main.py", "s2: show-diff main.py", "s3: review-changes main.py limit=20"], strategy["strategy_blocks"])
            self.assertIn(["s1: drop"], strategy["strategy_blocks"])

    def test_goal_execution_steering_does_not_force_repo_map_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            steering = worker._compose_steering()

            self.assertNotIn("Goal execution structural retrieval", steering)
            self.assertNotIn('repo-map /repo topic="<failing feature or symbol>" limit=20', steering)

    def test_discovery_remediation_steering_uses_strategy_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            worker.loop.bridge.tree.ingest_diagnostic_content(
                "main.py(1,1): error TS2305: Module './missing' has no exported member 'Thing'.",
                source_path="[run-check:typecheck]",
            )

            run_check = CommandResult(
                ok=True,
                output="command=npm run typecheck\nexit_code=1\nissues=1",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "run_check", "kind": "typecheck"},
            )
            worker._observe_command_result("run-check typecheck", run_check)

            steering = worker._compose_steering()
            strategy = worker.export_runtime_state()["active_mode_strategy"]

            self.assertEqual(strategy["mode"], "discovery_remediation")
            self.assertIn("Choose the remediation strategy path", steering)
            self.assertIn("s1: cat /repo/main.py", steering)
            self.assertIn("s1: list-issues", steering)
            self.assertIn("Do not return numbered prose steps", steering)
            self.assertIn(["s1: cat /repo/main.py"], strategy["strategy_blocks"])

    def test_failed_patch_enters_patch_resolution_and_blocks_follow_up_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            failed_patch = CommandResult(
                ok=False,
                output="patch_file: search text not found",
                command_type="write",
                needs_tool=True,
                tool_action={"type": "patch_file", "path": "main.py", "search": "missing", "replace": "bye"},
            )
            worker._observe_command_result("[dispatch: patch_file main.py]", failed_patch)

            patch_resolution = worker.patch_resolution_state()
            self.assertIsNotNone(patch_resolution)
            assert patch_resolution is not None
            self.assertEqual(patch_resolution["path"], "main.py")

            blocked_edit = CommandResult(
                ok=True,
                output="",
                command_type="write",
                needs_tool=True,
                tool_action={
                    "type": "replace_lines",
                    "path": "main.py",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "print('bye')",
                },
            )
            message = worker._dispatch_tool_action(blocked_edit)

            self.assertFalse(blocked_edit.ok)
            self.assertIn("patch resolution active", message)
            suggestion_types = {str(item.get("type", "")) for item in worker.runtime_suggested_next_actions()}
            self.assertIn("read_file", suggestion_types)
            self.assertIn("drop_context", suggestion_types)

    def test_read_clears_patch_resolution_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            worker._enter_patch_resolution(
                action_type="replace_lines",
                path="main.py",
                reason="replace_lines: expected target lines missing",
            )

            result = worker.execute_operator_action({"type": "read_file", "path": "main.py"})

            self.assertTrue(result.ok)
            self.assertIsNone(worker.patch_resolution_state())

    def test_discovery_remediation_blocks_write_until_target_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            worker.loop.bridge.tree.ingest_diagnostic_content(
                "main.py(1,1): error TS2305: Module './missing' has no exported member 'Thing'.",
                source_path="[run-check:typecheck]",
            )

            run_check = CommandResult(
                ok=True,
                output="command=npm run typecheck\nexit_code=1\nissues=1",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "run_check", "kind": "typecheck"},
            )
            worker._observe_command_result("run-check typecheck", run_check)

            self.assertIsNotNone(worker.discovery_remediation_state())
            blocked_write = CommandResult(
                ok=True,
                output="",
                command_type="write",
                needs_tool=True,
                tool_action={"type": "write_file", "path": "main.py", "content": "print('bye')\n"},
            )
            message = worker._dispatch_tool_action(blocked_write)

            self.assertFalse(blocked_write.ok)
            self.assertIn("discovery remediation active", message)
            self.assertIn("repo_map", message)
            self.assertIn("read_line_range", message)
            suggestions = worker.runtime_suggested_next_actions()
            suggestion_types = {str(item.get("type", "")) for item in suggestions}
            self.assertIn("read_file", suggestion_types)

            read_result = worker.execute_operator_action({"type": "read_file", "path": "main.py"})
            self.assertTrue(read_result.ok)
            self.assertIsNone(worker.discovery_remediation_state())

    def test_execute_operator_action_can_close_active_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            opened = worker.ensure_issue_for_plan(
                original_request="Repair active issue recovery",
                plan_summary="Add manual close action",
            )

            result = worker.execute_operator_action({"type": "close_active_issue"})

            self.assertTrue(result.ok)
            self.assertEqual(result.payload.get("issue_id"), opened.get("issue_id"))
            self.assertEqual(result.payload.get("issue", {}).get("status"), "closed")
            self.assertEqual(
                result.payload.get("issue", {}).get("lifecycle_notes"),
                ["Closed manually from the VS Code Issues panel."],
            )
            self.assertEqual(worker.issue_ledger.active_issue_id, "")
            self.assertIsNone(worker.issue_ledger.active_issue())

    def test_execute_operator_action_rejects_close_when_no_active_issue_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.execute_operator_action({"type": "close_active_issue"})

            self.assertFalse(result.ok)
            self.assertEqual(result.payload.get("error"), "No active issue to close.")

    def test_npm_command_requires_approval_and_surfaces_worker_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            result = CommandResult(
                ok=True,
                output="",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "npm_command", "command": "install react", "path": "package.json"},
            )

            message = worker._dispatch_tool_action(result)

            self.assertFalse(result.ok)
            self.assertIn("approval required", message)
            suggestion_types = {str(item.get("type", "")) for item in worker.runtime_suggested_next_actions()}
            self.assertIn("approve_npm_command", suggestion_types)
            self.assertIn("reject_npm_command", suggestion_types)

    def test_execute_operator_action_can_approve_pending_npm_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            result = CommandResult(
                ok=True,
                output="",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "npm_command", "command": "install react", "path": "package.json"},
            )
            worker._dispatch_tool_action(result)
            completed = subprocess.CompletedProcess(
                args=["npm", "install", "react"],
                returncode=0,
                stdout="added 1 package\n",
                stderr="",
            )

            with mock.patch("tree_loop.subprocess.run", return_value=completed):
                approval = worker.execute_operator_action({"type": "approve_npm_command"})

            self.assertTrue(approval.ok)
            self.assertIn("manager=npm", str(approval.payload.get("message", "")))
            self.assertEqual(worker.runtime_suggested_next_actions(), [])

    def test_execute_operator_action_can_reject_pending_npm_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            result = CommandResult(
                ok=True,
                output="",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "npm_command", "command": "install react", "path": "package.json"},
            )
            worker._dispatch_tool_action(result)

            rejection = worker.execute_operator_action({"type": "reject_npm_command"})

            self.assertTrue(rejection.ok)
            self.assertIn("Rejected pending npm command", str(rejection.payload.get("message", "")))
            self.assertEqual(worker.runtime_suggested_next_actions(), [])

    def test_discovery_budget_allows_set_fact_after_budget_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            worker.configure_discovery_budget("quick", "Quick", 1)
            assert worker.discovery_budget is not None
            worker.discovery_budget.tool_calls_used = 1

            set_fact = CommandResult(
                ok=True,
                output="",
                command_type="tool",
                needs_tool=True,
                tool_action={
                    "type": "set_fact",
                    "issue_id": "demo",
                    "fact_type": "architecture",
                    "key": "entrypoint",
                    "value": "planner.py owns discovery",
                },
            )

            message = worker._dispatch_tool_action(set_fact)

            self.assertTrue(set_fact.ok)
            self.assertEqual(message, "fact recorded: entrypoint")
            assert worker.discovery_budget is not None
            self.assertEqual(worker.discovery_budget.tool_calls_used, 1)

    def test_discovery_budget_skips_malformed_set_fact_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            worker.configure_discovery_budget("quick", "Quick", 1)
            assert worker.discovery_budget is not None
            worker.discovery_budget.tool_calls_used = 1

            set_fact = CommandResult(
                ok=True,
                output="",
                command_type="tool",
                needs_tool=True,
                tool_action={
                    "type": "set_fact",
                    "issue_id": "demo",
                    "fact_type": "goal",
                    "key": "entrypoint",
                    "value": "",
                },
            )

            message = worker._dispatch_tool_action(set_fact)

            self.assertFalse(set_fact.ok)
            self.assertIn("set_fact: missing key or value", message)
            self.assertIn("fact demo/goal/<key> <value>", message)
            self.assertIsNone(worker.issue_ledger.find_fact("entrypoint"))
            assert worker.discovery_budget is not None
            self.assertEqual(worker.discovery_budget.tool_calls_used, 1)

    def test_finish_blocked_while_edit_batch_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient([
                    "batch start\nwrite /repo/main.py print('bye')\nfinish done",
                    "batch end\nfinish done",
                ]),
                root=root,
                max_turns=4,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.run_task("Update main.py in a batch")

            self.assertTrue(result.ok)
            self.assertFalse(worker.edit_batch_state()["active"])
            self.assertIn("finish blocked: end the active edit batch before finish", worker.history[0].results[2].output)
            self.assertIn("host verified 1 file", worker.history[1].results[0].output)

    def test_same_turn_edit_batch_verifies_before_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["batch start\nwrite /repo/main.py print('bye')\nbatch end\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )

            result = worker.run_task("Update main.py in one bounded batch")

            self.assertTrue(result.ok)
            self.assertTrue(result.validation_passed)
            self.assertFalse(worker.edit_batch_state()["active"])
            self.assertEqual(worker.edit_batch_state()["queued_count"], 0)
            self.assertIn("host verified 1 file", worker.history[0].results[2].output)


class WorkerStatusRenderingTests(unittest.TestCase):
    def test_print_worker_status_includes_repo_facts_and_latest_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            worker._latest_review = {"action_type": "review_changes", "summary": "Reviewed 1 changed file.", "path": "main.py"}

            with mock.patch("builtins.print") as mock_print:
                print_worker_status(worker)

            rendered = "\n".join(" ".join(str(arg) for arg in call.args) for call in mock_print.call_args_list)
            self.assertIn("Repo Facts:", rendered)
            self.assertIn("Latest Review:", rendered)
            self.assertIn("Reviewed 1 changed file.", rendered)

    def test_print_worker_status_includes_suggested_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["write /repo/main.py print('bye')\nfinish done"]),
                root=root,
                max_turns=3,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            worker.run_task("Update main.py without validation")

            with mock.patch("builtins.print") as mock_print:
                print_worker_status(worker)

            rendered = "\n".join(" ".join(str(arg) for arg in call.args) for call in mock_print.call_args_list)
            self.assertIn("Suggested Next Actions:", rendered)
            self.assertIn("Show Diff", rendered)

    def test_delete_session_clears_runtime_and_deletes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = TreeLoopPlannerWorker(
                model=FakeModelClient(["finish done"]),
                root=root,
                max_turns=2,
                checkpoint_interval=0,
                thinking_mode="low",
                verbose=False,
            )
            observability_path = root / "memory_observability.test.md"
            repo_facts_path = root / "repo_facts.md"
            repo_facts_path.write_text("placeholder", encoding="utf-8")
            observability_path.write_text("trace", encoding="utf-8")
            worker.ensure_issue_for_plan(original_request="Repair planner", plan_summary="Initial repair")

            with mock.patch.object(worker, "_observability_path", return_value=observability_path):
                message = worker.delete_session()

            self.assertEqual(message, "Session deleted. Repo facts and observability were cleared.")
            self.assertFalse(repo_facts_path.exists())
            self.assertFalse(observability_path.exists())
            self.assertEqual(worker.issue_ledger.total_fact_count(), 0)
            self.assertEqual(worker.fact_map, {})
            self.assertEqual(worker.history, [])


class PlannerBridgeParityTests(unittest.TestCase):
    def test_planner_export_state_surfaces_worker_validation_suggestions_after_failed_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")

            planner = PlannerAgent(
                model_client=FakeModelClient(["finish done"]),
                config=SimpleNamespace(
                    root=root,
                    provider="local",
                    model="test-model",
                    thinking_mode="low",
                    verbosity="medium",
                    max_parallel_workers=1,
                ),
                worker_factory=lambda: TreeLoopPlannerWorker(
                    model=FakeModelClient(["write /repo/main.py print('bye')\nfinish done"]),
                    root=root,
                    max_turns=3,
                    checkpoint_interval=0,
                    thinking_mode="low",
                    verbose=False,
                ),
                json_loader=lambda text: {},
            )
            planner.session.pending_plan = PlannerPlan(
                original_request="Update main.py",
                summary="Try a write without validation",
                goals=[
                    PlannerGoal(
                        goal_id="goal-1",
                        title="Update main",
                        goal="Edit main.py and finish.",
                        reason="Exercise finish blocking.",
                    )
                ],
            )

            message = planner.execute_pending_plan()
            state = planner.export_state()

            self.assertIn("failed", message.lower())
            worker_state = state.get("worker_state", {})
            actions = worker_state.get("suggested_next_actions", [])
            action_types = {str(item.get("type", "")) for item in actions if isinstance(item, dict)}
            self.assertIn("show_diff", action_types)
            self.assertIn("review_changes", action_types)
            self.assertTrue(state.get("execution_paused"))
            self.assertIsNotNone(state.get("paused_plan"))

    def test_extension_bridge_initialize_and_worker_action_return_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            stdin_payload = "\n".join(
                [
                    json.dumps({"id": "1", "type": "initialize"}),
                    json.dumps({"id": "2", "type": "worker_action", "action": {"type": "read_file", "path": "main.py"}}),
                ]
            ) + "\n"

            with mock.patch("live_test_loop.create_model_client", return_value=FakeModelClient(["finish done"])):
                with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = main(["--root", str(root), "--extension-bridge"])

            self.assertEqual(exit_code, 0)
            lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
            self.assertEqual(lines[0]["message"], "initialized")
            self.assertTrue(lines[0]["state"]["planner"].get("worker_state"))
            self.assertTrue(lines[1]["ok"])
            self.assertIn("Read main.py.", lines[1]["message"])

    def test_extension_bridge_submit_and_planner_action_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            planner_plan = json.dumps(
                {
                    "action": {
                        "type": "present_plan",
                        "summary": "Inspect the repo before editing.",
                        "goals": [
                            {
                                "goal_id": "goal-1",
                                "title": "Inspect main",
                                "goal": "Read main.py and report findings.",
                                "reason": "Need a first-pass understanding of the entrypoint.",
                            }
                        ],
                    }
                }
            )
            stdin_payload = "\n".join(
                [
                    json.dumps({"id": "1", "type": "initialize"}),
                    json.dumps({"id": "2", "type": "submit", "text": "inspect the project"}),
                    json.dumps({"id": "3", "type": "planner_action", "action": "reject_plan"}),
                ]
            ) + "\n"

            with mock.patch("live_test_loop.create_model_client", return_value=FakeModelClient([planner_plan])):
                with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = main(["--root", str(root), "--extension-bridge"])

            self.assertEqual(exit_code, 0)
            lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
            self.assertEqual(lines[1]["id"], "2")
            self.assertIn("Plan Summary", lines[1]["message"])
            self.assertEqual(lines[2]["id"], "3")
            self.assertEqual(lines[2]["message"], "Plan rejected. Describe what should change and I will revise it.")
            planner_state = lines[2]["state"]["planner"]
            self.assertTrue(planner_state["awaiting_plan_revision"])
            self.assertIsNone(planner_state["pending_plan"])

    def test_extension_bridge_close_issue_planner_action_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = IssueFactLedger.empty()
            issue = ledger.ensure_issue_open(
                request_summary="Close me from the extension",
                plan_summary="Active extension close target",
            )
            (root / "repo_facts.md").write_text(ledger.to_markdown(), encoding="utf-8")

            stdin_payload = "\n".join(
                [
                    json.dumps({"id": "1", "type": "initialize"}),
                    json.dumps({"id": "2", "type": "planner_action", "action": "close_issue", "issue_id": issue.issue_id}),
                ]
            ) + "\n"

            with mock.patch("live_test_loop.create_model_client", return_value=FakeModelClient(["finish done"])):
                with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = main(["--root", str(root), "--extension-bridge"])

            self.assertEqual(exit_code, 0)
            lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
            self.assertEqual(lines[1]["id"], "2")
            self.assertTrue(lines[1]["ok"])
            self.assertIn(f"Closed issue {issue.issue_id}", lines[1]["message"])
            planner_state = lines[1]["state"]["planner"]
            issue_state = planner_state["issue_state"]
            self.assertIsNone(issue_state.get("active_issue"))
            reopenable_ids = {str(item.get("issue_id", "")) for item in issue_state.get("reopenable_issues", [])}
            self.assertIn(issue.issue_id, reopenable_ids)

    def test_extension_bridge_discovery_streams_progress_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            offer_discovery = json.dumps(
                {
                    "action": {
                        "type": "offer_discovery",
                        "reason": "Need repo discovery before planning.",
                        "prompt": "Choose discovery depth.",
                        "recommended_mode": "quick",
                    }
                }
            )
            present_plan = json.dumps(
                {
                    "action": {
                        "type": "present_plan",
                        "summary": "Use discovered context.",
                        "goals": [
                            {
                                "goal_id": "goal-1",
                                "title": "Inspect main",
                                "goal": "Read main.py and summarize it.",
                                "reason": "Need discovered entrypoint context.",
                            }
                        ],
                    }
                }
            )
            stdin_payload = "\n".join(
                [
                    json.dumps({"id": "1", "type": "initialize"}),
                    json.dumps({"id": "2", "type": "submit", "text": "inspect the repo"}),
                    json.dumps({"id": "3", "type": "planner_action", "action": "select_discovery_mode", "mode": "quick"}),
                ]
            ) + "\n"

            model = SharedCursorFakeModelClient([
                offer_discovery,
                "cat /repo/main.py\nfinish discovery complete",
                present_plan,
            ])
            with mock.patch("live_test_loop.create_model_client", return_value=model):
                with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = main(["--root", str(root), "--extension-bridge"])

            self.assertEqual(exit_code, 0)
            lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
            discovery_progress = [line for line in lines if line.get("type") == "progress" and line.get("domain") == "discovery"]
            self.assertTrue(discovery_progress)
            self.assertTrue(any(line.get("action_type") == "model_call_start" for line in discovery_progress))
            self.assertTrue(any(line.get("action_type") == "model_call_finish" for line in discovery_progress))
            self.assertTrue(any(line.get("action_type") == "cat" for line in discovery_progress))
            self.assertEqual(lines[-1]["id"], "3")

    def test_extension_bridge_discovery_surfaces_commandless_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            offer_discovery = json.dumps(
                {
                    "action": {
                        "type": "offer_discovery",
                        "reason": "Need repo discovery before planning.",
                        "prompt": "Choose discovery depth.",
                        "recommended_mode": "deep",
                    }
                }
            )
            present_plan = json.dumps(
                {
                    "action": {
                        "type": "present_plan",
                        "summary": "Use discovered context.",
                        "goals": [
                            {
                                "goal_id": "goal-1",
                                "title": "Inspect main",
                                "goal": "Read main.py and summarize it.",
                                "reason": "Need discovered entrypoint context.",
                            }
                        ],
                    }
                }
            )
            stdin_payload = "\n".join(
                [
                    json.dumps({"id": "1", "type": "initialize"}),
                    json.dumps({"id": "2", "type": "submit", "text": "inspect the repo"}),
                    json.dumps({"id": "3", "type": "planner_action", "action": "select_discovery_mode", "mode": "deep"}),
                ]
            ) + "\n"

            model = SharedCursorFakeModelClient([
                offer_discovery,
                "I'll check the Mechanics.md file and the grid implementation to provide accurate instructions.",
                "cat /repo/main.py\nfinish discovery complete",
                present_plan,
            ])
            with mock.patch("live_test_loop.create_model_client", return_value=model):
                with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = main(["--root", str(root), "--extension-bridge"])

            self.assertEqual(exit_code, 0)
            lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
            discovery_progress = [line for line in lines if line.get("type") == "progress" and line.get("domain") == "discovery"]
            self.assertTrue(any(line.get("action_type") == "model_call_start" for line in discovery_progress))
            self.assertTrue(any(line.get("action_type") == "model_call_finish" for line in discovery_progress))
            self.assertTrue(
                any("failed command preflight" in str(line.get("summary", "")) for line in discovery_progress)
            )
            self.assertTrue(any(line.get("action_type") == "cat" for line in discovery_progress))
            self.assertEqual(lines[-1]["id"], "3")

    def test_extension_bridge_goal_execution_streams_progress_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            Path(root, "main.py").write_text("print('hello')\n")
            present_plan = json.dumps(
                {
                    "action": {
                        "type": "present_plan",
                        "summary": "Inspect the repo before editing.",
                        "goals": [
                            {
                                "goal_id": "goal-1",
                                "title": "Inspect main",
                                "goal": "Read main.py and report findings.",
                                "reason": "Need a first-pass understanding of the entrypoint.",
                            }
                        ],
                    }
                }
            )
            stdin_payload = "\n".join(
                [
                    json.dumps({"id": "1", "type": "initialize"}),
                    json.dumps({"id": "2", "type": "submit", "text": "inspect the project"}),
                    json.dumps({"id": "3", "type": "planner_action", "action": "approve_plan"}),
                ]
            ) + "\n"

            model = SharedCursorFakeModelClient([
                present_plan,
                "cat /repo/main.py\nfinish done",
            ])
            with mock.patch("live_test_loop.create_model_client", return_value=model):
                with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = main(["--root", str(root), "--extension-bridge"])

            self.assertEqual(exit_code, 0)
            lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
            plan_progress = [line for line in lines if line.get("type") == "progress" and line.get("domain") == "plan"]
            self.assertTrue(plan_progress)
            self.assertTrue(any(line.get("action_type") == "cat" for line in plan_progress))
            self.assertTrue(any(line.get("type") == "goal_start" for line in lines))
            self.assertTrue(any(line.get("type") == "goal_finish" for line in lines))
