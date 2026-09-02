from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from context_tree import ContextTree
from discovery import find_symbol_definitions, outline_file, read_symbol, repo_map
from discovery.dispatch import execute_discovery_action
from tree_commands import TreeCommandParser


class WorkspaceDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        (self.root / "README.md").write_text("# Workspace\n", encoding="utf-8")
        self.component = self.root / "src/components/VariationsPanel.tsx"
        self.component.parent.mkdir(parents=True)
        self.component.write_text(
            "export const VariationsPanel: React.FC<PanelProps> = (props) => {\n"
            "  return null;\n"
            "};\n",
            encoding="utf-8",
        )
        hook = self.root / "src/assistant/useAssistantConversation.ts"
        hook.parent.mkdir(parents=True)
        hook.write_text("export const useAssistantConversation = () => {};\n", encoding="utf-8")
        snapshot = self.root / "logs/diagnostics.txt"
        snapshot.parent.mkdir()
        snapshot.write_text(
            "src/components/VariationsPanel.tsx(1,1): error TS2304: Cannot find name 'missing'.\n",
            encoding="utf-8",
        )

    def cold_tree(self) -> ContextTree:
        tree = ContextTree(self.root)
        self.assertEqual(tree.index_repo(max_files=1), 1)
        return tree

    def test_discovery_command_matrix_is_independent_of_initial_index(self) -> None:
        cases = [
            ('find /repo -name "useAssistantConversation*" limit=20', "src/assistant/useAssistantConversation.ts"),
            ("find-symbol /repo VariationsPanel", '"name": "VariationsPanel"'),
            ("find_symbol /repo VariationsPanel", '"name": "VariationsPanel"'),
            ('grep /repo "React.FC" limit=20', "src/components/VariationsPanel.tsx:1:"),
            ("grep /repo 'React.FC' limit=20", "src/components/VariationsPanel.tsx:1:"),
            ("symbols /repo/src/components/VariationsPanel.tsx", '"name": "VariationsPanel"'),
            ("ls /repo/src depth=2", "components/VariationsPanel.tsx"),
            ("stat /repo/src/components/VariationsPanel.tsx", '"type": "file"'),
            ("cat /repo/src/components/VariationsPanel.tsx:1-2", "React.FC<PanelProps>"),
            ("read-line-range /repo/src/components/VariationsPanel.tsx 1-2", "React.FC<PanelProps>"),
            ('repo-map /repo topic="VariationsPanel" limit=5', '"name": "VariationsPanel"'),
            ("read-diagnostics /repo/logs/diagnostics.txt", "ingested 1 issue(s)"),
            ("ingest-log /repo/logs/diagnostics.txt", "Ingested 1 issue(s)"),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                # Fresh index for every command: an earlier read must not prime the test.
                result = TreeCommandParser(self.cold_tree()).parse_and_execute(command)
                self.assertTrue(result.ok, result.output)
                self.assertIn(expected, result.output)

    def test_shared_discovery_scanner_recognizes_typed_components(self) -> None:
        path = "src/components/VariationsPanel.tsx"
        definitions = find_symbol_definitions("VariationsPanel", root=self.root)
        outline = outline_file(path, root=self.root)
        body = read_symbol(path, "VariationsPanel", root=self.root)
        mapping = repo_map(topic="VariationsPanel", root=self.root)
        self.assertEqual(definitions["hits"][0]["file_path"], path)
        self.assertIn("VariationsPanel", {symbol["name"] for symbol in outline["symbols"]})
        self.assertTrue(body["ok"])
        self.assertTrue(any(symbol["name"] == "VariationsPanel" for symbol in mapping["top_symbols"]))

    def test_discovery_action_matrix_uses_host_workspace(self) -> None:
        path = "/repo/src/components/VariationsPanel.tsx"
        actions = [
            {"type": "list_files", "path": "/repo/src"},
            {"type": "find_files", "patterns": ["useAssistantConversation*"]},
            {"type": "find_files", "glob": "*useAssistantConversation*", "hidden": False},
            {"type": "search_in_files", "query": "VariationsPanel"},
            {"type": "read_file", "path": path, "start_line": 1, "end_line": 2},
            {"type": "outline_file", "path": path},
            {"type": "read_symbol", "path": path, "symbol_name": "VariationsPanel"},
            {"type": "find_symbol_definitions", "symbol_name": "VariationsPanel"},
            {"type": "find_symbol_references", "symbol_name": "VariationsPanel"},
            {"type": "trace_dependencies", "path": path},
            {"type": "find_related_files", "path": path},
            {"type": "find_related_tests", "target": "VariationsPanel"},
            {"type": "find_related_tests", "path": path},
            {"type": "find_related_configs", "target": "VariationsPanel"},
            {"type": "find_canonical_implementation", "topic": "VariationsPanel"},
            {"type": "find_similar_code", "snippet": "VariationsPanel"},
            {"type": "find_entry_points"},
            {"type": "find_ownership", "target": path},
            {"type": "semantic_search", "intent": "VariationsPanel"},
            {"type": "repo_map", "topic": "VariationsPanel"},
            {"type": "investigate", "topic": "VariationsPanel"},
        ]
        for action in actions:
            with self.subTest(action=action["type"]):
                result = execute_discovery_action(action, root=self.root)
                self.assertTrue(result["ok"], result)

    def test_discovery_dispatch_rejects_mutations_and_root_overrides(self) -> None:
        for action in [
            {"type": "write_file", "path": "bad.txt", "content": "no"},
            {"type": "list_files", "root": "/"},
            {"type": "list_files", "path": "/etc"},
            {"type": "list_files", "path": "/repo/../"},
        ]:
            with self.subTest(action=action):
                self.assertFalse(execute_discovery_action(action, root=self.root)["ok"])
        parser_result = TreeCommandParser(self.cold_tree()).parse_and_execute(
            'discover {"type":"write_file","path":"bad.txt","content":"no"}'
        )
        self.assertFalse(parser_result.ok)
        self.assertFalse((self.root / "bad.txt").exists())

    def test_hot_file_preload_does_not_require_index_membership(self) -> None:
        tree = self.cold_tree()
        self.assertEqual(tree.preload_files(["src/components/VariationsPanel.tsx"]), 1)
        self.assertIn("VariationsPanel", tree.render_hot_files_block(["src/components/VariationsPanel.tsx"]))

    def test_reads_and_searches_see_external_edits_and_deletes(self) -> None:
        tree = self.cold_tree()
        path = "/repo/src/components/VariationsPanel.tsx"
        self.assertIn("VariationsPanel", tree.cat(path))
        self.component.write_text("export const UpdatedPanel = () => null;\n", encoding="utf-8")
        self.assertIn("UpdatedPanel", tree.cat(path))
        self.assertEqual(tree.extract_symbols(path)[0]["name"], "UpdatedPanel")
        self.assertTrue(tree.grep("/repo/src", "UpdatedPanel"))
        self.assertFalse(tree.find_symbols("/repo/src", "VariationsPanel"))
        self.component.unlink()
        self.assertIn("not found", tree.cat(path))
        self.assertEqual(tree.find("/repo", "VariationsPanel*"), [])
        self.assertEqual(tree.ls("/repo/src/components"), [])

    def test_workspace_searches_respect_limits_and_explicit_scope(self) -> None:
        tree = self.cold_tree()
        self.assertEqual(len(tree.find("/repo", "*.ts*", limit=1)), 1)
        self.assertEqual(len(tree.grep("/repo/src", "export", limit=1)), 1)
        self.assertEqual(tree.find("/repo/src/assistant", "VariationsPanel*"), [])
        self.assertEqual(tree.find_symbols("/repo/src/assistant", "VariationsPanel"), [])
        self.assertEqual(tree.grep("/repo/src/assistant", "VariationsPanel"), [])

    def test_workspace_searches_do_not_read_excluded_or_external_files(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        external = Path(outside.name) / "secret.py"
        external.write_text("def HiddenMarker(): pass\n", encoding="utf-8")
        (self.root / "secret.py").symlink_to(external)
        for folder in ("node_modules", ".git"):
            excluded = self.root / folder
            excluded.mkdir()
            (excluded / "secret.py").write_text("def HiddenMarker(): pass\n", encoding="utf-8")
        tree = ContextTree(self.root)
        tree.index_repo()  # The boundary must hold even when a path could be indexed.
        self.assertEqual(tree.find("/repo", "secret.py"), [])
        self.assertEqual(tree.grep("/repo", "HiddenMarker"), [])
        self.assertEqual(tree.find_symbols("/repo", "HiddenMarker"), [])
        self.assertIn("not found", tree.cat("/repo/secret.py"))
        self.assertEqual(tree.grep("/repo/../", "HiddenMarker"), [])
        for action in [
            {"type": "find_files", "patterns": ["secret.py"]},
            {"type": "search_in_files", "query": "HiddenMarker"},
            {"type": "find_symbol_definitions", "symbol_name": "HiddenMarker"},
        ]:
            with self.subTest(action=action["type"]):
                result = execute_discovery_action(action, root=self.root)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["hits"], [])

    def test_other_virtual_mounts_stay_in_memory(self) -> None:
        tree = self.cold_tree()
        tree.set_fact("issue", "architecture", "db", "postgres")
        self.assertIn("facts/", {entry["name"] for entry in tree.ls("/")})
        self.assertEqual(tree.cat("/facts/issue/architecture/db").strip(), "1 | postgres")
        self.assertEqual(tree.grep("/facts", "postgres")[0]["path"], "facts/issue/architecture/db")


if __name__ == "__main__":
    unittest.main()
