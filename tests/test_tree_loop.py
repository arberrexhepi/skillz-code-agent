"""Tests for tree_loop.py — the LLM agent loop over ContextTree."""

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
from typing import List

from tree_loop import (
    LoopResult,
    RecentRead,
    TreeLoop,
    Turn,
    _build_user_prompt,
    _looks_like_command,
    extract_commands,
)


class FakeModel:
    """Fake model that returns pre-scripted responses in order."""

    def __init__(self, responses: List[str]) -> None:
        self.responses = list(responses)
        self.calls: List[dict] = []
        self._idx = 0

    def complete(self, system: str, prompt: str) -> str:
        self.calls.append({"system": system, "prompt": prompt})
        if self._idx >= len(self.responses):
            return "finish Done — no more scripted responses"
        resp = self.responses[self._idx]
        self._idx += 1
        return resp


class InterruptingModel:
    """Fake model that simulates an operator interrupt during completion."""

    def complete(self, system: str, prompt: str) -> str:
        raise KeyboardInterrupt()


class TestExtractCommands(unittest.TestCase):
    def test_thought_then_commands(self):
        raw = textwrap.dedent("""\
            I need to look at the project structure first.
            Let me check what files exist.

            ls /repo
            cat /repo/main.py:1-20
        """)
        result = extract_commands(raw)
        self.assertIn("ls /repo", result)
        self.assertIn("cat /repo/main.py:1-20", result)

    def test_pure_commands(self):
        raw = "ls /repo\ncat /repo/README.md"
        result = extract_commands(raw)
        self.assertIn("ls /repo", result)

    def test_strategy(self):
        raw = textwrap.dedent("""\
            Let me gather context in parallel.

            s1: cat /repo/main.py, cat /repo/helper.py
            s1 -> s2: fact demo/arch/overview combined
        """)
        result = extract_commands(raw)
        self.assertIn("s1:", result)
        self.assertIn("s2:", result)

    def test_no_commands(self):
        raw = "I'm just thinking about this problem. No actions needed yet."
        result = extract_commands(raw)
        self.assertEqual(result, "")

    def test_finish_command(self):
        raw = "All done.\n\nfinish Task complete"
        result = extract_commands(raw)
        self.assertIn("finish Task complete", result)

    def test_skips_prose_and_code_fences(self):
        raw = textwrap.dedent("""\
            >>th: I need to patch the type definition carefully.
            Here's the plan:
            1. Inspect the file.
            2. Apply the patch.

            ```text
            patch /repo/src/types/todo.ts "old" -> "new"
            ```
        """)
        result = extract_commands(raw)
        self.assertEqual(
            result,
            '>>th: I need to patch the type definition carefully.\npatch /repo/src/types/todo.ts "old" -> "new"',
        )

    def test_annotation_only_turn_is_preserved(self):
        raw = textwrap.dedent("""\
            >>th: I need one more clue before acting.
            >>q: Which todo shape should be canonical?
        """)
        result = extract_commands(raw)
        self.assertEqual(
            result,
            ">>th: I need one more clue before acting.\n>>q: Which todo shape should be canonical?",
        )


class TestLooksLikeCommand(unittest.TestCase):
    def test_tree_commands(self):
        self.assertTrue(_looks_like_command("ls /repo"))
        self.assertTrue(_looks_like_command("cat /repo/main.py"))
        self.assertTrue(_looks_like_command("read-line-range /repo/main.py 1-10"))
        self.assertTrue(_looks_like_command("grep /facts \"pattern\""))
        self.assertTrue(_looks_like_command("reopen-issue issue-001"))
        self.assertTrue(_looks_like_command("finish done"))
        self.assertTrue(_looks_like_command("fact demo/arch/key value"))
        self.assertTrue(_looks_like_command("skill count_py"))
        self.assertTrue(_looks_like_command("write /repo/foo.py content"))
        self.assertTrue(_looks_like_command("replace-lines /repo/foo.py:10-20 replacement"))

    def test_non_commands(self):
        self.assertFalse(_looks_like_command(""))
        self.assertFalse(_looks_like_command("I need to think about this"))
        self.assertFalse(_looks_like_command("The main.py file contains"))

    def test_strategy_labels(self):
        self.assertTrue(_looks_like_command("s1: cat /repo/main.py"))
        self.assertTrue(_looks_like_command("s2: ls /repo"))


class TestStrategyParserRegression(unittest.TestCase):
    def test_parse_strategy_preserves_inline_function_call_commas(self):
        from tree_commands import parse_strategy

        plan = parse_strategy(
            "s1: cat /repo/src/app.tsx, replace-lines /repo/src/app.tsx:10-10 addTodo(newTodoTask, Priority.Medium, Category.Work)"
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            plan.steps["s1"].commands,
            [
                "cat /repo/src/app.tsx",
                "replace-lines /repo/src/app.tsx:10-10 addTodo(newTodoTask, Priority.Medium, Category.Work)",
            ],
        )

    def test_parse_strategy_preserves_object_literal_and_string_commas(self):
        from tree_commands import parse_strategy

        plan = parse_strategy(
            's1: patch /repo/src/app.tsx { todo: true, priority: "medium, later" } -> { todo: false, priority: "done, now" }, cat /repo/src/app.tsx'
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            plan.steps["s1"].commands,
            [
                'patch /repo/src/app.tsx { todo: true, priority: "medium, later" } -> { todo: false, priority: "done, now" }, cat /repo/src/app.tsx',
            ],
        )


