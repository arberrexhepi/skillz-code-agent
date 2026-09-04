from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from main import AgentConfig, WorkingFolderAgent
from mutations import batch_mutate, replace_symbol
from tree_loop import TreeLoop
from tree_commands import CommandResult


class FakeModelClient:
    def complete(self, system: str, prompt: str) -> str:
        return '{"thought": "", "action": {"type": "finish", "message": "unused"}}'

    def get_last_metrics(self) -> dict:
        return {}

    def clone(self) -> "FakeModelClient":
        return FakeModelClient()


class FakeTreeModel:
    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or ["finish done"])

    def complete(self, system: str, prompt: str) -> str:
        if self._responses:
            return self._responses.pop(0)
        return "finish done"


class MutationPackageTests(unittest.TestCase):
    def test_replace_symbol_rewrites_python_function_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "sample.py"
            target.write_text("def greet():\n    return 'hi'\n", encoding="utf-8")

            result = replace_symbol(
                "sample.py",
                "greet",
                "function",
                "def greet():\n    return 'bye'",
                root=root,
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["applied"])
            self.assertIn("return 'bye'", target.read_text(encoding="utf-8"))

    def test_batch_mutate_atomic_rolls_back_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "sample.py"
            target.write_text("alpha\nbeta\n", encoding="utf-8")

            result = batch_mutate(
                [
                    {"type": "replace_snippet", "file_path": "sample.py", "old_text": "alpha", "new_text": "omega"},
                    {"type": "replace_snippet", "file_path": "sample.py", "old_text": "missing", "new_text": "value"},
                ],
                atomic=True,
                root=root,
            )

            self.assertFalse(result["ok"])
            self.assertTrue(result["rolled_back"])
            self.assertEqual(target.read_text(encoding="utf-8"), "alpha\nbeta\n")


class MutationCliTests(unittest.TestCase):
    def test_replace_range_subcommand_returns_mutation_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "sample.py"
            target.write_text("one\ntwo\nthree\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "agent_tools.py"),
                    "replace-range",
                    "--root",
                    str(root),
                    "--path",
                    "sample.py",
                    "--start-line",
                    "2",
                    "--end-line",
                    "2",
                    "--new-text",
                    "updated",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["tool"], "replace_range")
            self.assertEqual(payload["data"]["mutation_type"], "replace_range")
            self.assertTrue(payload["data"]["applied"])
            self.assertEqual(target.read_text(encoding="utf-8"), "one\nupdated\nthree\n")


class StableRuntimeMutationTests(unittest.TestCase):
    def test_explicit_replace_range_action_sets_verification_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "sample.py"
            target.write_text("one\ntwo\nthree\n", encoding="utf-8")

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

            result = agent._execute_action(
                {
                    "type": "replace_range",
                    "path": "sample.py",
                    "start_line": 2,
                    "end_line": 2,
                    "new_text": "updated",
                }
            )

            self.assertTrue(result.ok)
            self.assertTrue(result.payload.get("verification_pending"))
            self.assertEqual(target.read_text(encoding="utf-8"), "one\nupdated\nthree\n")


class TreeLoopMutationExecutionTests(unittest.TestCase):
    def test_execute_tool_supports_replace_snippet_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "sample.py"
            target.write_text("print('hello')\n", encoding="utf-8")
            loop = TreeLoop(model=FakeTreeModel(), workspace_root=root, max_turns=1, verbose=False)

            result = loop._execute_tool(
                CommandResult(
                    ok=True,
                    output="",
                    command_type="write",
                    needs_tool=True,
                    tool_action={
                        "type": "replace_snippet",
                        "path": "sample.py",
                        "old_text": "hello",
                        "new_text": "world",
                    },
                )
            )

            self.assertIn("replace snippet", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "print('world')\n")


if __name__ == "__main__":
    unittest.main()