"""Tests for context_tree.py and tree_commands.py."""

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from context_tree import ContextTree, DirNode, FileNode, _resolve
from tree_commands import (
    CommandResult,
    TreeCommandParser,
    collapse_heredocs,
    execute_multi,
    execute_strategy,
    format_strategy_results,
    is_strategy,
    parse_multi_command,
    parse_strategy,
)


class TestTreeNodes(unittest.TestCase):
    def test_dir_add_file(self):
        root = DirNode(name="")
        f = root.add_file("hello.txt", content="hello world", size=11, lines=1)
        self.assertIsInstance(f, FileNode)
        self.assertEqual(f.content, "hello world")
        self.assertEqual(f.path(), "hello.txt")

    def test_dir_add_dir(self):
        root = DirNode(name="")
        sub = root.add_dir("src")
        sub.add_file("main.py", content="print('hi')")
        self.assertEqual(len(sub.children), 1)
        node = _resolve(root, "src/main.py")
        self.assertIsInstance(node, FileNode)
        self.assertEqual(node.content, "print('hi')")

    def test_resolve_missing(self):
        root = DirNode(name="")
        self.assertIsNone(_resolve(root, "nonexistent"))

    def test_lazy_loading(self):
        loaded = [False]
        def loader():
            loaded[0] = True
            return "lazy content"

        root = DirNode(name="")
        f = root.add_file("lazy.txt", loader=loader, size=12, lines=1)
        self.assertFalse(loaded[0])
        self.assertFalse(f.content_loaded())
        content = f.content
        self.assertTrue(loaded[0])
        self.assertEqual(content, "lazy content")
        self.assertTrue(f.content_loaded())

    def test_ls_depth(self):
        root = DirNode(name="")
        src = root.add_dir("src")
        src.add_file("a.py", content="a")
        sub = src.add_dir("utils")
        sub.add_file("b.py", content="b")

        # depth=1 should show src/ but not contents
        entries_d1 = root.ls(depth=1)
        names = [e["name"] for e in entries_d1]
        self.assertIn("src/", names)
        self.assertNotIn("src/a.py", names)

        # depth=2 should show src/ contents
        entries_d2 = root.ls(depth=2)
        names2 = [e["name"] for e in entries_d2]
        self.assertIn("src/a.py", names2)
        self.assertIn("src/utils/", names2)

    def test_walk(self):
        root = DirNode(name="")
        src = root.add_dir("src")
        src.add_file("a.py", content="a")
        sub = src.add_dir("utils")
        sub.add_file("b.py", content="b")

        all_nodes = list(root.walk())
        names = [n.name for n in all_nodes]
        self.assertIn("src", names)
        self.assertIn("a.py", names)
        self.assertIn("utils", names)
        self.assertIn("b.py", names)