class TestTurn(unittest.TestCase):
    def test_thought_from_annotations(self):
        from tree_commands import Annotation, CommandResult
        turn = Turn(
            turn_number=1,
            raw_output=">>th: I should check the repo\n>>th: Let me look\nls /repo",
            commands_issued=[">>th: I should check the repo", ">>th: Let me look", "ls /repo"],
            results=[
                CommandResult(ok=True, output=">>th: I should check the repo", command_type="annotation"),
                CommandResult(ok=True, output=">>th: Let me look", command_type="annotation"),
                CommandResult(ok=True, output="file1.py\nfile2.py", command_type="read"),
            ],
            annotations=[
                Annotation(tag="th", tag_name="thought", content="I should check the repo"),
                Annotation(tag="th", tag_name="thought", content="Let me look"),
            ],
        )
        self.assertEqual(turn.thought, "I should check the repo\nLet me look")

    def test_delegation_from_annotations(self):
        from tree_commands import Annotation, CommandResult
        turn = Turn(
            turn_number=1,
            raw_output=">>dg: if no tests found, create test file\nls /repo",
            commands_issued=[">>dg: if no tests found, create test file", "ls /repo"],
            results=[
                CommandResult(ok=True, output=">>dg: if no tests found, create test file", command_type="annotation"),
                CommandResult(ok=True, output="main.py", command_type="read"),
            ],
            annotations=[
                Annotation(tag="dg", tag_name="delegation", content="if no tests found, create test file"),
            ],
        )
        self.assertEqual(turn.delegations, ["if no tests found, create test file"])

    def test_plan_from_annotations(self):
        from tree_commands import Annotation, CommandResult
        turn = Turn(
            turn_number=1,
            raw_output=">>pl: Read main files then record findings\nls /repo",
            commands_issued=["ls /repo"],
            results=[CommandResult(ok=True, output="main.py", command_type="read")],
            annotations=[
                Annotation(tag="pl", tag_name="plan", content="Read main files then record findings"),
            ],
        )
        self.assertEqual(turn.plan, "Read main files then record findings")

    def test_has_finish(self):
        from tree_commands import CommandResult
        turn = Turn(
            turn_number=1,
            raw_output="finish All done",
            commands_issued=["finish All done"],
            results=[CommandResult(ok=True, output="All done", command_type="finish")],
        )
        self.assertTrue(turn.has_finish)

    def test_compact_format(self):
        from tree_commands import CommandResult
        turn = Turn(
            turn_number=3,
            raw_output="Checking.\n\nls /repo",
            commands_issued=["ls /repo"],
            results=[CommandResult(ok=True, output="main.py", command_type="read")],
            elapsed_s=1.5,
        )
        compact = turn.compact()
        self.assertIn("Turn 3", compact)
        self.assertIn("READ", compact)
        self.assertIn("main.py", compact)

    def test_compact_thoughts_trace(self):
        """Thoughts and plans appear in a [THOUGHTS] section in compact."""
        from tree_commands import Annotation, CommandResult
        turn = Turn(
            turn_number=2,
            raw_output=">>th: need to check\n>>pl: read then write\nls /repo",
            commands_issued=["ls /repo"],
            results=[CommandResult(ok=True, output="main.py", command_type="read")],
            annotations=[
                Annotation(tag="th", tag_name="thought", content="need to check"),
                Annotation(tag="pl", tag_name="plan", content="read then write"),
                Annotation(tag="dg", tag_name="delegation", content="if missing create it"),
            ],
            elapsed_s=1.0,
        )
        compact = turn.compact()
        self.assertIn("[THOUGHTS]", compact)
        self.assertIn(">>th: need to check", compact)
        self.assertIn(">>pl: read then write", compact)
        # delegation is NOT in [THOUGHTS], shown separately
        self.assertIn(">>dg: if missing create it", compact)
        # Thoughts section comes before command results
        thoughts_pos = compact.index("[THOUGHTS]")
        read_pos = compact.index("[✓ READ]")
        self.assertLess(thoughts_pos, read_pos)

    def test_compact_truncates_extreme_patch_command_preview(self):
        from tree_commands import CommandResult
        huge_patch = "patch /repo/src/file.ts " + ("A" * 500) + " -> " + ("B" * 500)
        turn = Turn(
            turn_number=1,
            raw_output=huge_patch,
            commands_issued=[huge_patch],
            results=[CommandResult(ok=True, output="patched src/file.ts", command_type="write", needs_tool=True)],
        )
        compact = turn.compact()
        self.assertIn("[✓ TOOL] patch /repo/src/file.ts", compact)
        self.assertIn("chars truncated", compact)

    def test_compact_preserves_collapsed_heredoc_command_shape(self):
        from tree_commands import CommandResult
        command = "replace-lines /repo/src/file.ts:1-1 replacement text"
        turn = Turn(
            turn_number=1,
            raw_output="replace-lines /repo/src/file.ts:1-1 <<<\nreplacement text\n>>>",
            commands_issued=[command],
            results=[CommandResult(ok=True, output="replaced lines 1-1 in src/file.ts with 1 line(s)", command_type="write", needs_tool=True)],
        )
        compact = turn.compact()
        self.assertIn(command, compact)
        self.assertNotIn("[✓ TOOL] replacement text", compact)

    def test_compact_masks_large_replace_lines_content_in_history(self):
        from tree_commands import CommandResult
        command = "replace-lines /repo/src/file.ts:1-20 " + ("A" * 500)
        turn = Turn(
            turn_number=1,
            raw_output=command,
            commands_issued=[command],
            results=[CommandResult(ok=True, output="replaced lines 1-20 in src/file.ts with 10 line(s)", command_type="write", needs_tool=True)],
        )
        compact = turn.compact()
        self.assertIn("replace-lines /repo/src/file.ts:1-20 [content omitted in history;", compact)
        self.assertNotIn("AAAAA", compact)


class TestBuildUserPrompt(unittest.TestCase):
    def test_basic_prompt(self):
        prompt = _build_user_prompt(
            task="Find TODOs",
            os_state="/repo: main.py, helper.py",
            history=[],
        )
        self.assertIn("TASK", prompt)
        self.assertIn("Find TODOs", prompt)
        self.assertIn("PLAYGROUND OS STATE", prompt)
        self.assertIn("main.py", prompt)
        self.assertIn("YOUR TURN", prompt)

    def test_prompt_with_history(self):
        from tree_commands import CommandResult
        turns = [
            Turn(
                turn_number=1,
                raw_output="Looking.\n\nls /repo",
                commands_issued=["ls /repo"],
                results=[CommandResult(ok=True, output="main.py", command_type="read")],
                elapsed_s=1.0,
            )
        ]
        prompt = _build_user_prompt(
            task="Find TODOs",
            os_state="<state>",
            history=turns,
        )
        self.assertIn("HISTORY", prompt)
        self.assertIn("Turn 1", prompt)

    def test_prompt_with_steering(self):
        prompt = _build_user_prompt(
            task="Task",
            os_state="<state>",
            history=[],
            steering="Always check tests first",
        )
        self.assertIn("OPERATOR STEERING", prompt)
        self.assertIn("Always check tests", prompt)

    def test_prompt_with_recent_file_context_commands(self):
        prompt = _build_user_prompt(
            task="Task",
            os_state="<state>",
            history=[],
            recent_reads=[RecentRead(command="cat /repo/src/main.py:1-10", output="1 | def main():")],
        )
        self.assertIn("RECENT FILE-CONTEXT COMMANDS", prompt)
        self.assertIn("cat /repo/src/main.py:1-10", prompt)
        self.assertIn("def main()", prompt)


