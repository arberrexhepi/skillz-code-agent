from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from context_tree import ContextTree
from discovery.dispatch import execute_discovery_action
from live_test_loop import TreeLoopPlannerWorker
from tests.test_live_test_loop import FakeModelClient
from tree_commands import TreeCommandParser, parse_strategy
from tree_loop import TreeLoop, extract_commands


class InspectionCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "app.py").write_text("def target():\n    return 1\n\nvalue = target()\n", encoding="utf-8")
        self.loop = TreeLoop(model=None, workspace_root=self.root, verbose=False)
        self.loop.setup()

    def execute(self, command):
        result = self.loop.bridge.execute_single(command)
        self.assertTrue(result.ok, result.output)
        output = self.loop._execute_tool(result)
        return result, output

    def test_inspect_symbol_and_references_are_executable_commands(self):
        for command in ["inspect-file /repo/app.py", "read-symbol /repo/app.py target", "references target path=/repo"]:
            self.assertEqual(extract_commands(command), command)
            self.assertTrue(self.loop.bridge.is_tree_command(command))
            result, output = self.execute(command)
            self.assertTrue(result.ok, output)
            self.assertIn("target", output)
        _, output = self.execute("inspect-file /repo/app.py")
        self.assertEqual(json.loads(output)["file_hash"], hashlib.sha256((self.root / "app.py").read_bytes()).hexdigest())

    def test_context_merges_overlapping_matches_and_is_bounded(self):
        (self.root / "large.py").write_text("\n".join(f"# anchor line {i}" for i in range(600)))
        _, output = self.execute('read-context /repo/large.py "anchor" before=80 after=80 limit=20')
        payload = json.loads(output)
        self.assertEqual(len(payload["blocks"]), 1)
        self.assertEqual(payload["match_count"], 600)
        self.assertTrue(payload["truncated"])
        self.assertLessEqual(sum(len(block["content"].splitlines()) for block in payload["blocks"]), 300)

    def test_inspection_rejects_traversal_and_external_symlinks(self):
        outside = self.root.parent / (self.root.name + "-outside.py")
        outside.write_text("secret = True\n")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        (self.root / "link.py").symlink_to(outside)
        for path in ["/repo/../" + outside.name, "/repo/link.py", "/repo/node_modules/secret.py"]:
            for action in [{"type": "inspect_file", "path": path}, {"type": "read_context", "path": path, "query": "secret"}]:
                self.assertFalse(execute_discovery_action(action, root=self.root)["ok"])

    def test_symbol_read_has_continuation_without_dumping_the_entire_symbol(self):
        (self.root / "long.py").write_text("def large():\n" + "    value = 1\n" * 500)
        _, output = self.execute("read-symbol /repo/long.py large limit=40 offset=20")
        hit = json.loads(output)["hits"][0]
        self.assertEqual(hit["start_line"], 21)
        self.assertEqual(len(hit["details"]["content"].splitlines()), 40)
        self.assertEqual(hit["details"]["next_offset"], 60)

    def test_symbol_edit_uses_hash_and_preserves_neighboring_declarations(self):
        path = self.root / "app.py"
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        command = f"edit-symbol /repo/app.py target hash={file_hash}\n" + "def target():\n    return 2\n"
        with mock.patch.object(self.loop, "_run_backend_diagnostics_for_path", return_value=""):
            result, output = self.execute(command)
        self.assertTrue(result.ok, output)
        self.assertIn("return 2", path.read_text())
        self.assertIn("value = target()", path.read_text())
        result, output = self.execute(command)
        self.assertFalse(result.ok)
        self.assertIn("expected_hash_mismatch", output)

    def test_symbol_edit_rejects_ambiguous_names_and_preserves_decorators(self):
        path = self.root / "app.py"
        path.write_text("@cache\ndef target():\n    return 1\n\nvalue = 1\n")
        with mock.patch.object(self.loop, "_run_backend_diagnostics_for_path", return_value=""):
            self.execute("edit-symbol /repo/app.py target\ndef target():\n    return 2\n")
        self.assertNotIn("@cache", path.read_text())
        path.write_text("def target():\n    pass\ndef target():\n    pass\n")
        before = path.read_text()
        result, _ = self.execute("edit-symbol /repo/app.py target\ndef target():\n    return 3\n")
        self.assertFalse(result.ok)
        self.assertEqual(path.read_text(), before)

    def test_atomic_edit_batch_rolls_back_and_rejects_paths_before_landing(self):
        path = self.root / "app.py"
        before = path.read_text()
        first = {"type": "replace_snippet", "path": "/repo/app.py", "old_text": "return 1", "new_text": "return 2"}
        for second in [{"type": "replace_snippet", "path": "app.py", "old_text": "missing", "new_text": "bad"},
                       {"type": "create_file", "path": "../outside.py", "content": "bad"}]:
            result, output = self.execute("edit-batch " + json.dumps({"operations": [first, second]}))
            self.assertFalse(result.ok, output)
            self.assertEqual(path.read_text(), before)

    def test_large_atomic_edit_batch_rolls_back_when_a_late_edit_fails(self):
        operations = [{"type": "create_file", "path": f"sample-{i}.txt", "content": str(i)} for i in range(34)]
        operations.append({"type": "replace_snippet", "path": "app.py", "old_text": "missing anchor", "new_text": "bad"})
        result, output = self.execute("edit-batch " + json.dumps({"operations": operations}))
        self.assertFalse(result.ok)
        self.assertIn('"rolled_back": true', output)
        self.assertEqual(list(self.root.glob("sample-*.txt")), [])

    def test_edit_symbol_heredoc_preserves_strategy_payload(self):
        raw = "s1: edit-symbol /repo/app.py target <<<\ndef target():\n    return 'literal -> s2, braces {}'\n>>>\ns1 -> s2: read-symbol /repo/app.py target"
        plan = parse_strategy(raw)
        self.assertIn("literal -> s2, braces {}", plan.steps["s1"].commands[0])
        self.assertEqual(plan.steps["s2"].depends_on, ["s1"])

    @unittest.skipUnless(shutil.which("node") and (Path(__file__).parents[1] / "desktop/node_modules/typescript").exists(), "Local TypeScript parser required")
    def test_tsx_symbol_edit_uses_parser_spans_with_braces_inside_strings_and_jsx(self):
        path = self.root / "App.tsx"
        path.write_text('export const Panel: React.FC = () => {\n  const text = "}";\n  return <div>{text}</div>;\n};\nexport const neighbor = 7;\n')
        with mock.patch.object(self.loop, "_run_backend_diagnostics_for_path", return_value=""):
            result, output = self.execute('edit-symbol /repo/App.tsx Panel\nexport const Panel = () => <span>ready</span>;\n')
        self.assertTrue(result.ok, output)
        self.assertEqual(path.read_text(), 'export const Panel = () => <span>ready</span>;\nexport const neighbor = 7;\n')


