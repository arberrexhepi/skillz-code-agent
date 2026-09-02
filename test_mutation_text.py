from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from context_tree import ContextTree
from tree_commands import TreeCommandParser, execute_multi, execute_strategy, parse_strategy
from tree_loop import TreeLoop, extract_commands


class MutationTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.parser = TreeCommandParser(ContextTree(self.root))

    def parse(self, command):
        result = self.parser.parse_and_execute(command)
        self.assertTrue(result.ok, result.output)
        return result.tool_action

    def through_pipeline(self, command, *, strategy=False):
        extracted = extract_commands(command)
        if strategy:
            results = [result for group in execute_strategy(self.parser, extracted).values() for result in group]
        else:
            results, _ = execute_multi(self.parser, extracted)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok, results[0].output)
        return results[0].tool_action

    def test_json_patch_operands_decode_once_and_preserve_literal_source(self):
        cases = [
            ('  readonly onOpenBrand: () => void;\n  readonly onOpenIntegration: () => void;', '  readonly onOpenGuidelines?: () => void;'),
            ('const label = "A -> B";', 'const label = "B -> C";'),
            ('\\n is two characters', '\\t stays literal'),
            ('const regex = /\\s+\\d/;', 'const path = "C:\\new\\file";'),
            ('  café 🪴\t ', '\n  replacement\n'),
            ('old', ''),
            ('"old"', '"new"'),
        ]
        for old, new in cases:
            for strategy in (False, True):
                with self.subTest(old=old, strategy=strategy):
                    command = 'patch /repo/example.txt ' + json.dumps(old) + ' -> ' + json.dumps(new)
                    action = self.through_pipeline(('s1: ' if strategy else '') + command, strategy=strategy)
                    self.assertEqual(action['search'], old)
                    self.assertEqual(action['replace'], new)

    def test_single_quoted_patch_operands(self):
        action = self.parse(r"patch /repo/example.txt '  old\nline' -> '  new\nline'")
        self.assertEqual(action['search'], '  old\nline')
        self.assertEqual(action['replace'], '  new\nline')

    def test_raw_patch_does_not_decode_code_strings_or_strip_whitespace(self):
        for old, new in [
            ('  const x = 1; ', '  const x = 2;  '),
            ('const label = "A -> B";', 'const label = "A to B";'),
            (r'const regex = /\n/;', r'const regex = /\t/;'),
            ('"old" + suffix', '"new" + suffix'),
        ]:
            with self.subTest(old=old):
                action = self.through_pipeline(f'patch /repo/example.txt {old} -> {new}')
                self.assertEqual(action['search'], old)
                self.assertEqual(action['replace'], new)

    def test_invalid_or_ambiguous_patch_operands_fail_without_dispatch(self):
        for payload in [
            '"unterminated -> "new"',
            'old -> middle -> new',
            r'"bad\q" -> "new"',
            '"" -> "new"',
            'old new',
        ]:
            with self.subTest(payload=payload):
                result = self.parser.parse_and_execute('patch /repo/example.txt ' + payload)
                self.assertFalse(result.ok)
                self.assertFalse(result.needs_tool)

    def test_inline_payload_whitespace_survives_all_stages(self):
        for header in (
            'write /repo/example.txt',
            'replace-lines /repo/example.txt:1-2',
            'replace-lines /repo/example.txt 1-2',
            'replace_lines /repo/example.txt 1 2',
        ):
            for strategy in (False, True):
                with self.subTest(header=header, strategy=strategy):
                    content = '\t  const values = first, second;  '
                    action = self.through_pipeline(('s1: ' if strategy else '') + header + ' ' + content, strategy=strategy)
                    self.assertEqual(action['content'], content)

    def test_heredocs_preserve_indentation_blank_lines_fences_and_command_like_text(self):
        for header in ('write /repo/example.txt', 'replace-lines /repo/example.txt:1-2'):
            for content in ('  first\n\tsecond  \n', '\n  first\n\n', '  first, second -> s2', '', '  ```ts\ncat /repo/not-a-command\ns1: cat /repo/not-a-step\n# comment\n```'):
                for strategy in (False, True):
                    with self.subTest(header=header, content=content, strategy=strategy):
                        command = ('s1: ' if strategy else '') + header + ' <<<\n' + content + '\n>>>'
                        action = self.through_pipeline(command, strategy=strategy)
                        self.assertEqual(action['content'], content)

    def test_write_and_replace_do_not_decode_quoted_source(self):
        for header in ('write /repo/example.txt', 'replace-lines /repo/example.txt:1-1'):
            content = r'"use strict"; // literal \n'
            self.assertEqual(self.through_pipeline(header + ' ' + content)['content'], content)

    def test_unterminated_heredoc_rejects_the_entire_batch(self):
        for strategy in (False, True):
            with self.subTest(strategy=strategy):
                raw = ('s1: ' if strategy else '') + 'write /repo/first.txt first\n'
                raw += ('s2: ' if strategy else '') + 'write /repo/second.txt <<<\n  incomplete\n'
                extracted = extract_commands(raw)
                if strategy:
                    results = [result for group in execute_strategy(self.parser, extracted).values() for result in group]
                else:
                    results, _ = execute_multi(self.parser, extracted)
                self.assertEqual(len(results), 1)
                self.assertFalse(results[0].ok)
                self.assertFalse(results[0].needs_tool)
                self.assertIn('Unterminated heredoc', results[0].output)

    def test_mutation_strategy_payload_does_not_become_dependency_or_parallel_command(self):
        raw = 's1: write /repo/example.txt text -> s2\ns1 -> s2: cat /repo/example.txt'
        plan = parse_strategy(raw)
        self.assertEqual(plan.steps['s1'].commands, ['write /repo/example.txt text -> s2'])
        self.assertEqual(plan.steps['s2'].depends_on, ['s1'])
        self.assertEqual(plan.steps['s1'].targets, [])

    def test_mutation_pipeline_writes_exact_bytes_on_disk(self):
        # Host diagnostics are unrelated to text transport; avoid launching a compiler.
        loop = TreeLoop(model=None, workspace_root=self.root, verbose=False)
        loop.setup()
        loop._append_backend_diagnostics = lambda message, *args, **kwargs: message
        target = self.root / 'example.txt'
        original = '  readonly onOpenBrand: () => void;\n  readonly onOpenIntegration: () => void;\n'
        target.write_text(original)
        loop.bridge.tree.index_repo(max_files=0)
        new = original.replace('onOpenBrand', 'onOpenGuidelines')
        command = 'patch /repo/example.txt ' + json.dumps(original) + ' -> ' + json.dumps(new)
        result = self.parser.parse_and_execute(command)
        message = loop._execute_tool(result)
        self.assertTrue(result.ok, message)
        self.assertEqual(target.read_text(), new)
        for command, expected in [
            ('replace-lines /repo/example.txt:1-1   indented  ', '  indented  \n  readonly onOpenIntegration: () => void;\n'),
            ('write /repo/example.txt <<<\n\n  first\n\tsecond  \n\n>>>', '\n  first\n\tsecond  \n'),
        ]:
            results, _ = execute_multi(self.parser, extract_commands(command))
            message = loop._execute_tool(results[0])
            self.assertTrue(results[0].ok, message)
            self.assertEqual(target.read_text(), expected)

    def test_missing_and_duplicate_exact_matches_leave_file_unchanged(self):
        loop = TreeLoop(model=None, workspace_root=self.root, verbose=False)
        loop.setup()
        target = self.root / 'example.txt'
        for original, search, expected_error in [('old old\n', 'old', 'ambiguous'), ('old\n', 'OLD', 'search text not found')]:
            with self.subTest(original=original):
                target.write_text(original)
                result = self.parser.parse_and_execute('patch /repo/example.txt ' + json.dumps(search) + ' -> "new"')
                message = loop._execute_tool(result)
                self.assertFalse(result.ok)
                self.assertIn(expected_error, message)
                self.assertEqual(target.read_text(), original)

    def test_raw_and_planner_backed_loops_land_identical_payloads(self):
        from live_test_loop import TreeLoopPlannerWorker
        from test_tree_loop_messages import RecordingMessageModel

        for planner_backed in (False, True):
            with self.subTest(planner_backed=planner_backed):
                worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
                worker.root = self.root
                worker.discovery_budget = None
                worker._patch_resolution_blocks_action = lambda action_type: None
                worker._discovery_remediation_blocks_action = lambda action_type: None
                old = '  first\n  second'
                new = '  first -> next\n  second'
                model = RecordingMessageModel([
                    'write /repo/runtime.txt <<<\n' + old + '\n>>>',
                    's1: patch /repo/runtime.txt ' + json.dumps(old) + ' -> ' + json.dumps(new),
                    's1: replace-lines /repo/runtime.txt:2-2 <<<\n\t  last, literal -> s2  \n>>>',
                ])
                loop = TreeLoop(
                    model=model, workspace_root=self.root, verbose=False, max_turns=3,
                    tool_dispatcher=worker._dispatch_tool_action if planner_backed else None,
                )
                worker.loop = loop
                loop._append_backend_diagnostics = lambda message, *args, **kwargs: message
                run = loop.run('Apply the three edits')
                self.assertEqual(len(run.turns), 3)
                for turn in run.turns:
                    for result in turn.results:
                        self.assertTrue(result.ok, result.output)
                self.assertEqual((self.root / 'runtime.txt').read_text(), '  first -> next\n\t  last, literal -> s2  ')

    def test_runtime_never_dispatches_writes_from_an_unterminated_batch(self):
        from test_tree_loop_messages import RecordingMessageModel

        model = RecordingMessageModel([
            's1: write /repo/first.txt first\ns2: write /repo/second.txt <<<\n  incomplete',
        ])
        loop = TreeLoop(model=model, workspace_root=self.root, verbose=False, max_turns=1)
        run = loop.run('Apply the edits')
        self.assertFalse((self.root / 'first.txt').exists())
        self.assertFalse((self.root / 'second.txt').exists())
        self.assertFalse(run.turns[0].results[0].ok)


if __name__ == '__main__':
    unittest.main()
