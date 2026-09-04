from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path
from diagnostics import BackendDiagnosticRun

from main import AgentConfig, WorkingFolderAgent, build_agent_config
from project_diagnostics import run_backend_diagnostics
from tree_loop import TreeLoop


class FakeModelClient:
    def complete(self, system: str, prompt: str) -> str:
        return '{"thought": "", "action": {"type": "finish", "message": "unused"}}'

    def get_last_metrics(self) -> dict:
        return {}

    def clone(self) -> "FakeModelClient":
        return FakeModelClient()


class ScriptedTreeModel:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def complete(self, system: str, prompt: str) -> str:
        if self._responses:
            return self._responses.pop(0)
        return "finish done"


def _write_fake_tsc(root: Path, expected_rel_path: str) -> None:
    bin_dir = root / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "tsc"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import pathlib
            import sys

            expected = {expected_rel_path!r}
            if "-p" not in sys.argv:
                sys.exit(0)
            config_path = pathlib.Path(sys.argv[sys.argv.index("-p") + 1])
            config = json.loads(config_path.read_text())
            files = [str(item).replace('\\\\', '/') for item in config.get("files", [])]
            if any(item.endswith(expected) for item in files):
                print(f"{{expected}}(4,1): error TS2582: Cannot find name 'describe'.", file=sys.stderr)
                sys.exit(2)
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


