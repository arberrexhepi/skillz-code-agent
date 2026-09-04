from __future__ import annotations

import json
import importlib.util
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from diagnostics import (
    build_check,
    changed_files_check,
    config_validate,
    dependency_check,
    duplication_check,
    project_problems,
    security_check,
    syntax_check,
    test_check as run_test_check,
)


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class DiagnosticsSuiteTests(unittest.TestCase):
    def test_syntax_check_reports_python_and_json_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
            (root / "bad.json").write_text('{"ok": }\n', encoding="utf-8")

            result = syntax_check(["bad.py", "bad.json"], root=root)

            self.assertFalse(result["ok"])
            self.assertEqual(result["summary"]["error"], 2)
            self.assertEqual({item["file"] for item in result["diagnostics"]}, {"bad.py", "bad.json"})

    def test_syntax_check_preserves_node_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "bad.js").write_text("function broken( {\n", encoding="utf-8")
            _write_executable(
                root / "node_modules" / ".bin" / "node",
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "print('bad.js:3')\n"
                "print('Unexpected token from stderr', file=sys.stderr)\n"
                "sys.exit(1)\n",
            )

            result = syntax_check(["bad.js"], root=root)

            self.assertFalse(result["ok"])
            message = result["diagnostics"][0]["message"]
            self.assertIn("bad.js:3", message)
            self.assertIn("Unexpected token from stderr", message)

    def test_config_validate_reports_structural_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text(json.dumps({"scripts": []}) + "\n", encoding="utf-8")
            (root / ".env").write_text("GOOD=1\nBROKEN\n", encoding="utf-8")

            result = config_validate(["package.json", ".env"], root=root)

            self.assertFalse(result["ok"])
            self.assertGreaterEqual(result["summary"]["error"], 2)
            messages = "\n".join(item["message"] for item in result["diagnostics"])
            self.assertIn("scripts", messages)
            self.assertIn("must contain '='", messages)

    def test_dependency_check_reports_missing_relative_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pkg = root / "pkg"
            pkg.mkdir(parents=True, exist_ok=True)
            (pkg / "module.py").write_text("from .missing import thing\n", encoding="utf-8")
            src = root / "src"
            src.mkdir(parents=True, exist_ok=True)
            (src / "index.ts").write_text("import { value } from './missing';\nconsole.log(value)\n", encoding="utf-8")

            result = dependency_check(["pkg/module.py", "src/index.ts"], root=root)

            self.assertFalse(result["ok"])
            self.assertEqual(result["summary"]["error"], 2)
            self.assertTrue(all(item["code"] == "MISSING_IMPORT" for item in result["diagnostics"]))

    def test_duplication_check_reports_duplicate_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            common_block = "alpha\nbeta\ngamma\n"
            (root / "one.py").write_text(common_block + "delta\n", encoding="utf-8")
            (root / "two.py").write_text("start\n" + common_block + "end\n", encoding="utf-8")

            result = duplication_check(["one.py", "two.py"], threshold=3, root=root)

            self.assertTrue(result["diagnostics"])
            self.assertEqual(result["diagnostics"][0]["category"], "duplication")

    def test_build_check_reports_failed_build_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text(json.dumps({"scripts": {"build": "fake"}}) + "\n", encoding="utf-8")
            _write_executable(
                root / "node_modules" / ".bin" / "npm",
                "#!/usr/bin/env python3\nimport sys\nprint('build exploded', file=sys.stderr)\nsys.exit(1)\n",
            )

            result = build_check(root=root)

            self.assertFalse(result["ok"])
            self.assertEqual(result["diagnostics"][0]["category"], "build")
            self.assertIn("build exploded", result["diagnostics"][0]["message"])

    def test_build_check_preserves_stdout_when_stderr_is_also_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text(json.dumps({"scripts": {"build": "fake"}}) + "\n", encoding="utf-8")
            _write_executable(
                root / "node_modules" / ".bin" / "npm",
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "print('compile failed in stdout')\n"
                "print('warning from stderr', file=sys.stderr)\n"
                "sys.exit(1)\n",
            )

            result = build_check(root=root)

            self.assertFalse(result["ok"])
            message = result["diagnostics"][0]["message"]
            self.assertIn("compile failed in stdout", message)
            self.assertIn("warning from stderr", message)

    @unittest.skipUnless(importlib.util.find_spec("pytest") is not None, "pytest is not installed")
    def test_test_check_runs_pytest_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tests_dir = root / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)
            (tests_dir / "test_failure.py").write_text(
                textwrap.dedent(
                    """\
                    def test_failure():
                        assert 1 == 2
                    """
                ),
                encoding="utf-8",
            )

            result = run_test_check(root=root)

            self.assertFalse(result["ok"])
            self.assertEqual(result["diagnostics"][0]["category"], "test")
            self.assertIn("assert 1 == 2", result["diagnostics"][0]["message"])

    def test_test_check_reports_exit_code_when_runner_emits_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tests_dir = root / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)
            (tests_dir / "test_placeholder.py").write_text(
                textwrap.dedent(
                    """\
                    def test_placeholder():
                        assert True
                    """
                ),
                encoding="utf-8",
            )
            python_bin = root / ".venv" / "bin"
            python_bin.mkdir(parents=True, exist_ok=True)
            _write_executable(
                python_bin / "python",
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.exit(7)\n",
            )

            result = run_test_check(root=root)

            self.assertFalse(result["ok"])
            self.assertEqual(result["diagnostics"][0]["category"], "test")
            self.assertIn("exit code 7", result["diagnostics"][0]["message"])

    def test_changed_files_check_aggregates_git_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            (root / "tracked.py").write_text("print('ok')\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
            (root / "package.json").write_text('{"scripts": []}\n', encoding="utf-8")

            result = changed_files_check(root=root)

            self.assertFalse(result["ok"])
            self.assertTrue(any(item["category"] == "config" for item in result["diagnostics"]))

    def test_project_problems_standard_combines_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            (root / "tests").mkdir(parents=True, exist_ok=True)
            (root / "tests" / "test_ok.py").write_text(
                textwrap.dedent(
                    """\
                    def test_ok():
                        assert True
                    """
                ),
                encoding="utf-8",
            )
            (root / "bad.json").write_text('{"oops": }\n', encoding="utf-8")

            result = project_problems(mode="standard", root=root)

            self.assertFalse(result["ok"])
            categories = {item["category"] for item in result["diagnostics"]}
            self.assertTrue(any("test_check" in item for item in result["raw_sources"]))
            self.assertIn("config", categories)

    def test_security_check_finds_secret_literals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "app.py").write_text('API_KEY = "ABCDEFGHIJKLMNOPQRSTUVWX"\n', encoding="utf-8")

            result = security_check(["app.py"], root=root)

            self.assertFalse(result["ok"])
            self.assertEqual(result["diagnostics"][0]["category"], "security")
