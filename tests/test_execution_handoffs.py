from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from context_tree import ContextTree
from live_test_loop import TreeLoopPlannerWorker
from main import GeminiModelClient
from planner import PlannerAgent
from model_result_delivery import prepare_model_result
from repository_changes import RepositorySnapshot
from tests.test_tree_loop_messages import RecordingMessageModel
from tree_commands import CommandResult
from tree_loop import TreeLoop


def shell_write(script: str) -> str:
    return f"shell {shlex.quote(sys.executable)} -c {shlex.quote(script)}"


class ExecutionHandoffTests(unittest.TestCase):
    def test_goal_two_preserves_facts_and_source_without_replaying_goal_one_dialogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("SOURCE_ANCHOR\n")
            model = RecordingMessageModel([
                ">>th: OLD_PLAN_PROMISE\ncat /repo/note.txt\nfact demo/architecture/entrypoint note.txt\nfinish Inspected",
                "cat /repo/note.txt", "finish Second goal done",
            ])
            worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=3, checkpoint_interval=0, verbose=False)
            self.assertTrue(worker.run_task("Goal one: discover").ok)
            worker.prepare_for_goal(preserve_context=True)
            self.assertEqual(len(worker.history), 1)  # Audit history is intact.
            self.assertEqual(worker.loop._conversation_messages, [])
            worker.loop._conversation_char_budget = 1  # Also exercise later compaction.
            second = worker.run_task("Goal two: inspect the retained target")
            self.assertTrue(second.ok, second.final_message)
            for call in model.calls[1:]:
                text = "\n".join(message["content"] for message in call)
                self.assertIn("Goal two: inspect the retained target", text)
                self.assertIn("SOURCE_ANCHOR", text)
                self.assertNotIn("OLD_PLAN_PROMISE", text)
                self.assertNotIn("Goal one: discover", text)
            self.assertEqual(worker.issue_ledger.find_fact("entrypoint").value, "note.txt")
            self.assertEqual(worker.loop._conversation_history_start, 1)

    def test_handoff_evidence_is_bounded_and_coverage_matches_what_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            retained = []
            loop = TreeLoop(model=RecordingMessageModel([]), workspace_root=Path(tmp), verbose=False,
                            read_context_observer=lambda packets: retained.append(list(packets)))
            for name in ("first", "second", "third"):
                command = f"cat /repo/{name}.txt"
                result = CommandResult(True, "1 | " + name * 4000, "read")
                prepare_model_result(command, result)
                loop._read_working_set.record(command, result)
            loop.begin_goal_handoff()
            self.assertLessEqual(loop._read_working_set.manifest()["used_chars"], 32000)
            self.assertLess(len(retained[-1]), 3)
            self.assertEqual(retained[-1], loop._read_working_set.packets)


