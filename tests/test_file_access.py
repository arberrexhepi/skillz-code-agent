import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from file_access import read_scope
from discovery.dispatch import execute_discovery_action

REPO_ROOT = Path(__file__).resolve().parents[1]


class ReadAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillz-read-grants-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "repo"
        self.documents = self.root.parent / "documents"
        self.outside = self.root.parent / "outside"
        for directory in (self.root, self.documents, self.outside):
            directory.mkdir()
        (self.documents / "notes ë.txt").write_text("hello ë", encoding="utf-8")
        self.grants = json.dumps([{"id": "documents", "path": str(self.documents)}])

    def call(self, tool, *args, grants="[]"):
        result = subprocess.run([sys.executable, str(REPO_ROOT / "agent_tools.py"), tool, "--root", str(self.root), *args], capture_output=True, text=True, encoding="utf-8", env={**os.environ, "SKILLZ_READ_ROOTS": grants, "SKILLZ_CONTEXT_ROOT": "", "PYTHONIOENCODING": "utf-8"})
        return json.loads(result.stdout)

    def test_default_rejects_other_folders_and_write_remains_repository_bound(self):
        file = str(self.documents / "notes ë.txt")
        self.assertFalse(self.call("read", "--path", file)["ok"])
        self.assertTrue(self.call("read", "--path", file, grants=self.grants)["ok"])
        self.assertFalse(self.call("write", "--path", file, "--content", "BAD", grants=self.grants)["ok"])
        self.assertEqual((self.documents / "notes ë.txt").read_text(encoding="utf-8"), "hello ë")
        self.assertTrue(self.call("write", "--path", "own.txt", "--content", "yes", grants=self.grants)["ok"])

    def test_traversal_and_symlink_escape_are_rejected(self):
        with patch.dict(os.environ, {"SKILLZ_READ_ROOTS": self.grants, "SKILLZ_CONTEXT_ROOT": ""}):
            with self.assertRaises(ValueError):
                read_scope(self.root, str(self.documents) + "/../outside/secret.txt")
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(self.documents / "escape"), str(self.outside)], check=True, capture_output=True)
            else:
                (self.documents / "escape").symlink_to(self.outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                read_scope(self.root, str(self.documents / "escape/secret.txt"))

    def test_beta_and_stable_reads_keep_the_granted_path(self):
        file = str(self.documents / "notes ë.txt")
        read = self.call("read", "--path", file, grants=self.grants)
        self.assertEqual(Path(read["data"]["path"]), Path(file))
        with patch.dict(os.environ, {"SKILLZ_READ_ROOTS": self.grants, "SKILLZ_CONTEXT_ROOT": ""}):
            result = execute_discovery_action({"type": "read_file", "path": file}, root=self.root)
            self.assertEqual(result["content"], "hello ë")
            self.assertEqual(Path(result["file_path"]), Path(file))

if __name__ == "__main__":
    unittest.main()
