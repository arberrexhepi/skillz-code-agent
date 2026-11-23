from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import MethodType
import unittest

from main import AgentConfig, WorkingFolderAgent
from tests.test_tree_loop_messages import RecordingMessageModel


class FakeTranscriptModel:
    def __init__(self):
        self.calls = []

    def complete_messages(self, system, messages):
        self.calls.append({"system": system, "messages": [dict(item) for item in messages]})
        return '{"thought":"ok","action":{"type":"finish","message":"done"}}'

    def complete(self, system, prompt):
        raise AssertionError("stable worker should prefer complete_messages")

    def get_last_metrics(self):
        return {"provider": "fake", "model": "fake-model", "usage": {"input_tokens": 10, "output_tokens": 2}}


class StableWorkerTranscriptTests(unittest.TestCase):
    def _worker(self) -> WorkingFolderAgent:
        worker = WorkingFolderAgent.__new__(WorkingFolderAgent)
        worker.model = FakeTranscriptModel()
        worker._model_conversation_messages = []
        worker._model_conversation_task = ""
        worker._system_prompt = MethodType(lambda self: "system", worker)
        worker._build_prompt = MethodType(lambda self, task: f"FULL CONTEXT: {task}", worker)
        worker._build_incremental_prompt = MethodType(lambda self, task: f"INCREMENTAL UPDATE: {task}", worker)
        return worker

    def test_stable_worker_uses_message_transcript_after_first_turn(self):
        worker = self._worker()
        worker._reset_model_conversation("Fix cache")

        first_prompt = worker._build_model_turn_prompt("Fix cache")
        first_raw = worker._complete_worker_model_turn(first_prompt)
        second_prompt = worker._build_model_turn_prompt("Fix cache")
        second_raw = worker._complete_worker_model_turn(second_prompt)

        self.assertIn("finish", first_raw)
        self.assertIn("finish", second_raw)
        self.assertEqual(first_prompt, "FULL CONTEXT: Fix cache")
        self.assertEqual(second_prompt, "INCREMENTAL UPDATE: Fix cache")

        first_call = worker.model.calls[0]["messages"]
        second_call = worker.model.calls[1]["messages"]
        self.assertEqual([item["role"] for item in first_call], ["user"])
        self.assertEqual([item["role"] for item in second_call], ["user", "assistant", "user"])
        self.assertEqual(second_call[0]["content"], "FULL CONTEXT: Fix cache")
        self.assertEqual(second_call[2]["content"], "INCREMENTAL UPDATE: Fix cache")

    def test_stable_worker_runs_more_than_four_edits_in_one_model_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writes = [{"type": "write_file", "path": f"sample-{i}.txt", "content": str(i)} for i in range(6)]
            action = [{"type": "begin_edit_batch"}, *writes, {"type": "end_edit_batch"}, {"type": "finish", "message": "Done"}]
            model = RecordingMessageModel([json.dumps({"thought": "Write fixture files", "actions": action})])
            config = AgentConfig("gemini", "fixture", root, Path(__file__).resolve().parents[1] / "agent_tools.py", max_steps=1, quiet=True)
            worker = WorkingFolderAgent(model, config)
            result = worker.run_task("Write the fixture files")
            self.assertTrue(result.ok, result.final_message)
            self.assertEqual(len(model.calls), 1)
            self.assertEqual([(root / write["path"]).read_text() for write in writes], [str(i) for i in range(6)])

    def test_stable_worker_retries_provider_rejection_even_if_the_json_is_valid(self):
        model = RecordingMessageModel([
            '{"action":{"type":"finish","message":"Rejected response"}}',
            '{"action":{"type":"finish","message":"Recovered response"}}',
        ])
        model.get_last_metrics = lambda: {"response": {"candidates": [
            {"finish_reason": "MALFORMED_RESPONSE" if len(model.calls) == 1 else "STOP"}
        ]}}
        with tempfile.TemporaryDirectory() as directory:
            config = AgentConfig("gemini", "fixture", Path(directory), Path(__file__).resolve().parents[1] / "agent_tools.py",
                                 max_steps=2, quiet=True)
            worker = WorkingFolderAgent(model, config)
            result = worker.run_task("Report the result")
        self.assertTrue(result.ok)
        self.assertEqual(result.final_message, "Recovered response")
        self.assertIn("MALFORMED_RESPONSE", worker.history[0].result.payload["error"])
        self.assertEqual(len(model.calls), 2)


if __name__ == "__main__":
    unittest.main()