class BatchDiagnosticsTests(unittest.TestCase):
    def worker(self, root, commands):
        return TreeLoopPlannerWorker(model=FakeModelClient(commands), root=root, max_turns=2,
                                     checkpoint_interval=0, thinking_mode="low", verbose=False)

    def test_batch_defers_diagnostics_until_supporting_edits_have_landed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker = self.worker(root, ["batch start\nwrite /repo/app.py result = helper()\nwrite /repo/helper.py def helper(): return 1\nbatch end\nfinish done"])
            observed = []
            def diagnose(path, **kwargs):
                self.assertTrue((root / "helper.py").exists())
                observed.append(path)
                return ""
            with mock.patch.object(worker.loop, "_run_backend_diagnostics_for_path", side_effect=diagnose):
                result = worker.run_task("Implement a coordinated change")
            self.assertTrue(result.ok)
            self.assertCountEqual(observed, ["app.py", "helper.py"])

    def test_batch_end_surfaces_diagnostics_and_blocks_finish(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = self.worker(Path(temp), ["batch start\nwrite /repo/app.py result = missing\nbatch end\nfinish done"])
            with mock.patch.object(worker.loop, "_run_backend_diagnostics_for_path", return_value="backend_diagnostics engine=python path=app.py issues=1\nundefined missing"):
                result = worker.run_task("Implement an invalid change")
            self.assertFalse(result.ok)
            self.assertFalse(worker.history[0].results[2].ok)
            self.assertIn("undefined missing", worker.history[0].results[2].output)

    def test_structural_edits_use_planner_dispatch_and_batch_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app.py").write_text("def target():\n    return 1\n")
            worker = self.worker(root, ["batch start\nedit-symbol /repo/app.py target <<<\ndef target():\n    return 2\n>>>\nbatch end\nfinish done"])
            with mock.patch.object(worker.loop, "_run_backend_diagnostics_for_path", return_value=""):
                result = worker.run_task("Edit by symbol")
            self.assertTrue(result.ok, result.final_message)
            self.assertIn("return 2", (root / "app.py").read_text())
            self.assertIn("app.py", result.touched_paths)

    def test_structured_reads_work_during_patch_and_diagnostic_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app.py").write_text("def target():\n    return 1\n")
            worker = self.worker(root, [])
            worker.loop.setup()
            worker._patch_resolution = {"path": "app.py"}
            _, results, _ = worker.loop._execute_command_batch("read-symbol /repo/app.py target")
            self.assertTrue(results[0].ok, results[0].output)
            self.assertIsNone(worker._patch_resolution)
            worker._discovery_remediation = {"path": "app.py"}
            _, results, _ = worker.loop._execute_command_batch('read-context /repo/app.py "return"')
            self.assertTrue(results[0].ok, results[0].output)
            self.assertIsNone(worker._discovery_remediation)
