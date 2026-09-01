from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Sequence

from tree_commands import CommandResult
from tree_loop import TreeLoop, Turn, extract_commands


class RecordingMessageModel:
    def __init__(self, responses: Sequence[str]) -> None:
        self.responses = list(responses)
        self.calls: List[List[Dict[str, str]]] = []
        self.final_retry_compactor = None

    def set_final_retry_message_compactor(self, compactor) -> None:
        self.final_retry_compactor = compactor

    def complete_messages(self, system: str, messages: Sequence[Dict[str, str]]) -> str:
        self.calls.append([dict(item) for item in messages])
        if not self.responses:
            raise AssertionError("model requested more responses than expected")
        return self.responses.pop(0)

    def complete(self, system: str, prompt: str) -> str:
        raise AssertionError("TreeLoop should use complete_messages when available")


class TreeLoopMessageTranscriptTests(unittest.TestCase):
    def test_issue_lifecycle_commands_are_extracted_as_executable(self) -> None:
        output = "\n".join([
            ">>th: the diagnostic is now clean",
            ">>pl: resolve it",
            "resolve-run-issue run-ts2339-deadbeef01",
        ])

        extracted = extract_commands(output)

        self.assertIn("resolve-run-issue run-ts2339-deadbeef01", extracted)

    def test_external_stop_ends_current_run_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("hello\n", encoding="utf-8")
            model = RecordingMessageModel([
                ">>th: inspect\n>>pl: read note\ncat /repo/note.txt",
            ])
            loop = TreeLoop(model=model, workspace_root=root, max_turns=3, verbose=False)

            def stop_after_command(_command: str, _result: CommandResult) -> None:
                loop.request_stop("execution made no progress")

            loop.command_observer = stop_after_command
            result = loop.run("Read the note")

        self.assertFalse(result.finished)
        self.assertEqual(len(model.calls), 1)
        self.assertIn("execution made no progress", result.finish_message)

    def test_run_uses_stable_message_transcript_instead_of_rebuilt_history_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("hello\n", encoding="utf-8")
            model = RecordingMessageModel([
                ">>th: inspect the file\n>>pl: read note.txt\ncat /repo/note.txt",
                ">>th: file was read\n>>pl: finish\nfinish done",
            ])
            loop = TreeLoop(model=model, workspace_root=root, max_turns=2, verbose=False)

            result = loop.run("Read the note")

        self.assertTrue(result.finished)
        self.assertEqual(len(model.calls), 2)

        first_call = model.calls[0]
        self.assertEqual([item["role"] for item in first_call], ["user"])
        self.assertIn("═══ TASK ═══", first_call[0]["content"])
        self.assertIn("═══ PLAYGROUND OS STATE ═══", first_call[0]["content"])

        second_call = model.calls[1]
        self.assertEqual(
            [item["role"] for item in second_call],
            ["user", "assistant", "user"],
        )
        self.assertIn("cat /repo/note.txt", second_call[1]["content"])
        self.assertIn("═══ LATEST PLAYGROUND OS RESULT ═══", second_call[2]["content"])
        self.assertIn("hello", second_call[2]["content"])
        self.assertNotIn("═══ PLAYGROUND OS STATE ═══", second_call[2]["content"])
        self.assertNotIn("═══ HISTORY (previous turns) ═══", second_call[2]["content"])

    def test_conversation_compaction_replaces_old_messages_with_single_summary(self) -> None:
        model = RecordingMessageModel([])
        with tempfile.TemporaryDirectory() as tmp:
            loop = TreeLoop(model=model, workspace_root=Path(tmp), verbose=False)
            loop._conversation_char_budget = 10
            loop._conversation_messages = [
                {"role": "user", "content": "x" * 20},
                {"role": "assistant", "content": "older response"},
            ]
            loop.history.append(Turn(
                turn_number=1,
                raw_output=">>th: old\n>>pl: finish\nfinish old",
                commands_issued=["finish old"],
                results=[CommandResult(ok=True, output="old", command_type="finish")],
            ))

            loop._compact_conversation_if_needed("Finish the task", "state snapshot")

        self.assertEqual(len(loop._conversation_messages), 1)
        summary = loop._conversation_messages[0]["content"]
        self.assertIn("═══ COMPACTED WORK SUMMARY ═══", summary)
        self.assertIn("Finish the task", summary)
        self.assertIn("Turn 1", summary)
        self.assertIn("═══ PLAYGROUND OS STATE REFRESH ═══", summary)
        self.assertIn("state snapshot", summary)

    def test_final_retry_compactor_refreshes_tree_state_and_replaces_transcript(self) -> None:
        model = RecordingMessageModel([])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "current.txt").write_text("current\n", encoding="utf-8")
            loop = TreeLoop(model=model, workspace_root=root, verbose=False)
            loop.setup()
            loop._conversation_task = "Continue current task"
            loop._conversation_messages = [
                {"role": "user", "content": "old request"},
                {"role": "assistant", "content": "old response"},
            ]

            compacted = list(model.final_retry_compactor(loop._conversation_messages))

        self.assertEqual(compacted, loop._conversation_messages)
        self.assertEqual(len(compacted), 1)
        self.assertIn("COMPACTED WORK SUMMARY", compacted[0]["content"])
        self.assertIn("Continue current task", compacted[0]["content"])
        self.assertIn("current.txt", compacted[0]["content"])

    def test_state_snapshot_is_not_resent_after_cached_transcript_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("hello\n", encoding="utf-8")
            model = RecordingMessageModel([
                ">>th: inspect\n>>pl: read\ncat /repo/note.txt",
                ">>th: done\n>>pl: finish\nfinish ok",
            ])
            loop = TreeLoop(model=model, workspace_root=root, max_turns=2, verbose=False)

            loop.run("Read once")

        first_state_count = sum("PLAYGROUND OS STATE" in item["content"] for item in model.calls[0])
        second_state_count = sum("PLAYGROUND OS STATE" in item["content"] for item in model.calls[1])
        self.assertEqual(first_state_count, 1)
        self.assertEqual(second_state_count, 1)
        self.assertNotIn("PLAYGROUND OS STATE", model.calls[1][-1]["content"])

    def test_reset_conversation_clears_message_state(self) -> None:
        model = RecordingMessageModel([])
        with tempfile.TemporaryDirectory() as tmp:
            loop = TreeLoop(model=model, workspace_root=Path(tmp), verbose=False)
            loop._conversation_messages = [{"role": "user", "content": "task"}]
            loop._conversation_task = "task"
            loop._conversation_compaction_count = 2

            loop.reset_conversation()

        self.assertEqual(loop._conversation_messages, [])
        self.assertEqual(loop._conversation_task, "")
        self.assertEqual(loop._conversation_compaction_count, 0)

    def test_shell_cannot_bypass_typed_git_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loop = TreeLoop(
                model=RecordingMessageModel([]),
                workspace_root=Path(tmp),
                verbose=False,
                allow_shell=True,
            )

            direct = loop._exec_run_shell({"command": "git reset --hard"})
            chained = loop._exec_run_shell({"command": "pwd && git push --force origin dev"})

        self.assertIn("direct git commands are disabled", direct)
        self.assertIn("direct git commands are disabled", chained)

    def test_annotation_only_output_is_repaired_before_command_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = RecordingMessageModel([
                ">>th: I found enough context.\n>>pl: I will finish with the discovery summary.",
                ">>th: execute the plan\n>>pl: finish\nfinish discovery summary",
            ])
            loop = TreeLoop(model=model, workspace_root=Path(tmp), max_turns=2, verbose=False)

            result = loop.run("Discover the landing page")

        self.assertTrue(result.finished)
        self.assertEqual(result.finish_message, "[finish: discovery summary]")
        self.assertEqual(len(result.turns), 1)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result.turns[0].results[-1].command_type, "finish")
        self.assertIn("FORMAT REPAIR REQUIRED", model.calls[1][-1]["content"])
        self.assertNotIn("planning_only", result.turns[0].commands_issued)

    def test_prose_only_output_is_repaired_before_it_becomes_an_invalid_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = RecordingMessageModel([
                "I will first inspect the repository and then decide what to change.",
                ">>th: inspect repository structure\n>>pl: map relevant files\nfinish format repaired",
            ])
            loop = TreeLoop(model=model, workspace_root=Path(tmp), max_turns=1, verbose=False)

            result = loop.run("Inspect the repository")

        self.assertTrue(result.finished)
        self.assertEqual(len(result.turns), 1)
        self.assertNotIn("model_output_invalid", result.turns[0].commands_issued)
        self.assertIn("FORMAT REPAIR REQUIRED", model.calls[1][-1]["content"])

    def test_attached_preamble_keeps_annotation_only_output_continuable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = RecordingMessageModel([
                "Preparing the next action.>>th: inspect current state\n>>pl: choose one command",
                "Still selecting the command.>>th: preserve context\n>>pl: execute on the next turn",
            ])
            loop = TreeLoop(model=model, workspace_root=Path(tmp), max_turns=1, verbose=False)

            result = loop.run("Continue the current issue")

        self.assertFalse(result.finished)
        self.assertIn("planning_only", result.turns[0].commands_issued)
        self.assertNotIn("model_output_invalid", result.turns[0].commands_issued)
        self.assertTrue(result.turns[0].results[-1].ok)

    def test_repeated_annotation_only_turns_still_stop_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = RecordingMessageModel([
                ">>th: planning only\n>>pl: I will inspect files.",
                ">>th: still planning only\n>>pl: I will finish later.",
                ">>th: planning only again\n>>pl: I will inspect files later.",
                ">>th: still no command\n>>pl: I will eventually finish.",
            ])
            loop = TreeLoop(model=model, workspace_root=Path(tmp), max_turns=3, verbose=False)

            result = loop.run("Discover the landing page")

        self.assertFalse(result.finished)
        self.assertEqual(len(result.turns), 2)
        self.assertIn("no executable tree commands", result.finish_message)
        self.assertEqual(result.turns[0].results[-1].command_type, "planning_only")
        self.assertEqual(result.turns[1].results[-1].command_type, "planning_only")
        self.assertEqual(len(model.calls), 4)

    def test_run_diagnostic_focus_uses_explicit_namespace_and_avoids_repeat_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loop = TreeLoop(model=RecordingMessageModel([]), workspace_root=Path(tmp), verbose=False)
            issue_id = "run-ts1005-deadbeef01"
            loop.bridge.tree.set_fact(loop.bridge.tree.LOG_ISSUES_ROOT, issue_id, "status", "open")
            loop.bridge.tree.set_fact(loop.bridge.tree.LOG_ISSUES_ROOT, issue_id, "summary", "';' expected")
            loop.bridge.tree.set_fact(loop.bridge.tree.LOG_ISSUES_ROOT, issue_id, "file", "src/app.ts")
            loop.bridge.tree.set_fact(loop.bridge.tree.LOG_ISSUES_ROOT, issue_id, "line", "20")

            loop._refresh_log_issue_signals()
            initial_steering = loop._signal_steering
            loop._record_run_issue_inspections(
                Turn(
                    turn_number=1,
                    raw_output="read",
                    commands_issued=["read-line-range /repo/src/app.ts 1-40"],
                    results=[CommandResult(ok=True, output="content", command_type="read")],
                )
            )
            loop._refresh_log_issue_signals()

        self.assertIn("RUN DIAGNOSTIC FOCUS", initial_steering)
        self.assertIn("transient and separate from the active durable planner issue", initial_steering)
        self.assertNotIn("`show-issue", initial_steering)
        self.assertIn("already inspected", loop._signal_steering)
        self.assertIn(f"Do not call `show-run-issue {issue_id}` again", loop._signal_steering)


if __name__ == "__main__":
    unittest.main()
