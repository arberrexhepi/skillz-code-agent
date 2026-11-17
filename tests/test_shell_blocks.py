from __future__ import annotations

from pathlib import Path
import os
import shlex
import sys
import tempfile
import unittest

from context_tree import ContextTree
from tree_commands import TreeCommandParser, is_strategy, parse_multi_command, parse_strategy
from tree_loop import TreeLoop, extract_commands
from tests.test_tree_loop_messages import RecordingMessageModel


class ShellBlockParserTests(unittest.TestCase):
    def test_multiline_tsx_eval_stays_one_command_in_extraction_and_dispatch(self):
        script = "npx tsx -e '\nconst value = { label: \"user-smoke-case\" };\nconsole.log(value);\n'"
        command = "shell " + script
        extracted = extract_commands(">>th: Run the finite smoke check\n>>pl: Verify and clean up\n" + command + "\nfinish Verified")
        commands = parse_multi_command(extracted)
        self.assertEqual(commands[-2:], [command, "finish Verified"])
        with tempfile.TemporaryDirectory() as tmp:
            result = TreeCommandParser(ContextTree(Path(tmp))).parse_and_execute(commands[-2])
        self.assertEqual(result.tool_action, {"type": "run_shell", "command": script})

    def test_native_heredoc_body_is_opaque_even_when_it_looks_like_agent_commands(self):
        command = "shell cat <<'BODY'\ns1: write outside.txt should-not-run\n>>th: source text\nfinish fake\n>>>\n```\nunmatched ' quote\nBODY"
        self.assertEqual(extract_commands(command + "\nfinish Real answer"), command + "\nfinish Real answer")
        self.assertFalse(is_strategy(command))
        self.assertEqual(parse_multi_command(command + "\nfinish Real answer"), [command, "finish Real answer"])

    def test_supported_model_prefixes_do_not_expose_shell_source_as_commands(self):
        command = "shell cat <<'BODY'\ns99: finish body-only\nBODY"
        for prefix in ("Command: ", "Action: ", "- ", "> - "):
            with self.subTest(prefix=prefix):
                self.assertEqual(extract_commands(prefix + command + "\nfinish Done"), command + "\nfinish Done")

    def test_tab_stripping_and_multiple_shell_heredocs_keep_their_delimiters(self):
        command = "shell cat <<'FIRST' <<-SECOND\nfirst body\nFIRST\n\tsecond body\n\tSECOND"
        self.assertEqual(parse_multi_command(command + "\ncat /repo/result.txt"), [command, "cat /repo/result.txt"])

    def test_quoted_and_arithmetic_shift_operators_do_not_open_heredocs(self):
        for command in ('shell printf "%s" "<<NO_BODY"', "shell printf '%s' $((1 << 2))", "shell echo ready # <<NOT_A_DELIMITER"):
            with self.subTest(command=command):
                self.assertEqual(parse_multi_command(command + "\nfinish done"), [command, "finish done"])

    def test_shell_continuations_and_pipelines_remain_inside_one_dispatch(self):
        for command in ("shell printf '%s' \\\n  continued", "shell printf '%s' test |\n  cat", "shell printf first &&\n  printf second"):
            with self.subTest(command=command):
                self.assertEqual(parse_multi_command(command + "\nfinish done"), [command, "finish done"])

    def test_unterminated_shell_block_rejects_the_whole_command_batch(self):
        for command in ("shell cat <<'MISSING'\nfinish not-a-command", "shell npx tsx -e '\nconst value = 1;"):
            with self.subTest(command=command):
                parsed = parse_multi_command(extract_commands("write first.txt must-not-land\n" + command))
                self.assertEqual(len(parsed), 1)
                self.assertTrue(parsed[0].startswith("__command_parse_error__ "))

    def test_strategy_labels_inside_shell_source_do_not_create_steps(self):
        raw = "s1: shell cat <<'SOURCE'\ns99: finish body-only\nconsole.log('literal, -> s77');\nSOURCE\ns1 -> s2: cat /repo/result.txt"
        plan = parse_strategy(extract_commands(raw))
        self.assertEqual(set(plan.steps), {"s1", "s2"})
        self.assertEqual(plan.execution_order, [["s1"], ["s2"]])
        self.assertEqual(len(plan.steps["s1"].commands), 1)
        self.assertIn("s99: finish body-only", plan.steps["s1"].commands[0])

    def test_explicit_shell_script_block_uses_the_existing_multiline_delimiters(self):
        command = "shell <<<\nprintf first\nprintf second\n>>>"
        self.assertEqual(parse_multi_command(extract_commands(command)), ["shell\nprintf first\nprintf second"])


class ShellBlockExecutionTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Native Windows shells have different quoting; artifact containers use POSIX shells")
    def test_multiline_quoted_code_executes_once_and_the_next_read_sees_its_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = 'from pathlib import Path\nPath("result.txt").write_text("one\\ntwo\\n")\nprint("script finished")'
            command = f"shell {shlex.quote(sys.executable)} -c '{code}'"
            model = RecordingMessageModel([command + "\ncat /repo/result.txt\nfinish Verified"])
            loop = TreeLoop(model=model, workspace_root=root, max_turns=1, verbose=False, allow_shell=True)
            result = loop.run("Run a finite multiline script")
            self.assertTrue(result.finished)
            self.assertEqual(len(result.turns[0].results), 3)
            self.assertIn("script finished", result.turns[0].results[0].output)
            self.assertEqual(result.turns[0].results[0].tool_action["_changed_paths"], ["result.txt"])
            self.assertIn("two", result.turns[0].results[1].output)

    @unittest.skipIf(os.name == "nt", "Native Windows shells do not implement POSIX heredocs")
    def test_native_heredoc_runs_a_finite_sqlite_smoke_check_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = f"shell {shlex.quote(sys.executable)} <<'VERIFY'\n" + "\n".join([
                "import sqlite3",
                "from pathlib import Path",
                'db = Path("smoke.sqlite")',
                "with sqlite3.connect(db) as connection:",
                '    connection.execute("create table scene (id text primary key, title text)")',
                '    connection.execute("insert into scene values (?, ?)", ("user-smoke-1", "first"))',
                '    connection.execute("update scene set title = ?", ("second",))',
                "connection.close()",
                "with sqlite3.connect(db) as connection:",
                '    assert connection.execute("select title from scene").fetchone()[0] == "second"',
                '    connection.execute("delete from scene")',
                "connection.close()",
                "db.unlink()",
                'print("SKILLZ_ARTIFACT_READY")',
                "VERIFY",
            ])
            loop = TreeLoop(model=RecordingMessageModel([command + "\nfinish Smoke check passed"]), workspace_root=root,
                            max_turns=1, verbose=False, allow_shell=True)
            result = loop.run("Verify SQLite using only a temporary test database")
            self.assertTrue(result.finished)
            self.assertEqual(len(result.turns[0].results), 2)
            self.assertIn("SKILLZ_ARTIFACT_READY", result.turns[0].results[0].output)
            self.assertFalse((root / "smoke.sqlite").exists())

    def test_unterminated_script_never_executes_a_preceding_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loop = TreeLoop(model=RecordingMessageModel(["write first.txt must-not-land\nshell cat <<'MISSING'\nfinish fake"]),
                            workspace_root=root, max_turns=1, verbose=False, allow_shell=True)
            result = loop.run("Reject incomplete command blocks")
            self.assertFalse(result.finished)
            self.assertFalse((root / "first.txt").exists())
            self.assertEqual(result.turns[0].results[0].command_type, "error")

    def test_multiline_shell_keeps_shell_access_and_git_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop = TreeLoop(model=RecordingMessageModel([]), workspace_root=Path(tmp), verbose=False, allow_shell=False)
            self.assertIn("disabled", loop._exec_run_shell({"command": "echo first\necho second"}))
            loop.allow_shell = True
            self.assertIn("direct git commands are disabled", loop._exec_run_shell({"command": "echo first\ngit reset --hard"}))


if __name__ == "__main__":
    unittest.main()