class TestContextTree(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create a small workspace
        src = os.path.join(self.tmpdir, "src")
        os.makedirs(src)
        Path(os.path.join(src, "main.py")).write_text("def main():\n    pass\n")
        Path(os.path.join(src, "utils.py")).write_text("# utils\ndef helper():\n    return 1\n")
        Path(os.path.join(self.tmpdir, "README.md")).write_text("# Hello\n")

        self.tree = ContextTree(Path(self.tmpdir))

    def test_index_repo(self):
        count = self.tree.index_repo()
        self.assertGreaterEqual(count, 3)

        # Can ls /repo
        entries = self.tree.ls("/repo")
        names = [e.get("name", e.get("path", "").split("/")[-1]) for e in entries]
        self.assertIn("src/", names)
        self.assertIn("README.md", names)

    def test_index_repo_records_line_counts_before_cat(self):
        self.tree.index_repo()
        entries = self.tree.ls("/repo/src")
        by_name = {entry["name"]: entry for entry in entries}
        self.assertEqual(by_name["main.py"]["lines"], 2)
        self.assertEqual(by_name["utils.py"]["lines"], 3)

    def test_cat_lazy(self):
        self.tree.index_repo()
        content = self.tree.cat("/repo/src/main.py")
        self.assertIn("def main():", content)
        self.assertIn("1 | def main():", content)

    def test_cat_line_range(self):
        self.tree.index_repo()
        content = self.tree.cat("/repo/src/utils.py", start_line=2, end_line=2)
        self.assertIn("def helper():", content)
        self.assertNotIn("# utils", content)

    def test_cat_large_file_returns_read_line_range_hint(self):
        big_lines = "\n".join(f"line {i}" for i in range(1, 260)) + "\n"
        Path(os.path.join(self.tmpdir, "src", "big.ts")).write_text(big_lines)
        self.tree.index_repo()
        content = self.tree.cat("/repo/src/big.ts")
        self.assertIn("[file too large to dump fully:", content)
        self.assertIn("Use `read-line-range /repo/src/big.ts <start>-<end>`", content)
        self.assertIn("Previewing lines 1-80.", content)
        self.assertIn(" 1 | line 1", content)
        self.assertNotIn("259 | line 259", content)

    def test_find(self):
        self.tree.index_repo()
        results = self.tree.find("/repo", "*.py")
        self.assertTrue(any("main.py" in r for r in results))

    def test_extract_symbols_for_python_and_ts(self):
        Path(os.path.join(self.tmpdir, "src", "widget.ts")).write_text(
            "export function renderWidget() {\n"
            "  return 1;\n"
            "}\n"
            "const widgetValue = 3;\n"
        )
        self.tree.index_repo()

        py_symbols = self.tree.extract_symbols("/repo/src/main.py")
        self.assertTrue(any(symbol["name"] == "main" and symbol["kind"] == "function" for symbol in py_symbols))

        ts_symbols = self.tree.extract_symbols("/repo/src/widget.ts")
        self.assertTrue(any(symbol["name"] == "renderWidget" and symbol["kind"] == "function" for symbol in ts_symbols))
        self.assertTrue(any(symbol["name"] == "widgetValue" and symbol["kind"] == "variable" for symbol in ts_symbols))

    def test_find_symbols_across_repo_subtree(self):
        Path(os.path.join(self.tmpdir, "src", "widget.ts")).write_text(
            "export const useTodo = () => 1;\n"
        )
        self.tree.index_repo()

        matches = self.tree.find_symbols("/repo/src", "useTodo")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["path"], "/repo/src/widget.ts")
        self.assertEqual(matches[0]["kind"], "variable")

    def test_grep(self):
        self.tree.index_repo()
        # Must preload content first for /repo grep
        self.tree.preload_files(["src/main.py", "src/utils.py"])
        results = self.tree.grep("/repo", "def ")
        self.assertGreater(len(results), 0)

    def test_facts(self):
        self.tree.set_fact("issue-42", "architecture", "entrypoint", "main.py is the entry")
        val = self.tree.get_fact("issue-42", "architecture", "entrypoint")
        self.assertEqual(val, "main.py is the entry")

        # ls /facts should show structure
        entries = self.tree.ls("/facts", depth=3)
        self.assertTrue(len(entries) > 0)

    def test_sync_facts(self):
        class FakeRecord:
            issue_id = "42"
            fact_type = "architecture"
            key = "db"
            value = "postgres"
            updated_step = 5
            updated_run_id = 1

        self.tree.sync_facts([FakeRecord()])
        val = self.tree.get_fact("42", "architecture", "db")
        self.assertEqual(val, "postgres")

    def test_skills(self):
        self.tree.register_skill(
            "lint",
            "Run linter on a file",
            args_schema={"path": "str"},
            handler=lambda path="": f"Linted {path}: 0 errors",
        )
        result = self.tree.invoke_skill("lint", path="src/main.py")
        self.assertIn("0 errors", result)

        skills = self.tree.list_skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "lint")

    def test_skills_include_metadata(self):
        self.tree.register_skill(
            "testing_playbook",
            "Testing guidance",
            tags=["testing", "runtime"],
            category="testing",
            priority=80,
            modes=["fast", "deep"],
            cache="# Testing Playbook",
        )

        skills = self.tree.list_skills()

        self.assertEqual(skills[0]["category"], "testing")
        self.assertEqual(skills[0]["priority"], "80")
        self.assertEqual(skills[0]["tags"], "testing, runtime")
        self.assertEqual(skills[0]["modes"], "fast, deep")

    def test_skill_with_cache(self):
        self.tree.register_skill(
            "readme",
            "Get project readme",
            cache="# Hello\nThis is the project.",
        )
        result = self.tree.invoke_skill("readme")
        self.assertEqual(result, "# Hello\nThis is the project.")

    def test_status(self):
        self.tree.sync_status({
            "completion_check_pending": False,
            "edit_batch_mode": False,
            "task_satisfied": True,
        })
        entries = self.tree.ls("/status")
        names = [e.get("path", "").split("/")[-1] for e in entries]
        self.assertIn("task_satisfied", names)

    def test_render_prompt_block(self):
        self.tree.index_repo()
        self.tree.set_fact("42", "architecture", "db", "postgres")
        self.tree.sync_status({"step": 5})
        block = self.tree.render_prompt_block(repo_depth=1)
        self.assertIn("WORKSPACE TREE", block)
        self.assertIn("FACTS", block)
        self.assertIn("STATUS", block)

    def test_invalidate_file(self):
        self.tree.index_repo()
        _ = self.tree.cat("/repo/src/main.py")  # load it
        node = self.tree.resolve("/repo/src/main.py")
        self.assertTrue(node.content_loaded())
        self.tree.invalidate_file("src/main.py")
        self.assertFalse(node.content_loaded())

    def test_refresh_repo_file_adds_new_file_after_initial_index(self):
        self.tree.index_repo()
        new_file = Path(self.tmpdir, "src", "new_context.py")
        new_file.write_text("VALUE = 7\n")

        self.tree.refresh_repo_file("src/new_context.py")

        listing = self.tree.find("/repo", "new_context.py")
        self.assertIn("repo/src/new_context.py", listing)
        content = self.tree.cat("/repo/src/new_context.py")
        self.assertIn("VALUE = 7", content)

    def test_refresh_repo_file_removes_deleted_file(self):
        self.tree.index_repo()
        doomed = Path(self.tmpdir, "src", "utils.py")
        doomed.unlink()

        self.tree.refresh_repo_file("src/utils.py")

        content = self.tree.cat("/repo/src/utils.py")
        self.assertTrue(content.startswith("[not found: "))

    def test_ingest_log_issues(self):
        log_path = Path(self.tmpdir, "live_trace.md")
        log_path.write_text(textwrap.dedent("""\
            3:21:23 PM [vite] Internal server error: Failed to resolve import "uuid" from "src/hooks/useIndexedDbTodos.ts". Does the file exist?
              Plugin: vite:import-analysis
              File: /tmp/app/src/hooks/useIndexedDbTodos.ts:5:29
            4:12:36 PM [vite] Internal server error: Failed to resolve import "./IndexedDbTodoItem" from "src/components/IndexedDbTodoList.tsx". Does the file exist?
              Plugin: vite:import-analysis
              File: /tmp/app/src/components/IndexedDbTodoList.tsx:3:30
        """))
        self.tree.index_repo()
        issues = self.tree.ingest_log_issues("/repo/live_trace.md")
        self.assertEqual(len(issues), 2)
        listed = self.tree.list_log_issues()
        self.assertEqual(len(listed), 2)
        shown = self.tree.show_log_issue(issues[0]["id"])
        self.assertIsNotNone(shown)
        self.assertEqual(shown["status"], "open")
        classifications = {issue["classification"] for issue in listed}
        self.assertIn("dependency_resolution_error", classifications)
        self.assertIn("local_import_missing", classifications)

    def test_resolve_log_issue(self):
        log_path = Path(self.tmpdir, "live_trace.md")
        log_path.write_text(textwrap.dedent("""\
            3:21:23 PM [vite] Internal server error: Failed to resolve import "uuid" from "src/hooks/useIndexedDbTodos.ts". Does the file exist?
              Plugin: vite:import-analysis
              File: /tmp/app/src/hooks/useIndexedDbTodos.ts:5:29
        """))
        self.tree.index_repo()
        issues = self.tree.ingest_log_issues("/repo/live_trace.md")
        issue_id = issues[0]["id"]
        self.assertTrue(self.tree.resolve_log_issue(issue_id))
        shown = self.tree.show_log_issue(issue_id)
        self.assertEqual(shown["status"], "resolved")

    def test_ingest_log_issues_classifies_runtime_command_failure(self):
        log_path = Path(self.tmpdir, "live_trace.md")
        log_path.write_text(textwrap.dedent("""\
            [✓ TOOL] shell npm install
            ⟶ unhandled action type: run_shell
        """))
        self.tree.index_repo()
        issues = self.tree.ingest_log_issues("/repo/live_trace.md")
        self.assertEqual(len(issues), 1)
        shown = self.tree.show_log_issue(issues[0]["id"])
        self.assertEqual(shown["classification"], "runtime_command_failure")

    def test_ingest_log_issues_parses_embedded_typescript_diagnostic(self):
        log_path = Path(self.tmpdir, "live_trace.md")
        log_path.write_text(textwrap.dedent("""\
            [{
              "resource": "/Users/artistefoundation/dev/test-app/src/components/DemoTodoInput.tsx",
              "owner": "typescript",
              "code": "2305",
              "severity": 8,
              "message": "Module '\\"../context/DemoTodoContext\\"' has no exported member 'useTodo'.",
              "source": "ts",
              "startLineNumber": 2,
              "startColumn": 10,
              "endLineNumber": 2,
              "endColumn": 17,
              "modelVersionId": 1,
              "origin": "extHost1"
            }]
        """))
        self.tree.index_repo()
        issues = self.tree.ingest_log_issues("/repo/live_trace.md")
        self.assertEqual(len(issues), 1)
        shown = self.tree.show_log_issue(issues[0]["id"])
        self.assertEqual(shown["classification"], "typescript_import_export_error")
        self.assertEqual(shown["tool"], "typescript")
        self.assertEqual(shown["code"], "2305")
        self.assertIn("has no exported member 'useTodo'", shown["message"])

    def test_ingest_diagnostic_content_parses_tsc_style_output(self):
        issues = self.tree.ingest_diagnostic_content(
            'src/components/DemoTodoInput.tsx(2,10): error TS2305: Module "../context/DemoTodoContext" has no exported member "useTodo".',
            source_path="[run-check:typecheck]",
        )
        self.assertEqual(len(issues), 1)
        shown = self.tree.show_log_issue(issues[0]["id"])
        self.assertEqual(shown["classification"], "typescript_import_export_error")
        self.assertEqual(shown["tool"], "typescript")
        self.assertEqual(shown["code"], "TS2305")