class ActualRepositoryChangesTests(unittest.TestCase):
    def test_snapshot_detects_already_dirty_changes_create_delete_and_ignores_touches(self):
        for git_repo in (False, True):
            with self.subTest(git_repo=git_repo), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if git_repo:
                    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
                target = root / "package.json"
                target.write_text("old")
                (root / "deleted.txt").write_text("delete")
                if git_repo:
                    subprocess.run(["git", "add", "package.json"], cwd=root, check=True)
                    target.write_text("dirty")
                before = RepositorySnapshot.capture(root)
                os.utime(target, None)
                touched = RepositorySnapshot.capture(root, previous=before)
                self.assertEqual(touched.changed_since(before), [])
                target.write_text("newer")
                (root / "deleted.txt").unlink()
                (root / "new.txt").write_text("new")
                (root / "node_modules").mkdir()
                (root / "node_modules" / "ignored.txt").write_text("dependency")
                after = RepositorySnapshot.capture(root, previous=touched)
                self.assertEqual(after.changed_since(before), ["deleted.txt", "new.txt", "package.json"])
                self.assertFalse(after.errors)

    def test_shell_mutation_resets_delivery_requires_verification_and_refreshes_cached_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"dependencies":{}}\n')
            command = shell_write("from pathlib import Path; Path('package.json').write_text('{\"dependencies\":{\"three\":\"installed\"}}\\n')")
            model = RecordingMessageModel(["cat /repo/package.json", command + "\ncat /repo/package.json", "finish Installed"])
            worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=3, checkpoint_interval=0, verbose=False)
            worker.loop.allow_shell = True
            result = worker.run_task("Install dependency")
            self.assertTrue(result.ok, result.final_message)
            self.assertTrue(worker._has_mutation)
            self.assertEqual(worker._execution_delivery.read_only_turns, 0)
            shell_result = worker.history[1].results[0]
            self.assertEqual(shell_result.tool_action["_changed_paths"], ["package.json"])
            self.assertIn('"three"', worker.history[1].results[1].output)
            self.assertIn("package.json", result.touched_paths)
            self.assertTrue(result.validation.passed)
            self.assertNotEqual(result.validation.kind, "none")

    def test_failed_shell_still_accounts_for_files_and_requires_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = shell_write("from pathlib import Path; Path('created.txt').write_text('partial'); raise SystemExit(2)")
            worker = TreeLoopPlannerWorker(model=RecordingMessageModel([command]), root=root,
                                           max_turns=1, checkpoint_interval=0, verbose=False)
            worker.loop.allow_shell = True
            result = worker.run_task("Attempt an edit")
            self.assertFalse(result.ok)
            self.assertTrue(worker._has_mutation)
            self.assertEqual(worker.history[0].results[0].tool_action["_changed_paths"], ["created.txt"])
            self.assertEqual(worker._execution_delivery.read_only_turns, 0)
            self.assertFalse(result.validation.passed)

    def test_shell_read_does_not_count_as_file_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = TreeLoopPlannerWorker(model=RecordingMessageModel([shell_write("print('read only')")]),
                                           root=Path(tmp), max_turns=1, checkpoint_interval=0, verbose=False)
            worker.loop.allow_shell = True
            worker.run_task("Inspect")
            self.assertFalse(worker._has_mutation)
            self.assertEqual(worker.history[0].results[0].tool_action["_changed_paths"], [])
            self.assertEqual(worker._execution_delivery.read_only_turns, 1)
            self.assertEqual(worker.loop._total_writes, 0)

    def test_timeout_still_records_partial_content_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loop = TreeLoop(model=RecordingMessageModel([]), workspace_root=root, verbose=False, allow_shell=True)
            def timeout(*args, **kwargs):
                (root / "partial.txt").write_text("saved before timeout")
                raise subprocess.TimeoutExpired("fixture", 60)
            action = {"type": "run_shell", "command": "fixture"}
            with patch("tree_loop.subprocess.run", side_effect=timeout):
                output = loop._exec_run_shell(action)
            self.assertIn("timed out", output)
            self.assertEqual(action["_changed_paths"], ["partial.txt"])
            self.assertTrue(action["_change_accounting"]["complete"])

    def test_planner_uses_observed_changes_for_shell_and_does_not_accept_known_noops(self):
        for action, expected in (
            ({"type": "run_shell", "_changed_paths": ["package.json"]}, True),
            ({"type": "run_shell", "_changed_paths": []}, False),
            ({"type": "npm_command", "_changed_paths": []}, False),
            ({"type": "replace_regex", "path": "app.ts"}, True),
        ):
            with self.subTest(action=action):
                turn = SimpleNamespace(results=[CommandResult(True, "ok", "mutation", tool_action=action)])
                self.assertEqual(PlannerAgent._history_has_successful_workspace_mutation([turn]), expected)

    def test_approved_dependency_command_is_observed_and_requires_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"dependencies":{}}\n')
            worker = TreeLoopPlannerWorker(model=RecordingMessageModel(["cat /repo/package.json\nfinish Verified"]),
                                           root=root, max_turns=1, checkpoint_interval=0, verbose=False)
            worker._pending_npm_command = {"command": "install fixture", "path": "package.json"}
            def install(*args, **kwargs):
                (root / "package.json").write_text('{"dependencies":{"fixture":"1.0.0"}}\n')
                (root / "package-lock.json").write_text("lock fixture")
                return subprocess.CompletedProcess(args[0], 0, "installed", "")
            with patch("tree_loop.subprocess.run", side_effect=install):
                approval = worker.execute_operator_action({"type": "approve_npm_command"})
            self.assertTrue(approval.ok)
            self.assertTrue(worker._has_mutation)
            self.assertFalse(worker._validation_after_mutation)
            self.assertEqual(worker.history[-1].results[0].tool_action["_changed_paths"], ["package-lock.json", "package.json"])
            self.assertTrue(worker.run_task("Verify the approved installation").ok)
            prompt = "\n".join(item["content"] for item in worker.model.calls[0])
            self.assertIn("Verify the approved installation", prompt)