class TestTreeLoop(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        Path(self.tmpdir, "main.py").write_text("# Main file\nclass Worker:\n    pass\n")
        Path(self.tmpdir, "helper.py").write_text("def help():\n    return 1\n")
        Path(self.tmpdir, "README.md").write_text("# Project\nA test project.\n")

    def test_single_turn_finish(self):
        model = FakeModel(["Let me finish.\n\nfinish Task complete — nothing to do"])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
        )
        result = loop.run("Describe the project")
        self.assertTrue(result.finished)
        self.assertIn("complete", result.finish_message.lower())
        self.assertEqual(len(result.turns), 1)

    def test_read_then_finish(self):
        model = FakeModel([
            "Let me check the project structure.\n\nls /repo",
            "I see the files. Let me read main.\n\ncat /repo/main.py",
            "It's a simple project.\n\nfinish Project has Worker class in main.py",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=10,
            verbose=False,
        )
        result = loop.run("What is this project?")
        self.assertTrue(result.finished)
        self.assertEqual(len(result.turns), 3)
        self.assertGreater(result.reads, 0)

    def test_strategy_execution(self):
        model = FakeModel([
            "Let me gather context.\n\ns1: cat /repo/main.py, cat /repo/helper.py\ns1 -> s2: ls /repo",
            "Got it.\n\nfinish Project has Worker + helper",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
        )
        result = loop.run("Analyze the project")
        self.assertTrue(result.finished)
        self.assertEqual(len(result.turns), 2)
        self.assertIn("[s1] cat /repo/main.py", result.turns[0].commands_issued[0])

    def test_max_turns_reached(self):
        model = FakeModel([
            "Still looking.\n\nls /repo",
            "More exploring.\n\ncat /repo/main.py",
            "Continuing.\n\ncat /repo/helper.py",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=3,
            verbose=False,
        )
        result = loop.run("Infinite task")
        self.assertFalse(result.finished)
        self.assertEqual(len(result.turns), 3)

    def test_history_fed_back(self):
        model = FakeModel([
            "Checking.\n\nls /repo",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
        )
        loop.run("Test")

        # Second call should have history in prompt
        self.assertEqual(len(model.calls), 2)
        second_prompt = model.calls[1]["prompt"]
        self.assertIn("HISTORY", second_prompt)
        self.assertIn("Turn 1", second_prompt)

    def test_system_prompt_has_grammar(self):
        model = FakeModel(["finish done"])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
        )
        loop.run("Test")
        system = model.calls[0]["system"]
        self.assertIn("ls", system)
        self.assertIn("cat", system)
        self.assertIn("read-line-range", system)
        self.assertIn("symbols", system)
        self.assertIn("find-symbol", system)
        self.assertIn("replace-lines", system)
        self.assertIn("STRATEGIES", system)
        self.assertIn("playground OS", system)
        self.assertIn("Start every turn with >>th: and >>pl:", system)
        self.assertIn("For localized file repairs, prefer this workflow", system)
        self.assertIn("use `repo-map`, `symbols`, or `find-symbol` first", system)
        self.assertIn("Strategy placeholders like `{s1}`", system)
        self.assertIn(".split('\n')[0]", system)
        self.assertIn(".match(/pattern/)[1]", system)
        self.assertIn("Literal braces in code are safe", system)
        self.assertIn("Use inline `replace-lines` only for short single-line replacements", system)
        self.assertIn("Use `batch start` before a cluster of related file fixes", system)
        self.assertIn("maintain composure", system)
        self.assertIn("Skills are part of your capability surface", system)
        self.assertIn("Use `skill` with no arguments to discover what skills are available", system)
        self.assertIn("prefer an explicit `skill` discovery", system)
        self.assertIn("When the user explicitly mentions a skill, playbook, style, or workflow", system)

    def test_fact_persists_across_turns(self):
        fact_records = []

        def persist_fact(result):
            action = result.tool_action or {}
            if action.get("type") == "set_fact":
                fact_records.append(
                    SimpleNamespace(
                        issue_id=action["issue_id"],
                        fact_type=action["fact_type"],
                        key=action["key"],
                        value=action["value"],
                        updated_step=1,
                        updated_run_id=1,
                    )
                )

        model = FakeModel([
            "Recording a finding.\n\nfact test/arch/entry main.py has Worker",
            "Let me verify.\n\ncat /facts/test/arch/entry",
            "finish Verified",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
            get_fact_records=lambda: fact_records,
            tool_dispatcher=persist_fact,
        )
        result = loop.run("Analyze and record")
        self.assertTrue(result.finished)
        # Turn 2 should have read back the fact
        turn2 = result.turns[1]
        self.assertTrue(any("Worker" in r.output for r in turn2.results))

    def test_loop_result_summary(self):
        model = FakeModel([
            "Looking.\n\nls /repo\ncat /repo/main.py",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
        )
        result = loop.run("Test")
        summary = result.summary()
        self.assertIn("FINISHED", summary)
        self.assertIn("2 turns", summary)

    def test_no_commands_turn(self):
        """Model produces only thinking, no commands — loop continues."""
        model = FakeModel([
            "Hmm, I need to think about this more carefully. The project seems complex.",
            "OK now I'll look.\n\nls /repo",
            "finish Done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
        )
        result = loop.run("Analyze")
        self.assertTrue(result.finished)
        self.assertEqual(len(model.calls), 3)
        # Commandless model output is retried but omitted from executable history.
        self.assertEqual(len(result.turns), 2)
        self.assertEqual(result.turns[0].commands_issued, ["ls /repo"])

    def test_tool_dispatcher_called(self):
        from tree_commands import CommandResult

        dispatched: List[CommandResult] = []
        model = FakeModel([
            "Need to write.\n\nwrite /repo/new.py print('hello')",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            tool_dispatcher=lambda r: dispatched.append(r),
        )
        result = loop.run("Create a file")
        self.assertTrue(result.finished)
        # write dispatches as tool, finish also dispatches as tool
        self.assertEqual(len(dispatched), 2)
        self.assertTrue(dispatched[0].needs_tool)
        self.assertEqual(result.writes, 2)

    def test_keyboard_interrupt_during_model_call_returns_partial_result(self):
        loop = TreeLoop(
            model=InterruptingModel(),
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        result = loop.run("Inspect the repo")
        self.assertFalse(result.finished)
        self.assertEqual(result.turns, [])
        self.assertIn("interrupted by operator during model call", result.finish_message)

    def test_run_shell_action_executes_in_live_loop(self):
        model = FakeModel([
            "shell pwd",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
        )
        result = loop.run("Check shell support")
        self.assertTrue(result.finished)
        first_turn_output = "\n".join(r.output for r in result.turns[0].results)
        self.assertIn("exit_code=0", first_turn_output)
        self.assertIn(self.tmpdir, first_turn_output)

    def test_run_shell_action_is_disabled_when_env_flag_is_false(self):
        model = FakeModel([
            "shell pwd",
            "finish done",
        ])
        with mock.patch.dict(os.environ, {"SHELL_ACCESS": "false"}, clear=False):
            loop = TreeLoop(
                model=model,
                workspace_root=Path(self.tmpdir),
                max_turns=5,
                verbose=False,
            )
            result = loop.run("Check shell support")

        self.assertTrue(result.finished)
        first_turn_output = "\n".join(r.output for r in result.turns[0].results)
        self.assertIn("run_shell: disabled by SHELL_ACCESS=false", first_turn_output)

    def test_run_shell_disabled_suggests_structured_npm_command_for_installs(self):
        model = FakeModel([
            "shell npm install react",
            "finish done",
        ])
        with mock.patch.dict(os.environ, {"SHELL_ACCESS": "false"}, clear=False):
            loop = TreeLoop(
                model=model,
                workspace_root=Path(self.tmpdir),
                max_turns=5,
                verbose=False,
            )
            result = loop.run("Install react")

        first_turn_output = "\n".join(r.output for r in result.turns[0].results)
        self.assertIn("Use the structured `npm <args>` command instead", first_turn_output)

    def test_exec_npm_command_runs_without_shell_access(self):
        loop = TreeLoop(
            model=FakeModel(["finish done"]),
            workspace_root=Path(self.tmpdir),
            max_turns=2,
            verbose=False,
            allow_shell=False,
        )
        Path(self.tmpdir, "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
        completed = subprocess.CompletedProcess(
            args=["npm", "install", "react"],
            returncode=0,
            stdout="added 1 package\n",
            stderr="",
        )

        with mock.patch("tree_loop.subprocess.run", return_value=completed):
            output = loop._exec_npm_command({"type": "npm_command", "command": "install react"})

        self.assertIn("manager=npm", output)
        self.assertIn("exit_code=0", output)
        self.assertIn("added 1 package", output)

    def test_run_shell_refuses_long_running_dev_command(self):
        model = FakeModel([
            "shell npm run dev",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
        )
        result = loop.run("Diagnose the app")
        self.assertTrue(result.finished)
        first_turn_output = "\n".join(r.output for r in result.turns[0].results)
        self.assertIn("refused long-running dev/watch command", first_turn_output)
        self.assertIn("npm run build", first_turn_output)

    def test_run_shell_allows_finite_vitest_version_command(self):
        model = FakeModel([
            "shell npx vitest --version",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
        )
        completed = subprocess.CompletedProcess(
            args="npx vitest --version",
            returncode=0,
            stdout="vitest/3.0.0\n",
            stderr="",
        )
        with mock.patch("tree_loop.subprocess.run", return_value=completed) as run_mock:
            result = loop.run("Check vitest availability")

        self.assertTrue(result.finished)
        run_mock.assert_called_once()
        first_turn_output = "\n".join(r.output for r in result.turns[0].results)
        self.assertNotIn("refused long-running dev/watch command", first_turn_output)
        self.assertIn("exit_code=0", first_turn_output)
        self.assertIn("vitest/3.0.0", first_turn_output)

    def test_run_check_strips_ansi_sequences_from_output(self):
        loop = TreeLoop(
            model=FakeModel(["finish done"]),
            workspace_root=Path(self.tmpdir),
            max_turns=2,
            verbose=False,
        )
        completed = subprocess.CompletedProcess(
            args="npm run test",
            returncode=1,
            stdout="\x1b[31mRUN\x1b[0m vitest\n",
            stderr="\x1b[33mId is missing\x1b[0m\n",
        )

        with mock.patch.object(loop, "_detect_check_command", return_value="npm run test"):
            with mock.patch("tree_loop.subprocess.run", return_value=completed):
                output = loop._exec_run_check({"type": "run_check", "kind": "test"})

        self.assertIn("RUN vitest", output)
        self.assertIn("Id is missing", output)
        self.assertNotIn("\x1b[31m", output)
        self.assertNotIn("\x1b[33m", output)

    def test_run_shell_rewrites_virtual_repo_paths_before_execution(self):
        Path(self.tmpdir, "existing.py").write_text("print('hello')\n")
        model = FakeModel([
            "shell rm -f /repo/existing.py",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
        )

        result = loop.run("Delete existing.py")

        self.assertTrue(result.finished)
        self.assertFalse(Path(self.tmpdir, "existing.py").exists())
        first_turn_output = "\n".join(r.output for r in result.turns[0].results)
        self.assertIn("exit_code=0", first_turn_output)

    def test_git_rm_is_forwarded_to_the_host_dispatcher(self):
        model = FakeModel([
            "git rm /repo/missing.py",
            "finish done",
        ])
        dispatched = []
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
            allow_shell=True,
            tool_dispatcher=lambda result: (
                dispatched.append(result.tool_action) or "git rm dispatched"
                if result.tool_action and result.tool_action.get("type") == "git_rm"
                else None
            ),
        )
        result = loop.run("Delete missing.py")

        self.assertTrue(result.finished)
        self.assertEqual(dispatched, [{"type": "git_rm", "paths": ["missing.py"]}])
        first_turn_output = "\n".join(r.output for r in result.turns[0].results)
        self.assertIn("git rm dispatched", first_turn_output)

    def test_log_issue_ingest_sets_signal_steering_and_focus(self):
        Path(self.tmpdir, "live_trace.md").write_text(textwrap.dedent("""\
            3:21:23 PM [vite] Internal server error: Failed to resolve import "uuid" from "src/hooks/useIndexedDbTodos.ts". Does the file exist?
              Plugin: vite:import-analysis
              File: /tmp/app/src/hooks/useIndexedDbTodos.ts:5:29
        """))
        model = FakeModel([
            "ingest-log /repo/live_trace.md",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        loop.run("Fix the issues in the trace")
        second_prompt = model.calls[1]["prompt"]
        self.assertIn("[SIGNAL single_issue_focus_ready]", second_prompt)
        self.assertIn("Current focus: run-dependency-resolution-er-", second_prompt)
        self.assertIn("show-run-issue", second_prompt)
        self.assertIn("current_focus_issue_id", second_prompt)

    def test_resolve_issue_emits_all_resolved_signal(self):
        Path(self.tmpdir, "live_trace.md").write_text(textwrap.dedent("""\
            3:21:23 PM [vite] Internal server error: Failed to resolve import "uuid" from "src/hooks/useIndexedDbTodos.ts". Does the file exist?
              Plugin: vite:import-analysis
              File: /tmp/app/src/hooks/useIndexedDbTodos.ts:5:29
        """))
        class ResolvingModel(FakeModel):
            def complete(self, system: str, prompt: str) -> str:
                self.calls.append({"system": system, "prompt": prompt})
                if self._idx == 0:
                    self._idx += 1
                    return "ingest-log /repo/live_trace.md"
                if self._idx == 1:
                    self._idx += 1
                    marker = "Current focus: "
                    issue_id = prompt.split(marker, 1)[1].split()[0]
                    return f"resolve-run-issue {issue_id}"
                self._idx += 1
                return "finish done"

        model = ResolvingModel([])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        loop.run("Fix the issues in the trace")
        third_prompt = model.calls[2]["prompt"]
        self.assertIn("[SIGNAL all_issues_resolved]", third_prompt)
        self.assertIn("Validate the current goal only, then finish", third_prompt)

    def test_multiple_unresolved_issues_emit_batch_signal_and_strategy_bias(self):
        Path(self.tmpdir, "live_trace.md").write_text(textwrap.dedent("""\
            3:21:23 PM [vite] Internal server error: Failed to resolve import "uuid" from "src/hooks/useIndexedDbTodos.ts". Does the file exist?
              Plugin: vite:import-analysis
              File: /tmp/app/src/hooks/useIndexedDbTodos.ts:5:29
            4:12:36 PM [vite] Internal server error: Failed to resolve import "./IndexedDbTodoItem" from "src/components/IndexedDbTodoList.tsx". Does the file exist?
              Plugin: vite:import-analysis
              File: /tmp/app/src/components/IndexedDbTodoList.tsx:3:30
        """))
        model = FakeModel([
            "ingest-log /repo/live_trace.md",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        loop.run("Fix the issues in the trace")
        second_prompt = model.calls[1]["prompt"]
        self.assertIn("[SIGNAL issue_batch_ready]", second_prompt)
        raw_signals = next(line for line in second_prompt.splitlines() if line.startswith("[RAW SIGNALS]"))
        self.assertEqual(raw_signals.count("issue_ingested:run-"), 2)
        self.assertIn("Inspect the referenced files directly in parallel", second_prompt)
        self.assertIn("resolve-run-issue", second_prompt)

    def test_run_check_typecheck_ingests_typescript_issue(self):
        Path(self.tmpdir, "package.json").write_text(textwrap.dedent("""\
            {
              "name": "tmp-app",
              "private": true,
              "scripts": {
                "typecheck": "printf 'src/components/DemoTodoInput.tsx(2,10): error TS2305: Module \\\"../context/DemoTodoContext\\\" has no exported member \\\"useTodo\\\".\\n' >&2; exit 1"
              }
            }
        """))
        model = FakeModel([
            "run-check typecheck",
            "list-issues",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        result = loop.run("There are errors in the app")
        self.assertTrue(result.finished)
        first_turn_output = "\n".join(r.output for r in result.turns[0].results)
        self.assertIn("issues=", first_turn_output)
        second_turn_output = "\n".join(r.output for r in result.turns[1].results)
        self.assertIn("run-ts2305-", second_turn_output)
        issue_classes = {issue["classification"] for issue in loop.bridge.tree.list_log_issues()}
        self.assertIn("typescript_import_export_error", issue_classes)

    def test_run_route_check_ingests_browser_issues(self):
        model = FakeModel([
            "run-route-check /todo base=http://127.0.0.1:4173",
            "list-issues",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        browser_payload = {
            "url": "http://127.0.0.1:4173/todo",
            "finalUrl": "http://127.0.0.1:4173/todo",
            "route": "/todo",
            "title": "Todo",
            "consoleMessages": [{"type": "error", "text": "ReferenceError: foo is not defined", "location": {"url": "http://127.0.0.1:4173/src/app.tsx", "lineNumber": 10, "columnNumber": 2}}],
            "pageErrors": [],
            "requestFailures": [],
            "responseErrors": [],
        }

        completed = mock.Mock()
        completed.returncode = 0
        completed.stdout = json.dumps(browser_payload)
        completed.stderr = ""

        with mock.patch("tree_loop.subprocess.run", return_value=completed):
            result = loop.run("Check the /todo route")

        self.assertTrue(result.finished)
        first_turn_output = "\n".join(r.output for r in result.turns[0].results)
        self.assertIn("issues=1", first_turn_output)
        second_turn_output = "\n".join(r.output for r in result.turns[1].results)
        self.assertIn("run-browser-console-error-", second_turn_output)
        issues = loop.bridge.tree.list_log_issues()
        issue = loop.bridge.tree.show_log_issue(issues[0]["id"])
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue["classification"], "browser_console_error")
        self.assertEqual(issue["route"], "/todo")

    def test_run_route_check_reports_missing_playwright(self):
        loop = TreeLoop(
            model=FakeModel(["finish done"]),
            workspace_root=Path(self.tmpdir),
            max_turns=1,
            verbose=False,
        )
        completed = mock.Mock()
        completed.returncode = 1
        completed.stdout = ""
        completed.stderr = "Error: Cannot find module 'playwright'"
        with mock.patch("tree_loop.subprocess.run", return_value=completed):
            message = loop._exec_run_route_check({"target": "/todo", "base_url": "http://127.0.0.1:4173"})
        self.assertIn("playwright is not installed", message)


# ── Annotation tests ───────────────────────────────────────────────────

class TestAnnotationParsing(unittest.TestCase):
    def test_parse_thought(self):
        from tree_commands import parse_annotation
        a = parse_annotation(">>th: I need to check the imports")
        self.assertIsNotNone(a)
        assert a is not None
        self.assertEqual(a.tag, "th")
        self.assertEqual(a.tag_name, "thought")
        self.assertEqual(a.content, "I need to check the imports")

    def test_parse_delegation(self):
        from tree_commands import parse_annotation
        a = parse_annotation(">>dg: if no tests exist, create them")
        self.assertIsNotNone(a)
        assert a is not None
        self.assertEqual(a.tag, "dg")
        self.assertEqual(a.tag_name, "delegation")
        self.assertEqual(a.content, "if no tests exist, create them")

    def test_parse_plan(self):
        from tree_commands import parse_annotation
        a = parse_annotation(">>pl: read all modules then summarize")
        self.assertIsNotNone(a)
        assert a is not None
        self.assertEqual(a.tag, "pl")
        self.assertEqual(a.tag_name, "plan")

    def test_parse_question(self):
        from tree_commands import parse_annotation
        a = parse_annotation(">>q: should I include test files?")
        self.assertIsNotNone(a)
        assert a is not None
        self.assertEqual(a.tag, "q")
        self.assertEqual(a.tag_name, "question")

    def test_parse_error_note(self):
        from tree_commands import parse_annotation
        a = parse_annotation(">>err: file not found, path may be wrong")
        self.assertIsNotNone(a)
        assert a is not None
        self.assertEqual(a.tag, "err")
        self.assertEqual(a.tag_name, "error_note")

    def test_not_annotation(self):
        from tree_commands import parse_annotation
        self.assertIsNone(parse_annotation("ls /repo"))
        self.assertIsNone(parse_annotation("just some text"))
        self.assertIsNone(parse_annotation(""))

    def test_custom_tag(self):
        from tree_commands import parse_annotation
        a = parse_annotation(">>ctx: extra context here")
        self.assertIsNotNone(a)
        assert a is not None
        self.assertEqual(a.tag, "ctx")
        self.assertEqual(a.tag_name, "ctx")  # unknown tags use tag as name

    def test_is_annotation(self):
        from tree_commands import is_annotation
        self.assertTrue(is_annotation(">>th: thinking"))
        self.assertTrue(is_annotation(">>dg: if X then Y"))
        self.assertFalse(is_annotation("ls /repo"))
        self.assertFalse(is_annotation(">> not a tag"))

    def test_compact_format(self):
        from tree_commands import Annotation
        a = Annotation(tag="th", tag_name="thought", content="checking repo")
        self.assertEqual(a.compact(), ">>th: checking repo")


class TestAnnotationsInExecuteMulti(unittest.TestCase):
    def test_annotations_extracted(self):
        from context_tree import ContextTree
        from tree_commands import TreeCommandParser, execute_multi
        import tempfile
        tmpdir = tempfile.mkdtemp()
        Path(tmpdir, "main.py").write_text("hello")
        tree = ContextTree(Path(tmpdir))
        tree.index_repo()
        parser = TreeCommandParser(tree)

        raw = ">>th: checking project\nls /repo\n>>pl: will read main next"
        results, annotations = execute_multi(parser, raw)
        self.assertEqual(len(results), 3)
        self.assertEqual(len(annotations), 2)
        self.assertEqual(annotations[0].tag, "th")
        self.assertEqual(annotations[1].tag, "pl")
        # Annotation results are ok with command_type="annotation"
        self.assertEqual(results[0].command_type, "annotation")
        self.assertTrue(results[0].ok)
        # Real command still works
        self.assertEqual(results[1].command_type, "read")
        self.assertTrue(results[1].ok)

    def test_no_annotations(self):
        from context_tree import ContextTree
        from tree_commands import TreeCommandParser, execute_multi
        import tempfile
        tmpdir = tempfile.mkdtemp()
        Path(tmpdir, "f.txt").write_text("x")
        tree = ContextTree(Path(tmpdir))
        tree.index_repo()
        parser = TreeCommandParser(tree)

        results, annotations = execute_multi(parser, "ls /repo")
        self.assertEqual(len(results), 1)
        self.assertEqual(len(annotations), 0)


class TestAnnotationsInLoop(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        Path(self.tmpdir, "main.py").write_text("class Worker: pass\n")

    def test_annotations_captured_in_turn(self):
        model = FakeModel([
            ">>th: I should explore the project structure\n>>pl: list files then read main\nls /repo",
            ">>th: Found main.py, finishing\nfinish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        result = loop.run("Explore")
        self.assertTrue(result.finished)
        # Turn 1 should have annotations
        t1 = result.turns[0]
        self.assertEqual(len(t1.annotations), 2)
        self.assertEqual(t1.thought, "I should explore the project structure")
        self.assertEqual(t1.plan, "list files then read main")

    def test_annotations_in_history(self):
        model = FakeModel([
            ">>th: exploring\nls /repo",
            ">>th: finishing now\nfinish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        loop.run("Test")
        # Second LLM call should include history with >>th: from turn 1
        second_prompt = model.calls[1]["prompt"]
        self.assertIn(">>th: exploring", second_prompt)

    def test_delegation_annotation(self):
        model = FakeModel([
            ">>th: checking for tests\n>>dg: if no test files, I'll create one\nfind /repo *test*",
            ">>th: no tests found, will create\nfinish Created test plan",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        result = loop.run("Check tests")
        t1 = result.turns[0]
        self.assertEqual(t1.delegations, ["if no test files, I'll create one"])

    def test_annotations_are_free(self):
        """Annotations should not count as reads or writes."""
        model = FakeModel([
            ">>th: just thinking\n>>pl: plan something\nls /repo",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        result = loop.run("Test")
        # Only 1 read (ls), 1 write (finish). Annotations don't count.
        self.assertEqual(result.reads, 1)
        self.assertEqual(result.writes, 1)


class TestCheckpoint(unittest.TestCase):
    """Tests for the checkpoint/proceed-stop mechanism."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        Path(self.tmpdir, "main.py").write_text("# Main\n")

    def test_checkpoint_stop_halts_loop(self):
        """Operator says stop at checkpoint — loop ends early."""
        # 5 turns of reads, never finishes
        responses = ["ls /repo"] * 10 + ["finish done"]
        model = FakeModel(responses)
        stop_called = []

        def stop_cb(loop, turn_num):
            stop_called.append(turn_num)
            return False  # stop

        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=100,
            checkpoint_interval=3,
            checkpoint_callback=stop_cb,
            verbose=False,
        )
        result = loop.run("Keep reading")
        # Should stop at turn 3
        self.assertFalse(result.finished)
        self.assertEqual(len(result.turns), 3)
        self.assertEqual(stop_called, [3])
        self.assertIn("stopped by operator", result.finish_message)

    def test_checkpoint_proceed_continues(self):
        """Operator says proceed — loop continues past checkpoint."""
        responses = ["ls /repo"] * 6 + ["finish all done"]
        model = FakeModel(responses)
        proceed_calls = []

        def proceed_cb(loop, turn_num):
            proceed_calls.append(turn_num)
            return True  # proceed

        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=100,
            checkpoint_interval=3,
            checkpoint_callback=proceed_cb,
            verbose=False,
        )
        result = loop.run("Keep reading then finish")
        self.assertTrue(result.finished)
        self.assertEqual(len(result.turns), 7)
        # Checkpoint triggered at turn 3 and 6
        self.assertEqual(proceed_calls, [3, 6])

    def test_no_checkpoint_when_interval_zero(self):
        """No checkpoints when interval is 0."""
        responses = ["ls /repo"] * 5 + ["finish done"]
        model = FakeModel(responses)
        cb_calls = []

        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=100,
            checkpoint_interval=0,
            checkpoint_callback=lambda l, t: cb_calls.append(t) or True,
            verbose=False,
        )
        result = loop.run("Do stuff")
        self.assertTrue(result.finished)
        self.assertEqual(cb_calls, [])  # never called


class TestExecuteTool(unittest.TestCase):
    """Tests that _execute_tool actually writes files to disk."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        Path(self.tmpdir, "existing.py").write_text("print('hello')\n")

    def _make_loop(self):
        model = FakeModel(["finish done"])
        return TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=1,
            verbose=False,
        )

    def test_write_file_creates_on_disk(self):
        loop = self._make_loop()
        from tree_commands import CommandResult
        r = CommandResult(
            ok=True, output="", command_type="write", needs_tool=True,
            tool_action={"type": "write_file", "path": "new_file.txt", "content": "hello world"},
        )
        result = loop._execute_tool(r)
        self.assertIn("wrote", result)
        target = Path(self.tmpdir, "new_file.txt")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(), "hello world")

    def test_write_file_creates_subdirs(self):
        loop = self._make_loop()
        from tree_commands import CommandResult
        r = CommandResult(
            ok=True, output="", command_type="write", needs_tool=True,
            tool_action={"type": "write_file", "path": "src/deep/file.py", "content": "x = 1\n"},
        )
        loop._execute_tool(r)
        target = Path(self.tmpdir, "src", "deep", "file.py")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(), "x = 1\n")

    def test_patch_file_modifies_on_disk(self):
        loop = self._make_loop()
        from tree_commands import CommandResult
        r = CommandResult(
            ok=True, output="", command_type="write", needs_tool=True,
            tool_action={"type": "patch_file", "path": "existing.py", "search": "hello", "replace": "world"},
        )
        result = loop._execute_tool(r)
        self.assertIn("patched", result)
        self.assertEqual(Path(self.tmpdir, "existing.py").read_text(), "print('world')\n")

    def test_replace_lines_modifies_on_disk(self):
        loop = self._make_loop()
        from tree_commands import CommandResult
        r = CommandResult(
            ok=True,
            output="",
            command_type="write",
            needs_tool=True,
            tool_action={
                "type": "replace_lines",
                "path": "existing.py",
                "start_line": 1,
                "end_line": 1,
                "content": "print('world')",
            },
        )
        result = loop._execute_tool(r)
        self.assertIn("replaced lines 1-1", result)
        self.assertIn("line delta +0", result)
        self.assertIn("re-read lines 1-4", result)
        self.assertEqual(Path(self.tmpdir, "existing.py").read_text(), "print('world')\n")

    def test_write_rejects_path_traversal(self):
        loop = self._make_loop()
        from tree_commands import CommandResult
        r = CommandResult(
            ok=True, output="", command_type="write", needs_tool=True,
            tool_action={"type": "write_file", "path": "../../etc/passwd", "content": "bad"},
        )
        result = loop._execute_tool(r)
        self.assertIn("escapes workspace", result)
        self.assertFalse(Path("/etc/passwd_test").exists())

    def test_replace_lines_can_delete_with_empty_content(self):
        loop = self._make_loop()
        from tree_commands import CommandResult
        r = CommandResult(
            ok=True,
            output="",
            command_type="write",
            needs_tool=True,
            tool_action={
                "type": "replace_lines",
                "path": "existing.py",
                "start_line": 1,
                "end_line": 1,
                "content": "",
            },
        )
        result = loop._execute_tool(r)
        self.assertIn("replaced lines 1-1", result)
        self.assertIn("line delta -1", result)
        self.assertIn("re-read lines 1-3", result)
        self.assertEqual(Path(self.tmpdir, "existing.py").read_text(), "")

    def test_begin_and_end_edit_batch_update_runtime_state(self):
        loop = self._make_loop()
        from tree_commands import CommandResult

        start = CommandResult(
            ok=True,
            output="",
            command_type="mutation",
            needs_tool=True,
            tool_action={"type": "begin_edit_batch"},
        )
        start_result = loop._execute_tool(start)
        self.assertIn("edit batch started", start_result)
        self.assertTrue(loop._signal_state["edit_batch_mode"])

        end = CommandResult(
            ok=True,
            output="",
            command_type="mutation",
            needs_tool=True,
            tool_action={"type": "end_edit_batch"},
        )
        end_result = loop._execute_tool(end)
        self.assertIn("edit batch ended", end_result)
        self.assertFalse(loop._signal_state["edit_batch_mode"])

    def test_turn_stops_after_first_failed_mutation(self):
        model = FakeModel([
            "patch /repo/existing.py missing -> world\nwrite /repo/after.txt done\nfinish done",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=3,
            verbose=False,
        )

        result = loop.run("Try one failing edit")

        self.assertTrue(result.finished)
        self.assertFalse(result.turns[0].results[0].ok)
        self.assertIn("search text not found", result.turns[0].results[0].output)
        self.assertIn("skipped after prior command failure", result.turns[0].results[1].output)
        self.assertFalse(Path(self.tmpdir, "after.txt").exists())

    def test_finish_must_be_last_executable_command_in_turn(self):
        model = FakeModel([
            "finish done\nls /repo",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=3,
            verbose=False,
        )

        result = loop.run("Check finish ordering")

        self.assertTrue(result.finished)
        self.assertFalse(result.turns[0].results[0].ok)
        self.assertIn("finish must be the final executable command", result.turns[0].results[0].output)
        self.assertIn("skipped after prior command failure", result.turns[0].results[1].output)

    def test_batch_turn_enforces_mutation_limit(self):
        model = FakeModel([
            "batch start\nwrite /repo/a.txt a\nwrite /repo/b.txt b\nwrite /repo/c.txt c\nwrite /repo/d.txt d\nwrite /repo/e.txt e\nbatch end",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=3,
            verbose=False,
        )

        result = loop.run("Exercise bounded batch writes")

        self.assertTrue(result.finished)
        self.assertIn("mutation batch limit exceeded", result.turns[0].results[5].output)
        self.assertIn("skipped after prior command failure", result.turns[0].results[6].output)
        self.assertFalse(Path(self.tmpdir, "e.txt").exists())

    def test_write_refreshes_recent_full_file_context(self):
        loop = self._make_loop()
        loop.setup()
        loop._recent_reads = [
            RecentRead(
                command="cat /repo/existing.py",
                output=loop.bridge.tree.cat("/repo/existing.py"),
            )
        ]

        from tree_commands import CommandResult
        r = CommandResult(
            ok=True,
            output="",
            command_type="write",
            needs_tool=True,
            tool_action={"type": "write_file", "path": "existing.py", "content": "print('updated')\n"},
        )
        loop._execute_tool(r)

        self.assertEqual(loop._recent_reads[0].command, "cat /repo/existing.py")
        self.assertIn("print('updated')", loop._recent_reads[0].output)
        self.assertNotIn("print('hello')", loop._recent_reads[0].output)

    def test_show_issue_reads_are_carried_in_recent_context(self):
        loop = self._make_loop()
        from tree_commands import CommandResult

        turn = Turn(
            turn_number=1,
            raw_output="show-issue issue-001",
            commands_issued=["show-issue issue-001"],
            results=[CommandResult(ok=True, output="issue-001\nfile: main.py", command_type="read")],
        )

        loop._capture_recent_reads(turn)

        self.assertEqual(len(loop._recent_reads), 1)
        self.assertEqual(loop._recent_reads[0].command, "show-issue issue-001")
        self.assertIn("issue-001", loop._recent_reads[0].output)

    def test_replace_lines_refreshes_recent_range_context(self):
        loop = self._make_loop()
        Path(self.tmpdir, "existing.py").write_text("a = 1\nb = 2\nc = 3\n")
        loop.setup()
        loop._recent_reads = [
            RecentRead(
                command="read-line-range /repo/existing.py 2-3",
                output=loop.bridge.tree.read_line_range("/repo/existing.py", 2, 3, include_line_numbers=True),
            )
        ]

        from tree_commands import CommandResult
        r = CommandResult(
            ok=True,
            output="",
            command_type="write",
            needs_tool=True,
            tool_action={
                "type": "replace_lines",
                "path": "existing.py",
                "start_line": 2,
                "end_line": 2,
                "content": "b = 20",
            },
        )
        loop._execute_tool(r)

        self.assertIn(" 2 | b = 20", loop._recent_reads[0].output)
        self.assertIn(" 3 | c = 3", loop._recent_reads[0].output)
        self.assertNotIn(" 2 | b = 2\n", loop._recent_reads[0].output)

    def test_full_loop_writes_to_disk(self):
        """End-to-end: model emits write command, file appears on disk."""
        model = FakeModel([
            "write /repo/output.md # Architecture\n\nfinish wrote the file",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        result = loop.run("Create output.md")
        self.assertTrue(result.finished)
        target = Path(self.tmpdir, "output.md")
        self.assertTrue(target.exists())

    def test_write_result_flows_into_history(self):
        """After write, r.output should contain the execution result, not the dispatch stub."""
        model = FakeModel([
            ">>th: writing the file\nwrite /repo/notes.md hello world",
            "finish done",
        ])
        loop = TreeLoop(
            model=model,
            workspace_root=Path(self.tmpdir),
            max_turns=5,
            verbose=False,
        )
        result = loop.run("Write notes.md")
        self.assertTrue(result.finished)
        # The write result should be in history as "wrote notes.md (N bytes)"
        turn1 = result.turns[0]
        write_results = [r for r in turn1.results if r.needs_tool and r.command_type == "write"]
        self.assertEqual(len(write_results), 1)
        self.assertIn("wrote", write_results[0].output)
        self.assertIn("notes.md", write_results[0].output)
        # It should NOT still be "[dispatch: write ...]"
        self.assertNotIn("dispatch", write_results[0].output)
        # Verify the compact format includes the write confirmation
        compact = turn1.compact()
        self.assertIn("wrote", compact)
        # And includes the thoughts trace
        self.assertIn("[THOUGHTS]", compact)
        self.assertIn(">>th: writing the file", compact)


if __name__ == "__main__":
    unittest.main()