class BackendDiagnosticsTests(unittest.TestCase):
    def test_build_agent_config_disables_shell_when_env_flag_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = argparse.Namespace(
                provider="local",
                model="test-model",
                max_steps=5,
                shell_timeout=60,
                thinking_mode="medium",
                verbosity="medium",
                show_prompts=False,
                show_model_output=False,
                confirm_writes=False,
                confirm_shell=False,
                memory_limit=5000,
                memory_retrieval_limit=8,
                max_parallel_workers=4,
                extension_bridge=False,
            )

            with mock.patch.dict(os.environ, {"SHELL_ACCESS": "false"}, clear=False):
                config = build_agent_config(args, root, Path(__file__).resolve().parents[1] / "agent_tools.py")

            self.assertFalse(config.allow_shell)

    def test_working_folder_agent_rejects_run_shell_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            agent = WorkingFolderAgent(
                FakeModelClient(),
                AgentConfig(
                    provider="local",
                    model="test-model",
                    root=root,
                    tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                    quiet=True,
                    allow_shell=False,
                ),
            )

            result = agent._handle_run_shell_action({"type": "run_shell", "command": "pwd"})

            self.assertFalse(result.ok)
            self.assertEqual(result.payload.get("code"), "SHELL_DISABLED")
            self.assertIn("SHELL_ACCESS=false", str(result.payload.get("message", "")))

    def test_run_backend_diagnostics_targets_file_even_when_tsconfig_excludes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "src" / "useTodoOperations.test.ts"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("describe('todo', () => {})\n", encoding="utf-8")
            (root / "tsconfig.json").write_text(
                json.dumps({"compilerOptions": {"strict": True}, "exclude": ["**/*.test.ts"]}) + "\n",
                encoding="utf-8",
            )
            _write_fake_tsc(root, "src/useTodoOperations.test.ts")

            result = run_backend_diagnostics(root, path="src/useTodoOperations.test.ts")

            self.assertEqual(result.engine, "tsc")
            self.assertEqual(result.path, "src/useTodoOperations.test.ts")
            self.assertEqual(len(result.diagnostics), 1)
            self.assertEqual(result.diagnostics[0]["code"], "TS2582")
            self.assertIn("Cannot find name 'describe'", result.diagnostics[0]["message"])

    def test_working_folder_agent_diagnose_action_surfaces_backend_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "src" / "useTodoOperations.test.ts"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("describe('todo', () => {})\n", encoding="utf-8")
            (root / "tsconfig.json").write_text(
                json.dumps({"compilerOptions": {"strict": True}, "exclude": ["**/*.test.ts"]}) + "\n",
                encoding="utf-8",
            )
            _write_fake_tsc(root, "src/useTodoOperations.test.ts")

            agent = WorkingFolderAgent(
                FakeModelClient(),
                AgentConfig(
                    provider="local",
                    model="test-model",
                    root=root,
                    tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                    quiet=True,
                ),
            )

            result = agent._handle_diagnose_action({"type": "diagnose", "path": "src/useTodoOperations.test.ts"})

            self.assertFalse(result.ok)
            self.assertEqual(result.payload.get("code"), "DIAGNOSTICS_FOUND")
            diagnostics = result.payload.get("diagnostics", [])
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0].get("path"), "src/useTodoOperations.test.ts")

    def test_working_folder_agent_diagnose_action_treats_silent_nonzero_backend_as_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "src" / "pages" / "Dashboard.test.tsx"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("export {}\n", encoding="utf-8")

            agent = WorkingFolderAgent(
                FakeModelClient(),
                AgentConfig(
                    provider="local",
                    model="test-model",
                    root=root,
                    tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                    quiet=True,
                ),
            )

            with mock.patch(
                "main.run_backend_diagnostics",
                return_value=BackendDiagnosticRun(
                    engine="tsc",
                    path="src/pages/Dashboard.test.tsx",
                    scope="file",
                    command=["tsc", "--noEmit"],
                    returncode=2,
                    stdout="",
                    stderr="",
                    diagnostics=[],
                ),
            ):
                result = agent._handle_diagnose_action({"type": "diagnose", "path": "src/pages/Dashboard.test.tsx"})

            self.assertTrue(result.ok)
            self.assertEqual(result.payload.get("diagnostics"), [])
            self.assertEqual(
                result.payload.get("summary"),
                "Backend tsc diagnostics found no issues in src/pages/Dashboard.test.tsx.",
            )

    def test_automatic_backend_diagnostics_are_recorded_as_diagnose_not_run_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "src" / "components" / "TodoInput.test.tsx"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("export {}\n", encoding="utf-8")

            agent = WorkingFolderAgent(
                FakeModelClient(),
                AgentConfig(
                    provider="local",
                    model="test-model",
                    root=root,
                    tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                    quiet=True,
                ),
            )

            with mock.patch(
                "main.run_backend_diagnostics",
                return_value=BackendDiagnosticRun(
                    engine="tsc",
                    path="src/components/TodoInput.test.tsx",
                    scope="file",
                    command=["tsc", "--noEmit"],
                    returncode=2,
                    stdout="",
                    stderr="src/components/TodoInput.test.tsx(3,1): error TS2582: Cannot find name 'describe'.\n",
                    diagnostics=[
                        {
                            "path": "src/components/TodoInput.test.tsx",
                            "line": 3,
                            "column": 1,
                            "endLineNumber": 3,
                            "endColumn": 1,
                            "code": "TS2582",
                            "message": "Cannot find name 'describe'.",
                        }
                    ],
                ),
            ):
                step = agent._run_automatic_diagnostics_for_path(
                    path="src/components/TodoInput.test.tsx",
                    trigger_action_type="patch_file",
                )

            self.assertIsNotNone(step)
            assert step is not None
            self.assertEqual(step.action.get("type"), "diagnose")
            self.assertEqual(step.action.get("agent"), "host_diagnostics")
            self.assertEqual(step.result.name, "host_diagnostics")
            self.assertEqual(step.result.payload.get("diagnostic_engine"), "tsc")

    def test_tree_loop_write_ingests_backend_diagnostics_without_editor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "src" / "useTodoOperations.test.ts"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("export {}\n", encoding="utf-8")
            (root / "tsconfig.json").write_text(
                json.dumps({"compilerOptions": {"strict": True}, "exclude": ["**/*.test.ts"]}) + "\n",
                encoding="utf-8",
            )
            _write_fake_tsc(root, "src/useTodoOperations.test.ts")

            loop = TreeLoop(
                model=ScriptedTreeModel(
                    [
                        "write /repo/src/useTodoOperations.test.ts describe('todo')",
                        "list-issues",
                        "finish done",
                    ]
                ),
                workspace_root=root,
                max_turns=4,
                verbose=False,
            )

            result = loop.run("Fix the TypeScript test diagnostics")

            self.assertTrue(result.finished)
            first_turn_output = "\n".join(item.output for item in result.turns[0].results)
            self.assertIn("backend_diagnostics engine=tsc", first_turn_output)
            second_turn_output = "\n".join(item.output for item in result.turns[1].results)
            self.assertIn("run-ts2582-", second_turn_output)
            self.assertIn("Cannot find name 'describe'", second_turn_output)
            latest = loop._signal_state.get("latest_diagnostics")
            self.assertIsInstance(latest, dict)
            self.assertEqual(latest.get("path"), "src/useTodoOperations.test.ts")

    def test_tree_loop_diagnose_treats_silent_nonzero_backend_as_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "src" / "pages" / "Dashboard.test.tsx"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("export {}\n", encoding="utf-8")

            loop = TreeLoop(
                model=ScriptedTreeModel(["finish done"]),
                workspace_root=root,
                max_turns=1,
                verbose=False,
            )

            with mock.patch(
                "tree_loop.run_backend_diagnostics",
                return_value=BackendDiagnosticRun(
                    engine="tsc",
                    path="src/pages/Dashboard.test.tsx",
                    scope="file",
                    command=["tsc", "--noEmit"],
                    returncode=2,
                    stdout="",
                    stderr="",
                    diagnostics=[],
                ),
            ):
                output = loop._exec_diagnose({"path": "src/pages/Dashboard.test.tsx"})

            self.assertEqual(
                output,
                "engine=tsc\npath=src/pages/Dashboard.test.tsx\nissues=0\nexit_code=0",
            )
            self.assertIsNone(loop._signal_state.get("latest_diagnostics"))

    def test_working_folder_agent_changed_files_check_action_surfaces_suite_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            (root / "tracked.py").write_text("print('ok')\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
            (root / "bad.json").write_text('{"oops": }\n', encoding="utf-8")

            agent = WorkingFolderAgent(
                FakeModelClient(),
                AgentConfig(
                    provider="local",
                    model="test-model",
                    root=root,
                    tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                    quiet=True,
                ),
            )

            result = agent._handle_changed_files_check_action({"type": "changed_files_check"})

            self.assertFalse(result.ok)
            self.assertEqual(result.payload.get("code"), "DIAGNOSTICS_FOUND")
            self.assertGreaterEqual(int(result.payload.get("diagnostic_count", 0)), 1)

    def test_working_folder_agent_project_problems_action_surfaces_suite_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "bad.json").write_text('{"oops": }\n', encoding="utf-8")

            agent = WorkingFolderAgent(
                FakeModelClient(),
                AgentConfig(
                    provider="local",
                    model="test-model",
                    root=root,
                    tool_script=Path(__file__).resolve().parents[1] / "agent_tools.py",
                    quiet=True,
                ),
            )

            result = agent._handle_project_problems_action({"type": "project_problems", "mode": "standard"})

            self.assertFalse(result.ok)
            self.assertEqual(result.payload.get("code"), "DIAGNOSTICS_FOUND")
            self.assertEqual(result.payload.get("mode"), "standard")