class TestTreeCommands(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        src = os.path.join(self.tmpdir, "src")
        os.makedirs(src)
        Path(os.path.join(src, "main.py")).write_text("def main():\n    pass\n")
        self.tree = ContextTree(Path(self.tmpdir))
        self.tree.index_repo()
        self.parser = TreeCommandParser(self.tree)

    def test_ls(self):
        r = self.parser.parse_and_execute("ls /repo")
        self.assertTrue(r.ok)
        self.assertEqual(r.command_type, "read")
        self.assertFalse(r.needs_tool)

    def test_cat(self):
        r = self.parser.parse_and_execute("cat /repo/src/main.py")
        self.assertTrue(r.ok)
        self.assertIn("def main():", r.output)
        self.assertIn("1 | def main():", r.output)
        self.assertIn("2 |     pass", r.output)

    def test_cat_line_range(self):
        r = self.parser.parse_and_execute("cat /repo/src/main.py:1-1")
        self.assertTrue(r.ok)
        self.assertIn("def main():", r.output)
        self.assertNotIn("pass", r.output)
        self.assertIn("1 |", r.output)

    def test_read_line_range_command(self):
        r = self.parser.parse_and_execute("read-line-range /repo/src/main.py 1-2")
        self.assertTrue(r.ok)
        self.assertIn("1 | def main():", r.output)
        self.assertIn("2 |     pass", r.output)

    def test_symbols_command(self):
        r = self.parser.parse_and_execute("symbols /repo/src/main.py")
        self.assertTrue(r.ok)
        data = json.loads(r.output)
        self.assertTrue(any(item["name"] == "main" and item["kind"] == "function" for item in data))

    def test_find_symbol_command(self):
        r = self.parser.parse_and_execute("find-symbol /repo/src main")
        self.assertTrue(r.ok)
        data = json.loads(r.output)
        self.assertEqual(data[0]["path"], "/repo/src/main.py")
        self.assertEqual(data[0]["name"], "main")

    def test_repo_map_command_returns_structural_funnel(self):
        Path(os.path.join(self.tmpdir, "src", "utils.py")).write_text(
            "def helper(value: str) -> str:\n"
            "    return value\n",
            encoding="utf-8",
        )
        Path(os.path.join(self.tmpdir, "src", "main.py")).write_text(
            "from src.utils import helper\n\n"
            "class Planner:\n"
            "    def decide(self, value: str) -> str:\n"
            "        return helper(value)\n",
            encoding="utf-8",
        )
        self.tree.refresh_repo_file("src/utils.py")
        self.tree.refresh_repo_file("src/main.py")

        r = self.parser.parse_and_execute('repo-map /repo topic="helper" limit=5 symbols-per-file=4')

        self.assertTrue(r.ok)
        self.assertEqual(r.command_type, "read")
        self.assertFalse(r.needs_tool)
        data = json.loads(r.output)
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["summary"]["symbol_count"], 2)
        paths = [item["file_path"] for item in data["files"]]
        self.assertIn("src/main.py", paths)
        self.assertIn("src/utils.py", paths)
        self.assertTrue(any(item["next_action"]["type"] in {"read_symbol", "outline_file"} for item in data["drill_down"]))

    def test_replace_lines_accepts_separate_start_and_end_tokens(self):
        r = self.parser.parse_and_execute("replace-lines /repo/src/main.py 1 2 def main():\n    return 0")
        self.assertTrue(r.ok)
        self.assertTrue(r.needs_tool)
        self.assertEqual(r.tool_action["type"], "replace_lines")
        self.assertEqual(r.tool_action["path"], "src/main.py")
        self.assertEqual(r.tool_action["start_line"], 1)
        self.assertEqual(r.tool_action["end_line"], 2)

    def test_replace_lines_preserves_multiline_content(self):
        raw = "replace-lines /repo/src/main.py:1-2 def main():\n    return 0\n"
        r = self.parser.parse_and_execute(raw)
        self.assertTrue(r.ok)
        self.assertEqual(r.tool_action["content"], "def main():\n    return 0\n")

    def test_stat(self):
        r = self.parser.parse_and_execute("stat /repo/src/main.py")
        self.assertTrue(r.ok)
        data = json.loads(r.output)
        self.assertEqual(data["type"], "file")

    def test_find(self):
        r = self.parser.parse_and_execute("find /repo *.py")
        self.assertTrue(r.ok)
        self.assertIn("main.py", r.output)

    def test_write_needs_tool(self):
        r = self.parser.parse_and_execute("write /repo/src/new.py print('hi')")
        self.assertTrue(r.ok)
        self.assertTrue(r.needs_tool)
        self.assertEqual(r.tool_action["type"], "write_file")
        self.assertEqual(r.tool_action["path"], "src/new.py")

    def test_replace_lines_needs_tool(self):
        r = self.parser.parse_and_execute("replace-lines /repo/src/main.py:2-2 return 0")
        self.assertTrue(r.ok)
        self.assertTrue(r.needs_tool)
        self.assertEqual(r.tool_action["type"], "replace_lines")
        self.assertEqual(r.tool_action["path"], "src/main.py")
        self.assertEqual(r.tool_action["start_line"], 2)
        self.assertEqual(r.tool_action["end_line"], 2)
        self.assertEqual(r.tool_action["content"], "return 0")

    def test_replace_lines_allows_empty_inline_content(self):
        r = self.parser.parse_and_execute("replace-lines /repo/src/main.py:2-2")
        self.assertTrue(r.ok)
        self.assertEqual(r.tool_action["type"], "replace_lines")
        self.assertEqual(r.tool_action["content"], "")

    def test_replace_lines_with_separate_range_token(self):
        r = self.parser.parse_and_execute("replace-lines /repo/src/main.py 1-2 def main():\n    return 0")
        self.assertTrue(r.ok)
        self.assertEqual(r.tool_action["type"], "replace_lines")
        self.assertEqual(r.tool_action["start_line"], 1)
        self.assertEqual(r.tool_action["end_line"], 2)

    def test_reopen_issue_command_family(self):
        Path(self.tmpdir, "live_trace.md").write_text(textwrap.dedent("""\
            3:21:23 PM [vite] Internal server error: Failed to resolve import "uuid" from "src/hooks/useIndexedDbTodos.ts". Does the file exist?
              Plugin: vite:import-analysis
              File: /tmp/app/src/hooks/useIndexedDbTodos.ts:5:29
        """))
        self.tree.index_repo()
        self.parser.parse_and_execute("ingest-log /repo/live_trace.md")
        issue_id = self.tree.list_log_issues()[0]["id"]
        self.parser.parse_and_execute(f"resolve-run-issue {issue_id}")
        reopened = self.parser.parse_and_execute(f"reopen-run-issue {issue_id}")
        self.assertTrue(reopened.ok)
        shown = self.parser.parse_and_execute(f"show-run-issue {issue_id}")
        self.assertIn(f"Run Diagnostic {issue_id} [open]", shown.output)

    def test_patch_needs_tool(self):
        r = self.parser.parse_and_execute("patch /repo/src/main.py pass -> return 0")
        self.assertTrue(r.ok)
        self.assertTrue(r.needs_tool)
        self.assertEqual(r.tool_action["type"], "patch_file")
        self.assertEqual(r.tool_action["search"], "pass")
        self.assertEqual(r.tool_action["replace"], "return 0")

    def test_mutate_command_dispatches_explicit_mutation_action(self):
        r = self.parser.parse_and_execute(
            'mutate {"type":"replace_range","path":"src/main.py","start_line":1,"end_line":1,"new_text":"print(0)"}'
        )
        self.assertTrue(r.ok)
        self.assertTrue(r.needs_tool)
        self.assertEqual(r.tool_action["type"], "replace_range")
        self.assertEqual(r.tool_action["path"], "src/main.py")

    def test_discover_command_dispatches_discovery_action(self):
        r = self.parser.parse_and_execute(
            'discover {"type":"find_symbol_definitions","path":"src","symbol_name":"Planner","limit":5}'
        )
        self.assertTrue(r.ok)
        self.assertTrue(r.needs_tool)
        self.assertEqual(r.command_type, "read")
        self.assertEqual(r.tool_action["type"], "find_symbol_definitions")
        self.assertEqual(r.tool_action["symbol_name"], "Planner")

    def test_shell_needs_tool(self):
        r = self.parser.parse_and_execute("shell python -m pytest")
        self.assertTrue(r.ok)
        self.assertTrue(r.needs_tool)
        self.assertEqual(r.tool_action["type"], "run_shell")

    def test_git_status(self):
        r = self.parser.parse_and_execute("git status")
        self.assertTrue(r.ok)
        self.assertTrue(r.needs_tool)
        self.assertEqual(r.tool_action["type"], "git_status")

    def test_git_add(self):
        r = self.parser.parse_and_execute("git add src/main.py src/utils.py")
        self.assertTrue(r.ok)
        self.assertEqual(r.tool_action["paths"], ["src/main.py", "src/utils.py"])

    def test_git_commit(self):
        r = self.parser.parse_and_execute("git commit fix: refactor parser")
        self.assertTrue(r.ok)
        self.assertEqual(r.tool_action["message"], "fix: refactor parser")

    def test_fact(self):
        r = self.parser.parse_and_execute("fact 42/architecture/db postgres with pgvector")
        self.assertTrue(r.ok)
        self.assertEqual(r.command_type, "mutation")
        self.assertTrue(r.needs_tool)  # needs host persistence
        self.assertEqual(r.tool_action["type"], "set_fact")
        self.assertEqual(r.tool_action["key"], "db")
        self.assertEqual(r.tool_action["value"], "postgres with pgvector")
        # Also wrote to tree
        val = self.tree.get_fact("42", "architecture", "db")
        self.assertEqual(val, "postgres with pgvector")

    def test_expand_step(self):
        r = self.parser.parse_and_execute("expand 5")
        self.assertTrue(r.ok)
        self.assertEqual(r.tool_action["type"], "history_expand")
        self.assertEqual(r.tool_action["step"], 5)

    def test_expand_memory(self):
        r = self.parser.parse_and_execute("expand mem-abc-123")
        self.assertTrue(r.ok)
        self.assertEqual(r.tool_action["type"], "memory_expand")

    def test_batch(self):
        r1 = self.parser.parse_and_execute("batch start")
        self.assertEqual(r1.tool_action["type"], "begin_edit_batch")
        r2 = self.parser.parse_and_execute("batch end")
        self.assertEqual(r2.tool_action["type"], "end_edit_batch")

    def test_finish(self):
        r = self.parser.parse_and_execute("finish All changes applied and tested.")
        self.assertTrue(r.ok)
        self.assertEqual(r.tool_action["type"], "finish")
        self.assertEqual(r.tool_action["message"], "All changes applied and tested.")

    def test_skill(self):
        self.tree.register_skill("lint", "Run linter", handler=lambda path="": f"0 errors in {path}")
        r = self.parser.parse_and_execute("skill lint path=src/main.py")
        self.assertTrue(r.ok)
        self.assertEqual(r.command_type, "skill")
        self.assertIn("0 errors", r.output)

    def test_skill_list(self):
        self.tree.register_skill("lint", "Run linter")
        r = self.parser.parse_and_execute("skill")
        self.assertIn("lint", r.output)

    def test_unknown_command(self):
        r = self.parser.parse_and_execute("foobar blah")
        self.assertFalse(r.ok)
        self.assertEqual(r.command_type, "error")

    def test_empty_command(self):
        r = self.parser.parse_and_execute("")
        self.assertFalse(r.ok)

    def test_ingest_log_command_family(self):
        Path(self.tmpdir, "live_trace.md").write_text(textwrap.dedent("""\
            3:21:23 PM [vite] Internal server error: Failed to resolve import "uuid" from "src/hooks/useIndexedDbTodos.ts". Does the file exist?
              Plugin: vite:import-analysis
              File: /tmp/app/src/hooks/useIndexedDbTodos.ts:5:29
        """))
        self.tree.index_repo()
        ingest = self.parser.parse_and_execute("ingest-log /repo/live_trace.md")
        self.assertTrue(ingest.ok)
        self.assertEqual(ingest.command_type, "mutation")

        listed = self.parser.parse_and_execute("list-run-issues")
        self.assertTrue(listed.ok)
        issue_id = self.tree.list_log_issues()[0]["id"]
        self.assertIn(issue_id, listed.output)

        shown = self.parser.parse_and_execute(f"show-run-issue {issue_id}")
        self.assertTrue(shown.ok)
        self.assertIn("uuid", shown.output)
        self.assertIn("dependency_resolution_error", shown.output)

        resolved = self.parser.parse_and_execute(f"resolve-run-issue {issue_id}")
        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.command_type, "mutation")

    def test_read_diagnostics_command_family(self):
        Path(self.tmpdir, "diagnostics.json").write_text(textwrap.dedent("""\
            [{
              "resource": "/Users/artistefoundation/dev/test-app/src/components/DemoTodoInput.tsx",
              "owner": "typescript",
              "code": "2305",
              "severity": 8,
              "message": "Module '\\"../context/DemoTodoContext\\"' has no exported member 'useTodo'.",
              "source": "ts",
              "startLineNumber": 2,
              "startColumn": 10
            }]
        """))
        self.tree.index_repo()
        result = self.parser.parse_and_execute("read-diagnostics /repo/diagnostics.json")
        self.assertTrue(result.ok)
        self.assertEqual(result.command_type, "mutation")
        self.assertIn("ingested 1 issue", result.output)

    def test_ingest_runtime_stack_trace_creates_issue(self):
        Path(self.tmpdir, "live_trace.md").write_text(textwrap.dedent("""\
            ReferenceError: announcement is not defined
                at useTodoOperations (useTodoOperations.ts:322:5)
                at TodoProvider (TodoContext.tsx:53:7)
                at Object.react_stack_bottom_frame (react-dom_client.js?v=9b474997:18509:20)
                at renderWithHooks (react-dom_client.js?v=9b474997:5654:24)

            The above error occurred in the <TodoProvider> component.
        """))
        self.tree.index_repo()

        ingest = self.parser.parse_and_execute("ingest-log /repo/live_trace.md")

        self.assertTrue(ingest.ok)
        issue_id = self.tree.list_log_issues()[0]["id"]
        shown = self.parser.parse_and_execute(f"show-run-issue {issue_id}")
        self.assertTrue(shown.ok)
        self.assertIn("ReferenceError: announcement is not defined", shown.output)
        self.assertIn("javascript_runtime_reference_error", shown.output)
        self.assertIn("useTodoOperations.ts", shown.output)

    def test_run_check_command_dispatches(self):
        result = self.parser.parse_and_execute("run-check build")
        self.assertTrue(result.ok)
        self.assertTrue(result.needs_tool)
        self.assertEqual(result.tool_action["type"], "run_check")
        self.assertEqual(result.tool_action["kind"], "build")

    def test_run_route_check_command_dispatches(self):
        result = self.parser.parse_and_execute("run-route-check /todo base=http://127.0.0.1:4173")
        self.assertTrue(result.ok)
        self.assertTrue(result.needs_tool)
        self.assertEqual(result.tool_action["type"], "run_route_check")
        self.assertEqual(result.tool_action["target"], "/todo")
        self.assertEqual(result.tool_action["base_url"], "http://127.0.0.1:4173")


