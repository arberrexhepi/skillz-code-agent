from __future__ import annotations

import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import codex_subscription as codex


# A real local process consumes and produces UTF-8 bytes, just like the CLI.
# No authentication, network requests, or model calls are made by these tests.
_FAKE_CODEX = r'''
import json, sys
from pathlib import Path

def emit(payload):
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode('utf-8') + b'\n')
    sys.stdout.buffer.flush()

label = 'caf\u00e9 \u2014 \u4e16\u754c \U0001f680'
command = sys.argv[1]
if command == 'exec':
    prompt = sys.stdin.buffer.read().decode('utf-8')
    output = sys.argv[sys.argv.index('--output-last-message') + 1]
    Path(output).write_bytes(prompt.encode('utf-8'))
    emit({'type': 'turn.started', 'label': label})
    emit({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 1}})
    sys.stderr.buffer.write(label.encode('utf-8') + b'\n')
elif command == 'app-server':
    for line in sys.stdin.buffer:
        request = json.loads(line.decode('utf-8'))
        if 'id' in request:
            emit({'id': request['id'], 'result': {'label': label, 'echo': request['params']}})
elif command == 'login':
    sys.stderr.buffer.write(('Logged in using ChatGPT ' + label).encode('utf-8'))
elif command == '--version':
    sys.stdout.buffer.write(('codex-cli fixture ' + label).encode('utf-8'))
'''


class CodexUtf8Tests(unittest.TestCase):
    def setUp(self):
        original_popen = subprocess.Popen

        def launch(args, **kwargs):
            self.assertEqual(args[0], '/fixture/codex')
            child = original_popen([sys.executable, '-c', _FAKE_CODEX, *args[1:]], **kwargs)

            def cleanup():
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()

            self.addCleanup(cleanup)
            return child

        self.enterContext(patch.object(codex, 'resolve_codex_executable', return_value='/fixture/codex'))
        self.enterContext(patch.object(subprocess, 'Popen', side_effect=launch))
        # Enforce the affected Windows default even on UTF-8-default CI hosts.
        self.enterContext(patch.object(subprocess, '_text_encoding', return_value='cp1252'))
        self.label = 'caf\u00e9 \u2014 \u4e16\u754c \U0001f680'

    def completion(self, content, streaming):
        events = []
        system = 'Return the supplied text.'
        messages = [{'role': 'user', 'content': content}]
        with patch.object(codex, 'quick_chatgpt_auth_status', return_value={'authenticated': True}):
            response, metrics = codex.run_codex_subscription_completion(
                model='fixture-model', thinking_mode='low', system=system, messages=messages,
                timeout=5, progress_callback=events.append if streaming else None,
            )
        self.assertEqual(response, codex._render_backend_prompt(system, messages).strip())
        self.assertEqual(metrics['usage']['total_tokens'], 4)
        if streaming:
            self.assertEqual(events[0]['label'], self.label)
            self.assertEqual([event['type'] for event in events], ['turn.started', 'turn.completed'])

    def test_ascii_user_prompt_with_builtin_em_dash_uses_utf8(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                self.completion('Hello', streaming)

    def test_multilingual_prompts_and_events_survive_both_completion_paths(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                self.completion(self.label, streaming)

    def test_app_server_round_trips_unicode_json(self):
        with codex.CodexAppServerSession(timeout=3) as session:
            result = session.request('echo', {'message': self.label})
        self.assertEqual(result['label'], self.label)
        self.assertEqual(result['echo']['message'], self.label)

    def test_login_status_and_version_decode_utf8(self):
        status = codex.quick_chatgpt_auth_status()
        self.assertTrue(status['authenticated'])
        self.assertEqual(status['status_text'], 'Logged in using ChatGPT ' + self.label)
        self.assertEqual(codex._cli_version('/fixture/codex'), 'codex-cli fixture ' + self.label)


if __name__ == '__main__':
    unittest.main()
