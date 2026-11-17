from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from execution_evidence import repository_observation
from live_test_loop import TreeLoopPlannerWorker
from model_result_delivery import prepare_model_result
from tree_commands import CommandResult
from tree_loop import TreeLoop, Turn
from tests.test_tree_loop_messages import RecordingMessageModel
from tests.test_tree_loop import FakeModel


def source_result(path: str, marker: str, count: int = 240) -> tuple[str, CommandResult]:
    command = f"read-line-range /repo/{path} 1-{count}"
    result = CommandResult(True, "\n".join(f"{n} | {marker} {'.' * 75}" for n in range(1, count + 1)), "read")
    prepare_model_result(command, result)
    return command, result


class ModelResultDeliveryTests(unittest.TestCase):
    def test_long_source_tail_reaches_chat_and_legacy_providers(self):
        for model_type in (RecordingMessageModel, FakeModel):
            with self.subTest(provider=model_type.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                text = "\n".join(f"const line{n} = '{'x' * 65}';" for n in range(1, 110)) + "\nTAIL_ANCHOR_110\n"
                (root / "App.tsx").write_text(text)
                model = model_type(["read-line-range /repo/App.tsx 1-110", "finish inspected"])
                observed = []
                loop = TreeLoop(model=model, workspace_root=root, max_turns=2, verbose=False,
                                command_observer=lambda cmd, result: observed.append(repository_observation(cmd, result)))
                self.assertTrue(loop.run("Inspect the whole range").finished)
                call = model.calls[1]
                delivered = call["prompt"] if isinstance(call, dict) else "\n".join(msg["content"] for msg in call)
                self.assertIn("TAIL_ANCHOR_110", delivered)
                self.assertEqual(len(observed[0].source_lines), 110)

    def test_oversized_text_counts_only_complete_delivered_lines(self):
        command, result = source_result("App.tsx", "anchor", count=1000)
        original = result.output
        prepare_model_result(command, result, limit=5000)
        observation = repository_observation(command, result)
        last = observation.source_lines[-1][0]
        self.assertLess(last, 1000)
        self.assertIn(f"read-line-range /repo/App.tsx {last + 1}-", result.model_output)
        self.assertNotIn((last + 1, "anchor " + "." * 75), observation.source_lines)
        self.assertEqual(original, result.output)
        self.assertLessEqual(len(result.model_output), 5000)
        self.assertTrue(result.to_dict()["model_output_truncated"])
        self.assertEqual(result.to_dict()["model_output_chars"], len(result.model_output))

    def test_structured_source_is_valid_json_and_reports_its_delivered_span(self):
        content = "\n".join(f"line{n} {'x' * 95}" for n in range(1, 300))
        shapes = [
            ("read_file", {"start_line": 50, "end_line": 348, "content": content}),
            ("read_context", {"blocks": [{"start_line": 50, "end_line": 348, "content": content}]}),
            ("read_symbol", {"hits": [{"start_line": 50, "end_line": 348, "details": {
                "content": content, "symbol_start_line": 50, "symbol_end_line": 348}}]}),
        ]
        for kind, payload in shapes:
            with self.subTest(kind=kind):
                result = CommandResult(True, json.dumps(payload), "read", tool_action={"type": kind, "file_path": "App.tsx"})
                prepare_model_result("discover {}", result, limit=5000)
                shown = json.loads(result.model_output)
                self.assertTrue(shown["model_delivery"]["truncated"])
                observation = repository_observation("discover {}", result)
                self.assertEqual(observation.source_lines[0][0], 50)
                self.assertLess(observation.source_lines[-1][0], 348)
                self.assertIn(str(observation.source_lines[-1][0] + 1) + "-", shown["model_delivery"]["continue_with"])
                self.assertLessEqual(len(result.model_output), 5000)

    def test_compaction_and_final_retry_retain_whole_packets_and_forget_evictions(self):
        for retry in (False, True):
            with self.subTest(final_retry=retry), tempfile.TemporaryDirectory() as tmp:
                worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
                packets = [source_result(f"{name}.tsx", f"ONLY_{name}") for name in ("A", "B", "C")]
                worker._replace_retained_read_context(packets)
                loop = TreeLoop(model=RecordingMessageModel([]), workspace_root=Path(tmp), verbose=False,
                                read_context_observer=worker._replace_retained_read_context)
                for n, packet in enumerate(packets, 1):
                    loop._read_working_set.record(*packet)
                    loop.history.append(Turn(n, packet[0], [packet[0]], [packet[1]]))
                loop._conversation_messages = [{"role": "user", "content": "old" * 100}]
                loop._conversation_char_budget = 1
                if retry:
                    loop._compact_messages_for_final_retry(loop._conversation_messages)
                else:
                    loop._compact_conversation_if_needed("Inspect", "state")
                summary = loop._conversation_messages[0]["content"]
                self.assertNotIn("ONLY_A", summary)
                self.assertIn(packets[-1][1].model_output, summary)
                self.assertTrue(worker._execution_observation_is_new(repository_observation(*packets[0])))
                self.assertFalse(worker._execution_observation_is_new(repository_observation(*packets[-1])))
                loop.reset_conversation()
                self.assertTrue(worker._execution_observation_is_new(repository_observation(*packets[-1])))

    def test_observer_replacement_cannot_leak_cached_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("UNDELIVERED_SOURCE")
            model = RecordingMessageModel(["cat /repo/note.txt", "finish blocked"])

            def block(command, result):
                if command.startswith("cat"):
                    result.ok = False
                    result.output = "Read blocked: explicit reason"

            loop = TreeLoop(model=model, workspace_root=root, max_turns=2, verbose=False, command_observer=block)
            loop.run("Read")
            result_message = model.calls[1][-1]["content"]
            self.assertIn("Read blocked: explicit reason", result_message)
            self.assertNotIn("UNDELIVERED_SOURCE", result_message)
            self.assertEqual(loop._read_working_set.packets, [])

    def test_read_then_edit_in_one_batch_cannot_restore_stale_compacted_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("OLD_SOURCE")
            model = RecordingMessageModel(["cat /repo/note.txt\nwrite note.txt NEW_SOURCE", "finish done"])
            loop = TreeLoop(model=model, workspace_root=root, max_turns=2, verbose=False)
            loop.run("Update the note")
            self.assertEqual((root / "note.txt").read_text(), "NEW_SOURCE")
            summary = loop._compacted_context("Update the note", "state")
            self.assertNotIn("OLD_SOURCE", summary)

    def test_diagnostic_source_survives_compaction_until_the_repair(self):
        from model_result_delivery import ReadWorkingSet

        working = ReadWorkingSet()
        packets = [source_result(f"{name}.tsx", f"ONLY_{name}") for name in ("A", "B", "C")]
        for packet in packets:
            working.record(*packet)
        working.focus_diagnostic("A.tsx", 100)
        retained = working.compact()
        commands = [command for command, _ in retained]
        self.assertIn(packets[0][0], commands)
        self.assertIn(packets[-1][0], commands)
        self.assertLessEqual(working.manifest()["used_chars"], working.manifest()["budget_chars"])
        working.invalidate("A.tsx")
        self.assertIsNone(working.focus)


if __name__ == "__main__":
    unittest.main()