class TestMultiCommand(unittest.TestCase):
    def test_parse_multi(self):
        raw = """
        # List the repo
        ls /repo
        cat /repo/README.md
        # done
        """
        cmds = parse_multi_command(raw)
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0], "ls /repo")
        self.assertEqual(cmds[1], "cat /repo/README.md")

    def test_execute_multi(self):
        tmpdir = tempfile.mkdtemp()
        Path(os.path.join(tmpdir, "hello.txt")).write_text("hi")
        tree = ContextTree(Path(tmpdir))
        tree.index_repo()
        parser = TreeCommandParser(tree)

        results, annotations = execute_multi(parser, "ls /repo\ncat /repo/hello.txt")
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].ok)
        self.assertTrue(results[1].ok)
        self.assertIn("hi", results[1].output)
        self.assertEqual(len(annotations), 0)


class TestCollapseHeredocs(unittest.TestCase):
    """Tests for collapse_heredocs() — universal <<< >>> support."""

    def test_write_heredoc(self):
        lines = [
            "write /repo/file.md <<<",
            "# Title",
            "body text",
            ">>>",
        ]
        result = collapse_heredocs(lines)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].startswith("write /repo/file.md\n"))
        self.assertIn("# Title\nbody text", result[0])

    def test_strategy_heredoc(self):
        lines = [
            "s1: cat /repo/main.py",
            "s2: write /repo/Demo.tsx <<<",
            "import React from 'react';",
            "export default Demo;",
            ">>>",
        ]
        result = collapse_heredocs(lines)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], "s1: cat /repo/main.py")
        self.assertIn("import React", result[1])
        self.assertIn("export default Demo;", result[1])
        self.assertTrue(result[1].startswith("s2: write /repo/Demo.tsx\n"))

    def test_annotation_heredoc(self):
        lines = [
            ">>th: <<<",
            "I need to do multiple things:",
            "1. Read the file",
            "2. Modify it",
            ">>>",
        ]
        result = collapse_heredocs(lines)
        self.assertEqual(len(result), 1)
        self.assertIn(">>th:", result[0])
        self.assertIn("1. Read the file", result[0])

    def test_no_heredoc_passthrough(self):
        lines = ["ls /repo", "cat /repo/file.py"]
        result = collapse_heredocs(lines)
        self.assertEqual(result, lines)

    def test_multiple_heredocs(self):
        lines = [
            "write /repo/a.txt <<<",
            "content a",
            ">>>",
            "ls /repo",
            "write /repo/b.txt <<<",
            "content b",
            ">>>",
        ]
        result = collapse_heredocs(lines)
        self.assertEqual(len(result), 3)
        self.assertIn("content a", result[0])
        self.assertEqual(result[1], "ls /repo")
        self.assertIn("content b", result[2])

    def test_heredoc_preserves_indentation(self):
        lines = [
            "write /repo/file.py <<<",
            "def hello():",
            "    return 'world'",
            ">>>",
        ]
        result = collapse_heredocs(lines)
        self.assertIn("    return 'world'", result[0])