class ExplicitDependencyReadsTests(unittest.TestCase):
    def test_explicit_dependency_reads_refresh_but_broad_search_excludes_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dependency = root / "node_modules" / "muxer" / "build" / "index.d.ts"
            dependency.parent.mkdir(parents=True)
            dependency.write_text("export type DependencySentinel = string;\n")
            (root / "app.ts").write_text("const app = true;\n")
            tree = ContextTree(root)
            tree.index_repo()
            path = "/repo/node_modules/muxer/build/index.d.ts"
            self.assertIn("DependencySentinel", tree.cat(path))
            self.assertIn("DependencySentinel", tree.read_line_range(path, 1, 1))
            dependency.write_text("export type UpdatedDependency = number;\n")
            self.assertIn("UpdatedDependency", tree.cat(path))
            self.assertIn("index.d.ts", [entry["name"] for entry in tree.ls("/repo/node_modules/muxer/build")])
            self.assertFalse(any("node_modules" in path for path, _ in tree._iter_repo_entries("/repo")))

    def test_explicit_reads_keep_containment_and_git_protection(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "escape").symlink_to(Path(outside), target_is_directory=True)
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("private metadata")
            (root / "alias").symlink_to(root / ".git", target_is_directory=True)
            tree = ContextTree(root)
            for path in ("/repo/node_modules/escape/secret.txt", "/repo/../secret.txt", "/repo/.git/config", "/repo/alias/config"):
                self.assertIsNone(tree._resolve_repo_path(path), path)

    def test_dependency_packets_survive_compaction_and_are_dropped_after_manifest_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = root / "node_modules" / "pkg" / "build" / "index.d.ts"
            declaration.parent.mkdir(parents=True)
            declaration.write_text("export type RetainedDependency = string;\n")
            (root / "package.json").write_text("{}")
            install = shell_write("from pathlib import Path; Path('package.json').write_text('{\"changed\":true}')")
            model = RecordingMessageModel([
                "read-line-range /repo/node_modules/pkg/build/index.d.ts 1-1", install, "finish done",
            ])
            loop = TreeLoop(model=model, workspace_root=root, max_turns=3, verbose=False, allow_shell=True)
            loop._conversation_char_budget = 1
            self.assertTrue(loop.run("Read a dependency then install").finished)
            self.assertIn("RetainedDependency", "\n".join(message["content"] for message in model.calls[1]))
            self.assertNotIn("RetainedDependency", "\n".join(message["content"] for message in model.calls[2]))


