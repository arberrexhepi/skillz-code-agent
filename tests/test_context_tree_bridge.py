"""Tests for context_tree_bridge.py."""

import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence

from context_tree_bridge import ContextTreeBridge


@dataclass
class FakeFactRecord:
    issue_id: str = "42"
    fact_type: str = "architecture"
    key: str = "entrypoint"
    value: str = "main.py"
    updated_step: int = 1
    updated_run_id: int = 1


@dataclass
class FakeMemoryItem:
    id: str = "mem-001"
    summary: str = "Read main.py lines 1-50"
    kind: str = "read_file"
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {"tool": "read", "paths": ["src/main.py"], "tags": ["read"], "importance": 0.8, "produced_by_step": 1}


class TestContextTreeBridge(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        src = os.path.join(self.tmpdir, "src")
        os.makedirs(src)
        Path(os.path.join(src, "main.py")).write_text("def main():\n    print('hello')\n")
        Path(os.path.join(src, "utils.py")).write_text("def helper():\n    return 1\n")
        Path(os.path.join(self.tmpdir, "README.md")).write_text("# Project\n")

        self.facts = [FakeFactRecord()]
        self.memory_items = [FakeMemoryItem()]
        self.status = {
            "task_satisfied": False,
            "edit_batch_mode": False,
            "completion_check_pending": False,
        }

        self.bridge = ContextTreeBridge(
            workspace_root=Path(self.tmpdir),
            get_fact_records=lambda: self.facts,
            get_memory_items=lambda: self.memory_items,
            get_status=lambda: self.status,
        )

    def test_setup(self):
        result = self.bridge.setup()
        self.assertGreaterEqual(result["files_indexed"], 3)
        self.assertGreater(result["fact_count"], 0)

    def test_render_for_prompt(self):
        self.bridge.setup()
        block = self.bridge.render_for_prompt()
        self.assertIn("WORKSPACE TREE", block)
        self.assertIn("FACTS", block)
        self.assertIn("MEMORY", block)
        self.assertIn("STATUS", block)
        self.assertIn("entrypoint", block)

    def test_render_with_hot_files(self):
        self.bridge.setup()
        block = self.bridge.render_for_prompt(hot_files=["src/main.py"])
        self.assertIn("HOT FILES", block)
        self.assertIn("def main():", block)

    def test_render_command_grammar(self):
        grammar = self.bridge.render_command_grammar()
        self.assertIn("ls /repo", grammar)
        self.assertIn("cat", grammar)
        self.assertIn("patch", grammar)
        self.assertIn("skill", grammar)

    def test_is_tree_command(self):
        self.assertTrue(self.bridge.is_tree_command("ls /repo"))
        self.assertTrue(self.bridge.is_tree_command("cat /repo/src/main.py"))
        self.assertTrue(self.bridge.is_tree_command("git status"))
        self.assertTrue(self.bridge.is_tree_command("finish Done."))
        self.assertFalse(self.bridge.is_tree_command('{"type": "read_file"}'))
        self.assertFalse(self.bridge.is_tree_command(""))

    def test_execute_read(self):
        self.bridge.setup()
        results = self.bridge.execute("ls /repo")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].command_type, "read")
        self.assertFalse(results[0].needs_tool)

    def test_execute_multi_reads(self):
        self.bridge.setup()
        raw = "ls /repo\ncat /repo/src/main.py\nfind /repo *.py"
        results = self.bridge.execute(raw)
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertTrue(r.ok)
            self.assertFalse(r.needs_tool)

    def test_execute_write_needs_tool(self):
        self.bridge.setup()
        results = self.bridge.execute("patch /repo/src/main.py hello -> goodbye")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].needs_tool)
        self.assertEqual(results[0].tool_action["type"], "patch_file")

    def test_execute_fact(self):
        self.bridge.setup()
        results = self.bridge.execute("fact 42/architecture/db postgres")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].needs_tool)
        self.assertEqual(results[0].tool_action["type"], "set_fact")
        # Also written to tree immediately
        val = self.bridge.tree.get_fact("42", "architecture", "db")
        self.assertEqual(val, "postgres")

    def test_on_write_complete(self):
        self.bridge.setup()
        # Load the file
        _ = self.bridge.tree.cat("/repo/src/main.py")
        node = self.bridge.tree.resolve("/repo/src/main.py")
        self.assertTrue(node.content_loaded())
        # Simulate write
        self.bridge.on_write_complete("src/main.py")
        self.assertFalse(node.content_loaded())

    def test_skill_registration(self):
        self.bridge.setup()
        self.bridge.register_skill(
            "typecheck",
            "Run TypeScript type checker",
            cache="0 errors found",
        )
        results = self.bridge.execute("skill typecheck")
        self.assertTrue(results[0].ok)
        self.assertIn("0 errors", results[0].output)

    def test_skill_with_handler(self):
        self.bridge.setup()
        self.bridge.register_skill(
            "count_lines",
            "Count lines in a file",
            handler=lambda path="": f"42 lines in {path}",
        )
        results = self.bridge.execute("skill count_lines path=src/main.py")
        self.assertIn("42 lines", results[0].output)

    def test_execute_single(self):
        self.bridge.setup()
        result = self.bridge.execute_single("stat /repo/src/main.py")
        self.assertTrue(result.ok)
        data = json.loads(result.output)
        self.assertEqual(data["type"], "file")

    def test_mixed_read_write_commands(self):
        """Multi-command with both reads and writes."""
        self.bridge.setup()
        raw = """ls /repo
cat /repo/README.md
patch /repo/src/main.py hello -> goodbye
finish Applied changes."""
        results = self.bridge.execute(raw)
        self.assertEqual(len(results), 4)
        # First two are free reads
        self.assertFalse(results[0].needs_tool)
        self.assertFalse(results[1].needs_tool)
        # Last two need tool dispatch
        self.assertTrue(results[2].needs_tool)
        self.assertTrue(results[3].needs_tool)

    def test_grep_facts(self):
        self.bridge.setup()
        results = self.bridge.execute('grep /facts "main"')
        self.assertTrue(results[0].ok)
        # Should find the entrypoint fact
        self.assertIn("main", results[0].output.lower())

    # ── Strategy tests through the bridge ──────────────────────────────

    def test_is_tree_command_detects_strategy(self):
        self.assertTrue(self.bridge.is_tree_command("s1: ls /repo"))
        self.assertTrue(self.bridge.is_tree_command("s1: cat /repo/main.py\ns1 -> s2: ls /repo"))

    def test_execute_dispatches_strategy(self):
        self.bridge.setup()
        raw = "s1: cat /repo/README.md, ls /repo"
        results = self.bridge.execute(raw)
        # Flattened: 2 results from s1
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results))

    def test_execute_strategy_full(self):
        self.bridge.setup()
        raw = "s1: cat /repo/README.md\ns1 -> s2: ls /repo"
        by_label = self.bridge.execute_strategy_full(raw)
        self.assertIn("s1", by_label)
        self.assertIn("s2", by_label)
        self.assertTrue(by_label["s1"][0].ok)
        self.assertTrue(by_label["s2"][0].ok)

    def test_format_strategy(self):
        self.bridge.setup()
        raw = "s1: cat /repo/README.md, ls /repo"
        by_label = self.bridge.execute_strategy_full(raw)
        formatted = self.bridge.format_strategy(by_label)
        self.assertIn("[✓ s1 READ]", formatted)

    def test_strategy_grammar_in_prompt(self):
        grammar = self.bridge.render_command_grammar()
        self.assertIn("STRATEGIES", grammar)
        self.assertIn("s1:", grammar)
        self.assertIn("{sN}", grammar)


if __name__ == "__main__":
    unittest.main()