class TestHeredocInStrategy(unittest.TestCase):
    """Tests for heredoc working inside strategy blocks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        Path(self.tmpdir, "main.py").write_text("# Main\n")
        self.tree = ContextTree(Path(self.tmpdir))
        self.tree.index_repo()
        self.parser = TreeCommandParser(self.tree)

    def test_strategy_with_heredoc_write(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/main.py
            s2: write /repo/out.md <<<
            # Output
            Hello world
            >>>
        """)
        self.assertTrue(is_strategy(raw))
        plan = parse_strategy(raw)
        self.assertIsNotNone(plan)
        self.assertIn("s2", plan.steps)
        # The write command body should contain the heredoc content
        self.assertEqual(len(plan.steps["s2"].commands), 1)
        cmd = plan.steps["s2"].commands[0]
        self.assertIn("# Output", cmd)
        self.assertIn("Hello world", cmd)

    def test_parse_multi_command_heredoc_any_prefix(self):
        """parse_multi_command collapses heredoc on any line, not just write."""
        raw = ">>th: <<<\nthought line 1\nthought line 2\n>>>\nls /repo"
        cmds = parse_multi_command(raw)
        self.assertEqual(len(cmds), 2)
        self.assertIn("thought line 1", cmds[0])
        self.assertEqual(cmds[1], "ls /repo")

    def test_parse_multi_command_replace_lines_absorbs_multiline_inline_content(self):
        raw = "replace-lines /repo/src/main.py:10-10 setTodos(prev => { const next = [...prev, todo];\n  return next;\n})\ncat /repo/src/main.py:1-20"
        cmds = parse_multi_command(raw)
        self.assertEqual(len(cmds), 2)
        self.assertIn("return next;", cmds[0])
        self.assertTrue(cmds[1].startswith("cat /repo/src/main.py:1-20"))

    def test_parse_multi_command_write_absorbs_multiline_inline_content(self):
        raw = "write /repo/note.txt first line\nsecond line\nls /repo"
        cmds = parse_multi_command(raw)
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0], "write /repo/note.txt first line\nsecond line")
        self.assertEqual(cmds[1], "ls /repo")