class EmptyProviderRecoveryTests(unittest.TestCase):
    def test_empty_response_preserves_source_without_a_format_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("RETAIN_THIS_SOURCE\n")
            model = RecordingMessageModel(["cat /repo/note.txt", "  \n", "finish recovered"])
            events = []
            loop = TreeLoop(model=model, workspace_root=root, max_turns=2, verbose=False,
                            model_event_observer=events.append)
            result = loop.run("Inspect source")
            self.assertTrue(result.finished)
            self.assertEqual(len(result.turns), 2)
            self.assertEqual(len(model.calls), 3)
            retry = "\n".join(item["content"] for item in model.calls[2])
            self.assertIn("RETAIN_THIS_SOURCE", retry)
            self.assertIn("single empty-response retry", retry)
            self.assertFalse(any(event.get("repair_pending") for event in events))
            self.assertEqual(sum(event["event"] == "model_call_finish" for event in events), 3)

    def test_retry_keeps_export_range_when_same_goal_reads_exceed_48k(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "motion").mkdir(parents=True)
            (root / "src" / "motion" / "export.ts").write_text(
                "\n".join(f"export_anchor_{n} " + "x" * 65 for n in range(1, 301))
            )
            for name in ("encoder", "muxer"):
                dependency = root / "node_modules" / name / "index.d.ts"
                dependency.parent.mkdir(parents=True)
                dependency.write_text("\n".join(f"{name}_type_{n} " + "x" * 65 for n in range(1, 241)))
            model = RecordingMessageModel([
                "read-line-range /repo/src/motion/export.ts 81-280\n"
                "read-line-range /repo/node_modules/encoder/index.d.ts 1-240\n"
                "read-line-range /repo/node_modules/muxer/index.d.ts 1-240",
                "", "finish Inspected export and dependencies",
            ])
            replacements = []
            loop = TreeLoop(model=model, workspace_root=root, max_turns=2, verbose=False,
                            read_context_observer=lambda packets: replacements.append(list(packets)))
            result = loop.run("Inspect the existing MP4 export surface")
            self.assertTrue(result.finished)
            self.assertGreater(loop._read_working_set.manifest()["used_chars"], 48000)
            self.assertEqual(loop._conversation_compaction_count, 0)
            self.assertEqual(replacements, [])
            for call in model.calls[1:]:
                text = "\n".join(message["content"] for message in call)
                self.assertIn("export_anchor_81 ", text)
                self.assertIn("export_anchor_280 ", text)
                self.assertIn("encoder_type_240 ", text)
                self.assertIn("muxer_type_240 ", text)

    def test_malformed_function_calls_recover_in_place_and_continue_with_a_verified_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("original\n")
            model = RecordingMessageModel([
                "cat /repo/note.txt", "", "write unexpected.txt partial-provider-output",
                "replace-lines note.txt:1-1 fixed", "cat /repo/note.txt\nfinish Updated",
            ])
            reasons = ["STOP", "MALFORMED_FUNCTION_CALL", "MALFORMED_FUNCTION_CALL", "STOP", "STOP"]
            model.get_last_metrics = lambda: {
                "response": {"candidates": [{"finish_reason": reasons[len(model.calls) - 1]}]},
                "usage": {"input_tokens": 20, "output_tokens": 5},
            }
            with patch.dict(os.environ, {"SKILLZ_OBSERVABILITY_PATH": str(root / "trace.md")}):
                worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=3, checkpoint_interval=0, verbose=False)
                result = worker.run_task("Update the note")
            self.assertTrue(result.ok, result.final_message)
            self.assertEqual((root / "note.txt").read_text(), "fixed\n")
            self.assertFalse((root / "unexpected.txt").exists())
            self.assertEqual(len(model.calls), 5)
            self.assertEqual(len(worker.history), 3)
            self.assertEqual(worker.loop._conversation_compaction_count, 0)
            self.assertEqual(worker._execution_delivery.read_only_turns, 0)
            self.assertEqual(worker._observability_metrics["provider_protocol_failures"], 2)
            self.assertEqual(worker.usage_accounting_state()["current_run"]["model_calls"], 5)
            self.assertIn("Provider protocol recovery", (root / "trace.md").read_text())
            retry = "\n".join(message["content"] for message in model.calls[2])
            self.assertIn("original", retry)
            self.assertIn("one small executable text command", retry)
            self.assertNotIn("partial-provider-output", retry)

    def test_generic_empty_retry_does_not_spend_the_targeted_protocol_allowance(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = RecordingMessageModel(["", "", "", "finish Recovered"])
            reasons = ["STOP", "MALFORMED_FUNCTION_CALL", "MALFORMED_FUNCTION_CALL", "STOP"]
            model.get_last_metrics = lambda: {"response": {"candidates": [{"finish_reason": reasons[len(model.calls) - 1]}]}}
            loop = TreeLoop(model=model, workspace_root=Path(tmp), max_turns=1, verbose=False)
            self.assertTrue(loop.run("Complete the current answer").finished)
            self.assertEqual(len(model.calls), 4)
            self.assertEqual(len(loop.history), 1)
            self.assertEqual(loop._conversation_compaction_count, 0)

    def test_malformed_response_can_recover_after_a_blank_reply_and_validated_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("original\n")
            model = RecordingMessageModel([
                "cat /repo/note.txt", "replace-lines note.txt:1-1 validated_change", "cat /repo/note.txt",
                "", "", "finish Updated",
            ])
            reasons = ["STOP", "STOP", "STOP", "STOP", "MALFORMED_RESPONSE", "STOP"]
            model.get_last_metrics = lambda: {"response": {"candidates": [{"finish_reason": reasons[len(model.calls) - 1]}]}}
            trace = root / "trace.md"
            with patch.dict(os.environ, {"SKILLZ_OBSERVABILITY_PATH": str(trace)}):
                worker = TreeLoopPlannerWorker(model=model, root=root, max_turns=4, checkpoint_interval=0, verbose=False)
                result = worker.run_task("Update the note")
            self.assertTrue(result.ok, result.final_message)
            self.assertTrue(result.validation.passed)
            self.assertEqual((root / "note.txt").read_text(), "validated_change\n")
            self.assertEqual(len(worker.history), 4)
            self.assertEqual(worker.loop._conversation_compaction_count, 0)
            retry = "\n".join(message["content"] for message in model.calls[-1])
            self.assertIn("validated_change", retry)
            self.assertIn("MALFORMED_RESPONSE", retry)
            self.assertIn("one small executable text command", retry)
            self.assertEqual(worker._observability_metrics["provider_protocol_failures"], 1)
            self.assertEqual(worker.usage_accounting_state()["current_run"]["model_calls"], 6)

    def test_failed_malformed_response_repair_keeps_the_provider_reason(self):
        for reasons, detail in (
            (["STOP", "MALFORMED_RESPONSE", "MALFORMED_RESPONSE", "MALFORMED_RESPONSE"], "after 2 targeted retries"),
            (["STOP", "MALFORMED_RESPONSE", "STOP"], "the last retry returned no usable text"),
        ):
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as tmp:
                model = RecordingMessageModel([""] * len(reasons))
                model.get_last_metrics = lambda: {"response": {"candidates": [{"finish_reason": reasons[len(model.calls) - 1]}]}}
                loop = TreeLoop(model=model, workspace_root=Path(tmp), max_turns=4, verbose=False)
                result = loop.run("Current goal")
                self.assertFalse(result.finished)
                self.assertEqual(len(model.calls), len(reasons))
                self.assertEqual(loop.history, [])
                self.assertEqual(loop._conversation_compaction_count, 0)
                self.assertIn("failed with MALFORMED_RESPONSE", result.finish_message)
                self.assertIn(detail, result.finish_message)

    def test_persistent_protocol_failure_stays_bounded_with_source_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = RecordingMessageModel(["", "", ""])
            model.get_last_metrics = lambda: {"response": {"candidates": [{"finish_reason": "MALFORMED_FUNCTION_CALL"}]}}
            loop = TreeLoop(model=model, workspace_root=Path(tmp), max_turns=12, verbose=False)
            result = loop.run("Current goal")
            self.assertFalse(result.finished)
            self.assertEqual(len(model.calls), 3)
            self.assertEqual(loop.history, [])
            self.assertEqual(loop._conversation_compaction_count, 0)
            self.assertIn("MALFORMED_FUNCTION_CALL after 2 targeted retries", result.finish_message)
            self.assertEqual(loop._conversation_task, "Current goal")

    def test_two_empty_responses_stop_as_provider_failure_with_exact_usage_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = RecordingMessageModel(["", ""])
            model.get_last_metrics = lambda: {"response": {"candidates": [{"finish_reason": "MAX_TOKENS"}]},
                                               "usage": {"input_tokens": 20, "output_tokens": 5}}
            with patch.dict(os.environ, {"SKILLZ_OBSERVABILITY_PATH": str(Path(tmp) / "trace.md")}):
                worker = TreeLoopPlannerWorker(model=model, root=Path(tmp), max_turns=12, checkpoint_interval=0, verbose=False)
                result = worker.run_task("Current goal")
            self.assertFalse(result.ok)
            self.assertEqual(len(model.calls), 2)
            self.assertEqual(worker.history, [])
            self.assertEqual(worker._execution_delivery.read_only_turns, 0)
            self.assertIn("provider returned empty text twice", result.final_message)
            self.assertIn("MAX_TOKENS", result.final_message)
            trace = (Path(tmp) / "trace.md").read_text()
            self.assertIn("Empty provider response", trace)
            self.assertNotIn("## Model output format repair", trace)
            self.assertEqual(worker._observability_metrics["empty_provider_responses"], 2)
            usage = worker.usage_accounting_state()
            self.assertEqual(usage["current_run"]["model_calls"], 2)
            self.assertEqual(usage["current_run"]["input_tokens"], 40)

    def test_format_and_empty_recovery_have_separate_bounded_allowances(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = RecordingMessageModel(["", "I will inspect next", ""])
            loop = TreeLoop(model=model, workspace_root=Path(tmp), max_turns=12, verbose=False)
            result = loop.run("Inspect")
            self.assertFalse(result.finished)
            self.assertEqual(len(model.calls), 3)
            self.assertEqual(result.turns, [])
            self.assertIn("empty text twice", result.finish_message)

    def test_gemini_records_finish_and_block_metadata_without_part_content(self):
        client = GeminiModelClient.__new__(GeminiModelClient)
        client.model = "fixture"
        response = SimpleNamespace(text="HIDDEN_REASONING", response_id="response-123", usage_metadata=None,
                                   prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
                                   candidates=[SimpleNamespace(finish_reason="MAX_TOKENS", content=SimpleNamespace(parts=[
                                       SimpleNamespace(thought=True, text="HIDDEN_REASONING"),
                                       SimpleNamespace(function_call=SimpleNamespace(name="tool")),
                                   ]))])
        client._record_gemini_metrics(response)
        metrics = client.get_last_metrics()["response"]
        self.assertEqual(metrics["response_id"], "response-123")
        self.assertEqual(metrics["block_reason"], "SAFETY")
        self.assertEqual(metrics["candidates"][0]["part_types"], ["thought", "function_call"])
        self.assertNotIn("HIDDEN_REASONING", json.dumps(metrics))
        self.assertEqual(client._gemini_response_text(response), "")


if __name__ == "__main__":
    unittest.main()
