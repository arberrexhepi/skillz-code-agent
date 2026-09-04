from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from discovery import (
    find_symbol_definitions,
    find_symbol_references,
    investigate,
    outline_file,
    repo_map,
    trace_dependencies,
)


class DiscoverySuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "core.py").write_text(
            "from src.util import helper\n\n"
            "CONFIG_NAME = 'planner'\n\n"
            "class Planner:\n"
            "    def decide(self, value: str) -> str:\n"
            "        return helper(value)\n\n"
            "def build_planner() -> Planner:\n"
            "    return Planner()\n",
            encoding="utf-8",
        )
        (self.root / "src" / "util.py").write_text(
            "def helper(value: str) -> str:\n"
            "    return value.upper()\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_core.py").write_text(
            "from src.core import Planner\n\n"
            "def test_planner_round_trip() -> None:\n"
            "    planner = Planner()\n"
            "    assert planner.decide('ok') == 'OK'\n",
            encoding="utf-8",
        )
        (self.root / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\npythonpath = ['.']\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_outline_file_reports_symbols_imports_and_exports(self) -> None:
        result = outline_file("src/core.py", root=self.root)

        self.assertTrue(result["ok"])
        self.assertEqual(result["language"], "python")
        self.assertEqual(result["summary"]["import_count"], 1)
        symbol_names = {symbol["name"] for symbol in result["symbols"]}
        self.assertIn("Planner", symbol_names)
        self.assertIn("build_planner", symbol_names)
        exported_names = {item["name"] for item in result["exports"]}
        self.assertIn("Planner", exported_names)

    def test_symbol_definition_and_reference_search_distinguishes_definition(self) -> None:
        definitions = find_symbol_definitions("Planner", path=".", root=self.root)
        references = find_symbol_references("Planner", path=".", root=self.root)

        self.assertTrue(definitions["ok"])
        self.assertEqual(definitions["hits"][0]["file_path"], "src/core.py")
        self.assertTrue(definitions["hits"][0]["is_definition"])
        self.assertTrue(references["ok"])
        self.assertTrue(any(hit["file_path"] == "tests/test_core.py" and hit["is_reference"] for hit in references["hits"]))

    def test_trace_dependencies_and_investigate_surface_related_context(self) -> None:
        trace = trace_dependencies("src/core.py", direction="both", depth=2, root=self.root)
        result = investigate("Planner", path=".", mode="standard", root=self.root)

        self.assertTrue(trace["ok"])
        self.assertTrue(any(edge["to"] == "src/util.py" for edge in trace["imports"]))
        self.assertTrue(any(edge["from"] == "tests/test_core.py" for edge in trace["imported_by"]))
        self.assertTrue(result["ok"])
        self.assertIn("src/core.py", result["likely_edit_targets"])
        self.assertIn("tests/test_core.py", result["related_tests"])
        self.assertIn("pyproject.toml", result["related_configs"])

    def test_repo_map_returns_ranked_structural_context_and_drill_down(self) -> None:
        result = repo_map(path=".", topic="Planner", limit=5, symbols_per_file=4, root=self.root)

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["summary"]["symbol_count"], 3)
        self.assertGreaterEqual(result["summary"]["dependency_edge_count"], 1)
        paths = [item["file_path"] for item in result["files"]]
        self.assertIn("src/core.py", paths)
        core = next(item for item in result["files"] if item["file_path"] == "src/core.py")
        self.assertTrue(any(symbol["name"] == "Planner" for symbol in core["symbols"]))
        self.assertTrue(result["drill_down"])
        self.assertIn(result["drill_down"][0]["next_action"]["type"], {"read_symbol", "outline_file"})


class DiscoveryCliTests(unittest.TestCase):
    def test_outline_file_subcommand_returns_structured_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "module.py").write_text(
                "def run() -> None:\n"
                "    pass\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "agent_tools.py"),
                    "outline-file",
                    "--root",
                    str(root),
                    "--path",
                    "src/module.py",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["tool"], "outline_file")
            self.assertTrue(payload["data"]["ok"])
            self.assertEqual(payload["data"]["file_path"], "src/module.py")

    def test_repo_map_subcommand_returns_structural_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "module.py").write_text(
                "def run() -> None:\n"
                "    pass\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "agent_tools.py"),
                    "repo-map",
                    "--root",
                    str(root),
                    "--path",
                    ".",
                    "--topic",
                    "run",
                    "--limit",
                    "5",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["tool"], "repo_map")
            self.assertTrue(payload["data"]["ok"])
            self.assertEqual(payload["data"]["files"][0]["file_path"], "src/module.py")


if __name__ == "__main__":
    unittest.main()