# ── Strategy DAG tests ─────────────────────────────────────────────────

class TestStrategyParsing(unittest.TestCase):
    """Tests for parse_strategy / is_strategy."""

    def test_is_strategy_true(self):
        raw = "s1: cat /repo/main.py"
        self.assertTrue(is_strategy(raw))

    def test_is_strategy_false(self):
        self.assertFalse(is_strategy("cat /repo/main.py"))
        self.assertFalse(is_strategy("ls /repo"))
        self.assertFalse(is_strategy(""))

    def test_parse_single_step(self):
        plan = parse_strategy("s1: cat /repo/file.txt")
        self.assertIsNotNone(plan)
        self.assertIn("s1", plan.steps)
        self.assertEqual(plan.steps["s1"].commands, ["cat /repo/file.txt"])
        self.assertEqual(plan.execution_order, [["s1"]])

    def test_parse_parallel_group(self):
        raw = "s1: cat /repo/a.py, cat /repo/b.py"
        plan = parse_strategy(raw)
        self.assertEqual(len(plan.steps["s1"].commands), 2)

    def test_parse_dependency_arrow(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/a.py
            s1 -> s2: fact demo/result done
        """)
        plan = parse_strategy(raw)
        self.assertIn("s1", plan.steps)
        self.assertIn("s2", plan.steps)
        self.assertIn("s1", plan.steps["s2"].depends_on)
        # s1 in tier 0, s2 in tier 1
        self.assertEqual(plan.execution_order, [["s1"], ["s2"]])

    def test_parse_forward_targets(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/a.py -> s3
            s2: cat /repo/b.py -> s3
            s3: fact demo/merged combined
        """)
        plan = parse_strategy(raw)
        self.assertEqual(sorted(plan.steps["s3"].depends_on), ["s1", "s2"])
        # s1, s2 in first tier; s3 in second
        self.assertEqual(plan.execution_order[0], ["s1", "s2"])
        self.assertEqual(plan.execution_order[1], ["s3"])

    def test_parse_parallel_deps(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/a.py, cat /repo/b.py
            s2: grep /repo "class"
            s1, s2 -> s3: fact demo/all done
        """)
        plan = parse_strategy(raw)
        self.assertEqual(sorted(plan.steps["s3"].depends_on), ["s1", "s2"])

    def test_returns_none_for_non_strategy(self):
        self.assertIsNone(parse_strategy("cat /repo/main.py"))
        self.assertIsNone(parse_strategy(""))

    def test_comments_and_blanks_ignored(self):
        raw = textwrap.dedent("""\
            # This is a plan
            s1: cat /repo/a.py

            # Now combine
            s1 -> s2: fact demo/result done
        """)
        plan = parse_strategy(raw)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.steps), 2)


class TestStrategyExecution(unittest.TestCase):
    """Tests for execute_strategy / format_strategy_results."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create test files
        Path(self.tmpdir, "main.py").write_text("class Worker:\n    pass\n")
        Path(self.tmpdir, "helper.py").write_text("def help():\n    return 1\n")
        self.tree = ContextTree(Path(self.tmpdir))
        self.tree.index_repo()
        self.parser = TreeCommandParser(self.tree)

    def test_single_step_execution(self):
        raw = "s1: cat /repo/main.py"
        results = execute_strategy(self.parser, raw)
        self.assertIn("s1", results)
        self.assertTrue(results["s1"][0].ok)
        self.assertIn("class Worker", results["s1"][0].output)

    def test_parallel_group_execution(self):
        raw = "s1: cat /repo/main.py, cat /repo/helper.py"
        results = execute_strategy(self.parser, raw)
        self.assertEqual(len(results["s1"]), 2)
        self.assertTrue(all(r.ok for r in results["s1"]))
        combined = " ".join(r.output for r in results["s1"])
        self.assertIn("Worker", combined)
        self.assertIn("help", combined)

    def test_dependency_chain_execution(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/main.py
            s1 -> s2: ls /repo
        """)
        results = execute_strategy(self.parser, raw)
        self.assertIn("s1", results)
        self.assertIn("s2", results)
        self.assertTrue(results["s1"][0].ok)
        self.assertTrue(results["s2"][0].ok)

    def test_placeholder_substitution(self):
        # s1 reads a file, s2 writes a fact including {s1} output
        raw = textwrap.dedent("""\
            s1: cat /repo/main.py
            s1 -> s2: fact test/arch/placeholder {s1}
        """)
        results = execute_strategy(self.parser, raw)
        # s2's command should have had {s1} replaced with the file content
        s2_result = results["s2"][0]
        self.assertTrue(s2_result.ok)
        # The fact content should include the file content
        node = self.tree.cat("/facts/test/arch/placeholder")
        self.assertIn("class Worker", node)

    def test_placeholder_transform_chain(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/main.py
            s1 -> s2: fact test/arch/placeholder {s1.stdout.split('\\n').filter(line => !line.includes("pass")).join('\\n').trim()}
        """)
        results = execute_strategy(self.parser, raw)
        self.assertTrue(results["s2"][0].ok)
        node = self.tree.cat("/facts/test/arch/placeholder")
        self.assertIn("class Worker:", node)
        self.assertNotIn("pass", node)

    def test_placeholder_split_index_transform_chain(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/main.py
            s1 -> s2: fact test/arch/placeholder_first_line {s1.stdout.split('\\n')[0].trim()}
        """)
        results = execute_strategy(self.parser, raw)
        self.assertTrue(results["s2"][0].ok)
        node = self.tree.cat("/facts/test/arch/placeholder_first_line")
        self.assertIn("class Worker", node)

    def test_placeholder_regex_match_capture_transform_chain(self):
        raw = textwrap.dedent(r"""\
            s1: cat /repo/main.py
            s1 -> s2: fact test/arch/placeholder_class {s1.stdout.match(/class (\w+)/)[1]}
            s1 -> s3: fact test/arch/placeholder_line_number {s1.stdout.match(/(\d+) \| class/)[1]}
        """)
        results = execute_strategy(self.parser, raw)
        self.assertTrue(results["s2"][0].ok)
        self.assertTrue(results["s3"][0].ok)
        self.assertIn("Worker", self.tree.cat("/facts/test/arch/placeholder_class"))
        self.assertIn("1", self.tree.cat("/facts/test/arch/placeholder_line_number"))

    def test_placeholder_transform_error_is_explicit(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/main.py
            s1 -> s2: fact test/arch/placeholder {s1.stdout.map(line => line)}
        """)
        results = execute_strategy(self.parser, raw)
        self.assertFalse(results["s2"][0].ok)
        self.assertIn("placeholder error", results["s2"][0].output)

    def test_plain_braces_in_heredoc_do_not_trigger_placeholder_resolution(self):
        raw = textwrap.dedent("""\
            s1: write /repo/example.tsx <<<
            export const Demo = () => {
              return <div>{/* Speaker/Camera cutout */}</div>;
            };
            >>>
        """)
        results = execute_strategy(self.parser, raw)
        self.assertTrue(results["s1"][0].ok)
        self.assertNotIn("placeholder error", results["s1"][0].output)

    def test_css_braces_in_heredoc_do_not_trigger_placeholder_resolution(self):
        raw = textwrap.dedent("""\
            s1: write /repo/styles.css <<<
            .demo-card {
              color: red;
              background: linear-gradient(to bottom, #111, #222);
            }
            >>>
        """)
        results = execute_strategy(self.parser, raw)
        self.assertTrue(results["s1"][0].ok)
        self.assertNotIn("placeholder error", results["s1"][0].output)

    def test_three_tier_pipeline(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/main.py, cat /repo/helper.py
            s1 -> s2: ls /repo
            s2 -> s3: stat /repo/main.py
        """)
        results = execute_strategy(self.parser, raw)
        self.assertEqual(len(results), 3)
        for label in ("s1", "s2", "s3"):
            self.assertIn(label, results)
            self.assertTrue(all(r.ok for r in results[label]))

    def test_diamond_dag(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/main.py -> s3
            s2: cat /repo/helper.py -> s3
            s3: ls /repo
        """)
        results = execute_strategy(self.parser, raw)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.ok for r in results["s3"]))

    def test_patch_arrow_inside_strategy_is_not_parsed_as_dependency_target(self):
        raw = "s1: patch /repo/main.py pass -> return 0"
        plan = parse_strategy(raw)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.steps["s1"].commands, ["patch /repo/main.py pass -> return 0"])
        self.assertEqual(plan.steps["s1"].targets, [])

        results = execute_strategy(self.parser, raw)
        self.assertTrue(results["s1"][0].ok)
        self.assertEqual(results["s1"][0].tool_action["type"], "patch_file")
        self.assertEqual(results["s1"][0].tool_action["search"], "pass")
        self.assertEqual(results["s1"][0].tool_action["replace"], "return 0")

    def test_patch_replacement_starting_with_s_is_not_strategy_target(self):
        raw = "s1: patch /repo/main.py pass -> shifted"
        plan = parse_strategy(raw)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.steps["s1"].commands, ["patch /repo/main.py pass -> shifted"])
        self.assertEqual(plan.steps["s1"].targets, [])

        results = execute_strategy(self.parser, raw)
        self.assertTrue(results["s1"][0].ok)
        self.assertEqual(results["s1"][0].tool_action["replace"], "shifted")

    def test_strategy_dependency_targets_must_be_strategy_labels(self):
        raw = "s1: cat /repo/main.py -> s2\ns2: ls /repo"
        plan = parse_strategy(raw)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.steps["s1"].targets, ["s2"])
        self.assertIn("s1", plan.steps["s2"].depends_on)

    def test_format_strategy_results(self):
        raw = "s1: cat /repo/main.py, ls /repo"
        results = execute_strategy(self.parser, raw)
        formatted = format_strategy_results(results)
        self.assertIn("[✓ s1 READ]", formatted)
        self.assertIn("class Worker", formatted)

    def test_format_strategy_results_truncates_extreme_output(self):
        results = {
            "s1": [
                CommandResult(
                    ok=True,
                    output="A" * 2000,
                    command_type="read",
                )
            ]
        }
        formatted = format_strategy_results(results, max_output_chars=120)
        self.assertIn("[✓ s1 READ]", formatted)
        self.assertIn("chars truncated", formatted)

    def test_invalid_strategy(self):
        results = execute_strategy(self.parser, "not a strategy")
        self.assertIn("error", results)
        self.assertFalse(results["error"][0].ok)

    def test_strategy_summary(self):
        raw = textwrap.dedent("""\
            s1: cat /repo/main.py -> s2
            s2: ls /repo
        """)
        plan = parse_strategy(raw)
        summary = plan.summary()
        self.assertIn("tier 0", summary)
        self.assertIn("tier 1", summary)
        self.assertIn("s1", summary)
        self.assertIn("s2", summary)


if __name__ == "__main__":
    unittest.main()
