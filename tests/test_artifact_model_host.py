import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import artifact_model_host as helper


class ArtifactModelSetupTests(unittest.TestCase):
    def test_check_constructs_client_without_spending_model_usage(self):
        client = Mock()
        request = {"provider": "gemini", "model": "gemini-3-flash-preview"}
        output = io.StringIO()
        with patch.object(helper, "create_model_client", return_value=client) as factory, patch("sys.stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(output):
            helper.main(check=True)
        self.assertEqual(json.loads(output.getvalue()), {"ready": True})
        self.assertEqual(factory.call_args.kwargs["provider"], "gemini")
        self.assertEqual(client.mock_calls, [])

    def test_missing_sdk_reports_the_helper_interpreter(self):
        # -S removes site packages, reproducing a fresh Python installation.
        result = subprocess.run([sys.executable, "-S", str(Path(helper.__file__)), "--check"], input=json.dumps({"provider": "gemini", "model": "gemini-3-flash-preview"}), text=True, encoding="utf-8", capture_output=True, check=True)
        error = json.loads(result.stdout)["error"]
        self.assertIn(sys.executable, error)
        self.assertIn("-m pip install google-genai", error)
        self.assertIn("restart the artifact agent", error)

    def test_missing_key_explains_host_configuration(self):
        error = helper.setup_error(RuntimeError("GEMINI_API_KEY is not set."))
        self.assertIn("GEMINI_API_KEY", error)
        self.assertIn(str(Path(helper.__file__).with_name(".env")), error)
        self.assertIn("outside the artifact repository", error)

    def test_capability_probe_reports_configuration_without_exposing_key(self):
        import main as agent
        import os
        with patch.object(agent, 'genai', object()), patch.object(agent, 'genai_types', object()), patch.dict(os.environ, {'GEMINI_API_KEY': 'private-test-key'}):
            result = helper.capabilities({'provider': 'gemini'})
        self.assertTrue(result['sdkReady'])
        self.assertTrue(result['keyReady'])
        self.assertEqual(result['keyName'], 'GEMINI_API_KEY')
        self.assertNotIn('private-test-key', json.dumps(result))

    def test_codex_setup_does_not_require_provider_sdks(self):
        result = subprocess.run([sys.executable, "-S", str(Path(helper.__file__)), "--check"], input=json.dumps({"provider": "codex-subscription", "model": "gpt-5.4"}), text=True, encoding="utf-8", capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"ready": True})

    def test_codex_completion_never_invokes_gemini(self):
        import main as agent
        request = {"provider": "codex-subscription", "model": "gpt-5.4", "system": "rules", "messages": [{"role": "user", "content": "original prompt ë"}]}
        output = io.StringIO()
        with patch.object(agent, "GeminiModelClient", side_effect=AssertionError("Codex must not use Gemini")), patch.object(agent, "run_codex_subscription_completion", return_value=("Codex result", {"provider": "codex-subscription"})) as complete, patch("sys.stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(output):
            helper.main()
        self.assertEqual(json.loads(output.getvalue())["text"], "Codex result")
        self.assertEqual(complete.call_args.kwargs["model"], "gpt-5.4")
        self.assertEqual(complete.call_args.kwargs["messages"], request["messages"])


if __name__ == "__main__":
    unittest.main()
