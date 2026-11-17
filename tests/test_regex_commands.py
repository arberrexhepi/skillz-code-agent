from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from command_extensions import extension_action
from discovery.dispatch import execute_discovery_action
from execution_evidence import repository_observation
from live_test_loop import TreeLoopPlannerWorker
from main import AgentConfig, WorkingFolderAgent
from mutation_runtime import execute_mutation
from mutations import replace_regex
from regex_runtime import evaluate_regex
from tests.test_mutation_suite import FakeModelClient
from tests.test_live_test_loop import FakeModelClient as TreeModel
from tree_commands import CommandResult, parse_strategy
from tree_loop import TreeLoop, extract_commands


TOOLS = Path(__file__).resolve().parents[1] / "agent_tools.py"


class RegexCommandsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "app.txt"
        self.path.write_text("name = 12\nother = 34\n", encoding="utf-8")

    def find(self, **options):
        return execute_discovery_action({"type": "find_regex", "path": "/repo/app.txt", **options}, root=self.root)

    def test_preview_is_read_only_with_captures_hash_and_exact_source_windows(self):
        before = self.path.read_bytes()
        result = self.find(pattern=r"(?P<key>\w+) = (\d+)", replacement=r"\g<key>: \2", before=0, after=0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["match_count"], 2)
        self.assertEqual(result["matches"][1]["groups"], ["other", "34"])
        self.assertEqual(result["matches"][1]["named_groups"], {"key": "other"})
        self.assertEqual(result["matches"][1]["start_line"], 2)
        self.assertEqual(result["file_hash"], hashlib.sha256(before).hexdigest())
        self.assertIn("+name: 12", result["diff"])
        self.assertNotIn("updated", result)
        self.assertEqual(self.path.read_bytes(), before)

    def test_find_pages_matches_without_counting_unshown_source_as_read(self):
        result = self.find(pattern=r"\w+", limit=1, offset=1, before=0, after=0)
        self.assertEqual(result["match_count"], 4)
        self.assertEqual(result["matches"][0]["text"], "12")
        self.assertEqual(result["next_offset"], 2)
        command_result = CommandResult(True, json.dumps(result), "read", tool_action={"type": "find_regex", "path": "/repo/app.txt"})
        observation = repository_observation("find-regex /repo/app.txt {}", command_result)
        self.assertEqual([number for number, _ in observation.source_lines], [1])
        missing = self.find(pattern="absent")
        command_result.output = json.dumps(missing)
        self.assertTrue(repository_observation("find-regex /repo/app.txt {}", command_result).empty_search)

    def test_exact_match_count_and_hash_guard_apply_multimatch_edit(self):
        preview = self.find(pattern=r"(?P<key>\w+) = (\d+)")
        result = replace_regex("app.txt", r"(?P<key>\w+) = (\d+)", r"\g<key>: \2", expected_matches=2,
                               expected_hash=preview["file_hash"], root=self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.path.read_text(), "name: 12\nother: 34\n")
        self.assertEqual(result["details"]["replacements"], 2)
        self.assertEqual(result["changed_line_count"], 4)

    def test_default_count_rejects_ambiguous_or_missing_matches_without_writes(self):
        before = self.path.read_bytes()
        for pattern in (r"\d+", "missing"):
            result = replace_regex("app.txt", pattern, "replacement", root=self.root)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "occurrence_mismatch")
            self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_pattern_flags_template_or_count_cannot_write(self):
        before = self.path.read_bytes()
        for options in ({"pattern": "("}, {"pattern": "name", "flags": "g"},
                        {"pattern": "name", "replacement": r"\g<missing>"},
                        {"pattern": "name", "replacement": None}, {"pattern": "name", "expected_matches": None},
                        {"pattern": "name", "expected_matches": True}, {"pattern": "name", "expected_matches": 0}):
            action = {"type": "replace_regex", "path": "app.txt", "replacement": "new", **options}
            result, changed = execute_mutation(action, self.root)
            self.assertFalse(result["ok"], result)
            self.assertEqual(changed, [])
            self.assertEqual(self.path.read_bytes(), before)

    def test_stale_hash_fails_before_regex_evaluation(self):
        with mock.patch("mutations.regex_ops.evaluate_regex") as evaluate:
            result = replace_regex("app.txt", "name", "new", expected_hash="0" * 64, root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "expected_hash_mismatch")
        evaluate.assert_not_called()

    def test_change_during_evaluation_is_preserved(self):
        def evaluate(*args, **kwargs):
            self.path.write_text("external edit\n")
            return {"ok": True, "updated": "our edit\n", "match_count": 1}
        with mock.patch("mutations.regex_ops.evaluate_regex", side_effect=evaluate):
            result = replace_regex("app.txt", "name", "new", root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(self.path.read_text(), "external edit\n")

    def test_atomic_failure_does_not_restore_over_an_external_edit(self):
        def evaluate(*args, **kwargs):
            self.path.write_text("external edit\n")
            return {"ok": True, "updated": "our edit\n", "match_count": 1}
        with mock.patch("mutations.regex_ops.evaluate_regex", side_effect=evaluate):
            result, changed = execute_mutation({"type": "batch_mutate", "atomic": True, "operations": [
                {"type": "replace_regex", "path": "app.txt", "pattern": "name", "replacement": "new"},
            ]}, self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(changed, [])
        self.assertEqual(self.path.read_text(), "external edit\n")

    def test_atomic_rollback_reports_a_conflict_instead_of_overwriting_external_work(self):
        def evaluate(*args, **kwargs):
            self.path.write_text("external edit\n")
            return {"ok": True, "updated": "our edit\n", "match_count": 1}
        with mock.patch("mutations.regex_ops.evaluate_regex", side_effect=evaluate):
            result, changed = execute_mutation({"type": "batch_mutate", "atomic": True, "operations": [
                {"type": "replace_snippet", "path": "app.txt", "old_text": "name", "new_text": "changed"},
                {"type": "replace_regex", "path": "app.txt", "pattern": "other", "replacement": "new"},
            ]}, self.root)
        self.assertFalse(result["ok"])
        self.assertFalse(result["rolled_back"])
        self.assertEqual(result["operations"][-1]["reason"], "rollback_conflict")
        self.assertIn("app.txt", changed)
        self.assertEqual(self.path.read_text(), "external edit\n")

    def test_atomic_regex_batch_can_apply_guarded_edits_to_multiple_files(self):
        second = self.root / "second.txt"
        second.write_text("name\n")
        result, changed = execute_mutation({"type": "batch_mutate", "atomic": True, "operations": [
            {"type": "replace_regex", "path": "app.txt", "pattern": "name", "replacement": "new"},
            {"type": "replace_regex", "path": "second.txt", "pattern": "name", "replacement": "new"},
        ]}, self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(changed, ["app.txt", "second.txt"])
        self.assertEqual(second.read_text(), "new\n")

    def test_crlf_unicode_and_trailing_newline_are_preserved(self):
        original = "café = 12\r\nother = 34\r\n".encode()
        self.path.write_bytes(original)
        preview = self.find(pattern="café")
        self.assertEqual(preview["file_hash"], hashlib.sha256(original).hexdigest())
        result = replace_regex("app.txt", "café", "été", expected_hash=preview["file_hash"], root=self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(self.path.read_bytes(), "été = 12\r\nother = 34\r\n".encode())

    def test_atomic_batch_rolls_back_regex_changes_without_normalizing_crlf(self):
        original = b"name = 12\r\nother = 34\r\n"
        self.path.write_bytes(original)
        result, paths = execute_mutation({"type": "batch_mutate", "atomic": True, "operations": [
            {"type": "replace_regex", "path": "app.txt", "pattern": "name", "replacement": "first"},
            {"type": "replace_regex", "path": "app.txt", "pattern": "absent", "replacement": "second"},
        ]}, self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual(paths, [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_quoted_paths_and_regex_json_are_lossless_in_strategy_commands(self):
        path = '/repo/file with spaces.ts'
        options = {"pattern": r"\b(name)\b", "replacement": r"<span>{\1} -> literal</span>"}
        command = f"replace-regex {json.dumps(path)} {json.dumps(options)}"
        self.assertEqual(extension_action("replace-regex", command.split(None, 1)[1]), {"type": "replace_regex", "path": path, **options})
        self.assertEqual(extract_commands(command), command)
        self.assertEqual(parse_strategy("s1: " + command).steps["s1"].commands[0], command)
        with self.assertRaises(ValueError):
            extension_action("replace-regex", '/repo/app.txt {"pattern":"x","replacement":"y","root":"/tmp"}')

    def test_multiline_case_and_zero_width_replacements_follow_python_semantics(self):
        for text, pattern, replacement, flags in (("Foo\nfoo", "^foo$", "new", "im"),
                                                   ("start\nbody\nend", "start.*end", "new", "s"),
                                                   ("a\nb", "^", "> ", "m")):
            expected = re.sub(pattern, replacement, text, flags=sum({"i": re.I, "m": re.M, "s": re.S}[flag] for flag in flags))
            result = evaluate_regex(text, pattern=pattern, replacement=replacement, flags=flags)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["updated"], expected)

    def test_catastrophic_backtracking_times_out_without_writing(self):
        self.path.write_text("a" * 40 + "!")
        before = self.path.read_bytes()
        start = time.monotonic()
        result = replace_regex("app.txt", "(a+)+$", "replacement", root=self.root)
        self.assertEqual(result["reason"], "regex_timeout")
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(self.path.read_bytes(), before)

    def test_match_and_output_limits_return_failures(self):
        self.assertEqual(evaluate_regex("x" * 10001, pattern="x")["reason"], "too_many_matches")
        result = evaluate_regex("x" * 9000, pattern="x", replacement="y" * 500)
        self.assertFalse(result["ok"])

    def test_path_and_symlink_escape_fail_for_search_and_edits(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "secret.txt"
            outside.write_text("secret")
            self.assertFalse(execute_discovery_action({"type": "find_regex", "path": str(outside), "pattern": "secret"}, root=self.root)["ok"])
            with self.assertRaises(ValueError):
                replace_regex(str(outside), "secret", "changed", root=self.root)
            if os.name != "nt":
                (self.root / "escape.txt").symlink_to(outside)
                with self.assertRaises(ValueError):
                    replace_regex("escape.txt", "secret", "changed", root=self.root)
            self.assertEqual(outside.read_text(), "secret")

    def test_beta_runtime_counts_preview_as_read_and_verifies_real_replacement(self):
        preview = 'find-regex /repo/app.txt ' + json.dumps({"pattern": "name", "replacement": "updated"})
        edit = 'replace-regex /repo/app.txt ' + json.dumps({"pattern": "name", "replacement": "updated"})
        probe = TreeLoop(model=None, workspace_root=self.root, verbose=False)
        probe._execute_command_batch(preview)
        self.assertEqual(probe._total_writes, 0)
        worker = TreeLoopPlannerWorker(model=TreeModel([preview, edit, "cat /repo/app.txt\nfinish Updated"]), root=self.root,
                                       max_turns=3, checkpoint_interval=0, verbose=False)
        with mock.patch.object(worker.loop, "_run_backend_diagnostics_for_path", return_value=""):
            result = worker.run_task("Rename the field")
        self.assertTrue(result.ok, result.final_message)
        self.assertEqual(worker.history[0].results[0].command_type, "read")
        self.assertIn("app.txt", result.touched_paths)
        self.assertIn("updated = 12", self.path.read_text())

    def test_failed_regex_edit_guides_a_bounded_preview_then_releases_patch_recovery(self):
        worker = TreeLoopPlannerWorker(model=TreeModel([]), root=self.root, verbose=False)
        command = 'replace-regex /repo/app.txt {"pattern":"\\\\d+","replacement":"new"}'
        _, results, _ = worker.loop._execute_command_batch(command)
        self.assertFalse(results[0].ok)
        self.assertIn("find-regex", worker._active_mode_strategy()["steps"][0])
        worker.loop._execute_command_batch('find-regex /repo/app.txt {"pattern":"\\\\d+"}')
        self.assertIsNone(worker._patch_resolution)

    def test_cli_and_stable_runtime_support_the_same_regex_actions(self):
        spec = self.root / "regex.json"
        spec.write_text(json.dumps({"path": "app.txt", "pattern": r"\d+", "replacement": "0", "expected_matches": 2}))
        run = subprocess.run([sys.executable, str(TOOLS), "replace-regex", "--root", str(self.root), "--spec-file", str(spec)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(json.loads(run.stdout)["ok"])
        agent = WorkingFolderAgent(model_client=FakeModelClient(), config=AgentConfig(provider="fixture", model="fixture", root=self.root, tool_script=TOOLS))
        found = agent._execute_action({"type": "find_regex", "path": "app.txt", "pattern": "name"})
        self.assertTrue(found.ok, found.payload)
        edited = agent._execute_action({"type": "replace_regex", "path": "app.txt", "pattern": "name", "replacement": "renamed"})
        self.assertTrue(edited.ok, edited.payload)
        self.assertIn("renamed = 0", self.path.read_text())
        self.assertIsNotNone(agent.pending_verification)


if __name__ == "__main__":
    unittest.main()
