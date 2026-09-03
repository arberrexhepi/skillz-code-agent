from __future__ import annotations

import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_tools
from context_tree import ContextTree
from diagnostics.common import run_command
from discovery.common import run_git as discovery_git
from live_test_loop import TreeLoopPlannerWorker
from main import ToolbeltRunner, run_shell
from tree_loop import TreeLoop

LABEL = 'P\u00ebrsh\u00ebndetje \u010d \u2014 \u4e16\u754c \U0001f680'
ROOT = Path(__file__).resolve().parents[1]


class WindowsTextEncodingTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix='skillz-utf8-')
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve() / 'projekt-\u00eb'
        self.root.mkdir()
        self.file = self.root / 'sh\u00ebnime.txt'
        self.file.write_bytes(LABEL.encode('utf-8'))
        # Force Windows defaults even on macOS/Linux or UTF-8-enabled CI.
        self.enterContext(patch.object(subprocess, '_text_encoding', return_value='cp1252'))
        self.enterContext(patch.dict(os.environ, {'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '0'}))

    def worker(self):
        worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
        worker.root = self.root
        return worker

    def loop(self):
        loop = TreeLoop(model=None, workspace_root=self.root, verbose=False)
        loop.setup()
        loop._append_backend_diagnostics = lambda message, *args, **kwargs: message
        return loop

    def test_stable_and_beta_toolbelt_preserve_unicode_paths_and_content(self):
        stable = ToolbeltRunner(ROOT / 'agent_tools.py', self.root)
        for call in (stable.call, self.worker()._run_toolbelt_command):
            for inherited_encoding in ('utf-8', 'cp1252'):
                with self.subTest(call=call.__qualname__, encoding=inherited_encoding), patch.dict(os.environ, {'PYTHONIOENCODING': inherited_encoding}):
                    result = call('read', '--path', self.file.name)
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(result['data']['content'], LABEL)
                    self.assertEqual(result['data']['path'], self.file.name)

    def test_git_diff_retains_unicode_in_every_runner(self):
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.root, capture_output=True, encoding='utf-8', check=True)
        git('init', '-q')
        git('config', 'core.autocrlf', 'false')
        git('add', '--', self.file.name)
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Initial')
        self.file.write_bytes(('changed ' + LABEL).encode('utf-8'))
        for runner in (agent_tools.run_git, discovery_git):
            with self.subTest(runner=runner.__module__):
                self.assertIn(LABEL, runner(self.root, ['diff']).stdout)
        self.assertIn(LABEL, agent_tools.git_diff(self.root))
        code, stdout, stderr = self.worker()._run_git(['diff'])
        self.assertEqual(code, 0, stderr)
        self.assertIn(LABEL, stdout)

    def test_command_output_uses_utf8_and_tolerates_non_utf8_log_bytes(self):
        # Python subprocesses also need an explicit output encoding outside Electron.
        with patch.dict(os.environ, {'PYTHONIOENCODING': 'cp1252'}):
            result = run_command([sys.executable, '-c', 'print(' + ascii(LABEL) + ')'], cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), LABEL)
        result = run_command([sys.executable, '-c', "import sys; sys.stdout.buffer.write(" + repr(LABEL.encode('utf-8') + b'\xff') + "); sys.stderr.buffer.write(b'bad: \\x8d')"], cwd=self.root)
        self.assertEqual(result.stdout, LABEL + '\ufffd')
        self.assertEqual(result.stderr, 'bad: \ufffd')

    def test_stable_and_tree_shell_checks_keep_unicode_output(self):
        script = self.root / 'output.py'
        script.write_text('print(' + ascii(LABEL) + ')\n', encoding='utf-8')
        args = [Path(sys.executable).as_posix(), script.name]
        command = subprocess.list2cmdline(args) if os.name == 'nt' else shlex.join(args)
        with patch.dict(os.environ, {'SHELL_ACCESS': 'true', 'PYTHONIOENCODING': 'cp1252'}):
            self.assertEqual(run_shell(self.root, command)['stdout'].strip(), LABEL)
            loop = self.loop()
            self.assertIn(LABEL, loop._exec_run_shell({'command': command}))
            loop._detect_check_command = lambda kind: command
            self.assertIn(LABEL, loop._exec_run_check({'kind': 'test'}))

    def test_file_mutations_and_lazy_readers_preserve_utf8(self):
        with patch.object(io, 'text_encoding', side_effect=lambda encoding, *args: encoding or 'cp1252'):
            tree = ContextTree(self.root)
            tree.index_repo()
            self.assertEqual(tree.resolve('/repo/' + self.file.name).content, LABEL)
            self.assertEqual(tree._make_repo_loader(self.file)(), LABEL)
            loop = self.loop()
            loop._exec_patch_file({'path': self.file.name, 'search': LABEL, 'replace': LABEL + ' updated'})
            self.assertEqual(self.file.read_bytes().decode('utf-8'), LABEL + ' updated')
            loop._exec_replace_lines({'path': self.file.name, 'start_line': 1, 'end_line': 1, 'content': LABEL})
            self.assertEqual(self.file.read_bytes().decode('utf-8'), LABEL)
            loop._exec_write_file({'path': self.file.name, 'content': LABEL + ' new'})
            self.assertEqual(self.file.read_bytes().decode('utf-8'), LABEL + ' new')

    def test_ripgrep_json_preserves_unicode_matches(self):
        if not agent_tools.command_exists('rg'):
            self.skipTest('ripgrep is not installed')
        args = SimpleNamespace(root=str(self.root), path=None, ignore_case=False, hidden=True, glob=None, pattern='P', fixed_strings=True, limit=10)
        with patch.object(agent_tools, 'ok') as output:
            agent_tools.cmd_grep(args)
        matches = output.call_args.args[1]['matches']
        self.assertEqual(matches[0]['text'], LABEL)
        self.assertIn(self.file.name, matches[0]['path'])


if __name__ == '__main__':
    unittest.main()
