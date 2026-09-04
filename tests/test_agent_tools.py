from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_tools import patch_already_applied


class PatchAlreadyAppliedTests(unittest.TestCase):
    def test_delete_patch_is_not_treated_as_already_applied_when_search_is_missing(self) -> None:
        content = "import { render } from '@testing-library/react';\nconst value = 1;\n"

        self.assertFalse(patch_already_applied(content, "import React from 'react';\n", ""))

    def test_non_empty_replacement_can_still_be_treated_as_already_applied(self) -> None:
        content = "const greeting = 'world';\n"

        self.assertTrue(patch_already_applied(content, "const greeting = 'hello';\n", "const greeting = 'world';\n"))


class DiagnosticsCliTests(unittest.TestCase):
    def test_syntax_check_subcommand_reports_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "bad.json").write_text('{"ok": }\n', encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "agent_tools.py"),
                    "syntax-check",
                    "--root",
                    str(root),
                    "--path",
                    "bad.json",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["tool"], "syntax_check")
            self.assertFalse(payload["data"]["ok"])
            self.assertEqual(payload["data"]["summary"]["error"], 1)

    def test_changed_files_check_subcommand_reports_git_changed_file_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            (root / "tracked.py").write_text("print('ok')\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
            (root / "bad.json").write_text('{"ok": }\n', encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "agent_tools.py"),
                    "changed-files-check",
                    "--root",
                    str(root),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["tool"], "changed_files_check")
            self.assertFalse(payload["data"]["ok"])
            self.assertGreaterEqual(payload["data"]["summary"]["error"], 1)


if __name__ == "__main__":
    unittest.main()