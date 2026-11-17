from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mutations.batch as batch_module
from mutations.batch import batch_mutate
from mutations.batch_state import BatchState
from mutation_runtime import execute_mutation
from live_test_loop import TreeLoopPlannerWorker
from tests.test_tree_loop_messages import RecordingMessageModel


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class BatchHashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "note.txt"
        self.original = "alpha\nbeta\n"
        self.file.write_text(self.original)

    def replacements(self):
        before = digest(self.original)
        return [
            {"type": "replace_snippet", "path": "note.txt", "old_text": "alpha", "new_text": "ALPHA", "expected_hash": before},
            {"type": "replace_snippet", "file_path": "./note.txt", "old_text": "beta", "new_text": "BETA", "expected_hash": before},
        ]

    def test_multiple_edits_share_the_batch_start_hash_without_changing_inputs(self):
        for atomic in (False, True):
            with self.subTest(atomic=atomic):
                self.file.write_text(self.original)
                operations = self.replacements()
                untouched = copy.deepcopy(operations)
                result = batch_mutate(operations, atomic=atomic, root=self.root)
                self.assertTrue(result["ok"], result)
                self.assertEqual(self.file.read_text(), "ALPHA\nBETA\n")
                self.assertEqual(operations, untouched)
                first, second = result["operations"]
                self.assertEqual(first["after_hash"], second["before_hash"])
                self.assertEqual(second["details"]["batch_start_expected_hash"], digest(self.original))

    def test_stale_later_hash_rejects_the_whole_batch_before_any_writes(self):
        operations = self.replacements()
        operations[1]["expected_hash"] = "0" * 64
        with patch("mutations.batch._dispatch_operation") as dispatch:
            result = batch_mutate(operations, atomic=True, root=self.root)
        dispatch.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertFalse(result["rolled_back"])
        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(result["operations"][0]["reason"], "expected_hash_mismatch")
        self.assertEqual(result["operations"][0]["details"]["operation_index"], 2)
        self.assertEqual(result["operations"][0]["details"]["phase"], "batch_preflight")
        self.assertEqual(self.file.read_text(), self.original)

    def test_hash_for_a_later_internal_version_is_not_a_batch_start_hash(self):
        operations = self.replacements()
        operations[1]["expected_hash"] = digest("ALPHA\nbeta\n")
        result = batch_mutate(operations, root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(self.file.read_text(), self.original)

    def test_external_write_after_an_applied_operation_is_not_adopted_as_our_output(self):
        real_dispatch = batch_module._dispatch_operation
        def dispatch(operation, **kwargs):
            result = real_dispatch(operation, **kwargs)
            if kwargs["index"] == 0:
                self.file.write_text("external editor content\n")
            return result
        with patch("mutations.batch._dispatch_operation", side_effect=dispatch):
            result = batch_mutate(self.replacements(), atomic=True, root=self.root)
        self.assertFalse(result["ok"])
        self.assertFalse(result["rolled_back"])
        self.assertEqual(result["operations"][1]["reason"], "concurrent_file_change")
        self.assertEqual(result["operations"][-1]["reason"], "rollback_conflict")
        self.assertEqual(self.file.read_text(), "external editor content\n")

    def test_external_write_between_guard_and_handler_keeps_the_known_guard_hash(self):
        real_prepare = BatchState.prepare
        def prepare(state, operation):
            if operation.get("old_text") == "beta":
                self.file.write_text("external editor content\n")
            return real_prepare(state, operation)
        with patch.object(BatchState, "prepare", prepare):
            result = batch_mutate(self.replacements(), atomic=True, root=self.root)
        self.assertFalse(result["ok"])
        self.assertFalse(result["rolled_back"])
        self.assertEqual(result["operations"][1]["reason"], "expected_hash_mismatch")
        self.assertEqual(self.file.read_text(), "external editor content\n")

    def test_other_files_are_snapshotted_before_the_first_operation(self):
        other = self.root / "other.txt"
        other.write_text("original other\n")
        real_dispatch = batch_module._dispatch_operation
        def dispatch(operation, **kwargs):
            result = real_dispatch(operation, **kwargs)
            if kwargs["index"] == 0:
                other.write_text("external other\n")
            return result
        operations = [self.replacements()[0], {"type": "replace_snippet", "path": "other.txt", "old_text": "other", "new_text": "OTHER"}]
        with patch("mutations.batch._dispatch_operation", side_effect=dispatch):
            result = batch_mutate(operations, atomic=True, root=self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual(self.file.read_text(), self.original)
        self.assertEqual(other.read_text(), "external other\n")

    def test_concurrent_creation_of_a_planned_target_is_preserved_even_with_overwrite(self):
        other = self.root / "new.txt"
        real_dispatch = batch_module._dispatch_operation
        def dispatch(operation, **kwargs):
            result = real_dispatch(operation, **kwargs)
            if kwargs["index"] == 0:
                other.write_text("external new file\n")
            return result
        with patch("mutations.batch._dispatch_operation", side_effect=dispatch):
            result = batch_mutate([self.replacements()[0], {"type": "create_file", "path": "new.txt", "content": "ours", "overwrite": True}], atomic=True, root=self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual(other.read_text(), "external new file\n")
        self.assertEqual(self.file.read_text(), self.original)

    def test_copy_sources_are_not_owned_or_restored_by_rollback(self):
        real_dispatch = batch_module._dispatch_operation
        def dispatch(operation, **kwargs):
            result = real_dispatch(operation, **kwargs)
            self.file.write_text("external source\n")
            return result
        with patch("mutations.batch._dispatch_operation", side_effect=dispatch):
            result = batch_mutate([{"type": "copy_file", "source_path": "note.txt", "destination_path": "copy.txt"}], atomic=True, root=self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])
        self.assertFalse((self.root / "copy.txt").exists())
        self.assertEqual(self.file.read_text(), "external source\n")

    def test_copy_path_normalization_keeps_rollback_accounting_complete(self):
        result = batch_mutate([
            {"type": "copy_file", "source_path": " note.txt ", "destination_path": " copy.txt "},
            {"type": "replace_snippet", "path": "note.txt", "old_text": "absent", "new_text": "new"},
        ], atomic=True, root=self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])
        self.assertFalse((self.root / "copy.txt").exists())
        self.assertEqual(self.file.read_text(), self.original)

    def test_created_and_renamed_files_advance_versions_for_later_edits(self):
        result = batch_mutate([
            {"type": "create_file", "path": "new.txt", "content": "new\n"},
            {"type": "replace_snippet", "path": "new.txt", "old_text": "new", "new_text": "NEW"},
            {"type": "rename_file", "old_path": "new.txt", "new_path": "renamed.txt"},
            {"type": "append_block", "path": "renamed.txt", "new_text": "appended\n"},
        ], atomic=True, root=self.root)
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.root / "new.txt").exists())
        self.assertIn("appended", (self.root / "renamed.txt").read_text())

    def test_regex_edits_share_a_raw_crlf_hash_and_preserve_bytes(self):
        original = b"alpha = 1\r\nbeta = 2\r\n"
        self.file.write_bytes(original)
        before = hashlib.sha256(original).hexdigest()
        result = batch_mutate([
            {"type": "replace_regex", "path": "note.txt", "pattern": "alpha", "replacement": "ALPHA", "expected_hash": before},
            {"type": "replace_regex", "path": "note.txt", "pattern": "beta", "replacement": "BETA", "expected_hash": before},
        ], atomic=True, root=self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.file.read_bytes(), b"ALPHA = 1\r\nBETA = 2\r\n")

    def test_later_anchor_failure_restores_exact_starting_bytes(self):
        original = b"alpha\r\nbeta\r\n"
        self.file.write_bytes(original)
        before = hashlib.sha256(original).hexdigest()
        result = batch_mutate([
            {"type": "replace_regex", "path": "note.txt", "pattern": "alpha", "replacement": "ALPHA", "expected_hash": before},
            {"type": "replace_regex", "path": "note.txt", "pattern": "absent", "replacement": "BETA", "expected_hash": before},
        ], atomic=True, root=self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual(self.file.read_bytes(), original)

    def test_shared_mutation_adapter_reports_the_changed_file_once(self):
        result, paths = execute_mutation({"type": "batch_mutate", "atomic": True, "operations": self.replacements()}, self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(paths, ["note.txt"])

    def test_agent_can_apply_and_verify_two_guarded_edits_in_one_batch(self):
        import json
        command = "edit-batch " + json.dumps({"atomic": True, "operations": self.replacements()})
        model = RecordingMessageModel([command, "cat /repo/note.txt\nfinish Updated both locations"])
        worker = TreeLoopPlannerWorker(model=model, root=self.root, max_turns=2, checkpoint_interval=0, verbose=False)
        result = worker.run_task("Update both locations")
        self.assertTrue(result.ok, result.final_message)
        self.assertEqual(self.file.read_text(), "ALPHA\nBETA\n")


if __name__ == "__main__":
    unittest.main()
