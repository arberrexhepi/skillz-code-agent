import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import artifact_model_host as helper
from runtime_catalog import RUNTIME_PROVIDER_CATALOG


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

    def test_artifact_setup_offers_to_upgrade_older_gemini_sdk_without_blocking_launch(self):
        import main as agent
        with patch.object(agent, 'genai', object()), patch.object(agent, 'genai_types', object()), patch.object(helper.sys, 'version_info', (3, 11)):
            result = helper.capabilities({'provider': 'gemini'})
        self.assertTrue(result['sdkReady'])
        self.assertTrue(result['sdkUpdateAvailable'])

    def test_codex_setup_does_not_require_provider_sdks(self):
        model = RUNTIME_PROVIDER_CATALOG["codex-subscription"]["default_model"]
        result = subprocess.run([sys.executable, "-S", str(Path(helper.__file__)), "--check"], input=json.dumps({"provider": "codex-subscription", "model": model}), text=True, encoding="utf-8", capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"ready": True})

    def test_codex_completion_never_invokes_gemini(self):
        import main as agent
        model = RUNTIME_PROVIDER_CATALOG["codex-subscription"]["default_model"]
        request = {"provider": "codex-subscription", "model": model, "system": "rules", "messages": [{"role": "user", "content": "original prompt ë"}]}
        output = io.StringIO()
        with patch.object(agent, "GeminiModelClient", side_effect=AssertionError("Codex must not use Gemini")), patch.object(agent, "run_codex_subscription_completion", return_value=("Codex result", {"provider": "codex-subscription"})) as complete, patch("sys.stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(output):
            helper.main()
        self.assertEqual(json.loads(output.getvalue())["text"], "Codex result")
        self.assertEqual(complete.call_args.kwargs["model"], model)
        self.assertEqual(complete.call_args.kwargs["messages"], request["messages"])

    def test_model_host_streams_summary_separately_from_final_text(self):
        client = Mock()
        client.get_last_metrics.return_value = {}

        def complete(system, messages, on_summary, *, on_output):
            on_summary("Checking the workspace.")
            assert on_output.preview_strategy
            assert on_output.preserve_request_context
            assert on_output.require_command_frame
            on_output.report(11, ["s1: cat"])
            return "finish done"

        client.complete_messages_with_progress.side_effect = complete
        request = {"provider": "gemini", "model": "gemini-3-flash-preview", "system": "rules",
                   "messages": [], "reasoning_summary": True, "preview_strategy": True,
                   "preserve_request_context": True, "require_command_frame": True}
        output = io.StringIO()
        with patch.object(helper, "create_model_client", return_value=client), patch("sys.stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(output):
            helper.main()
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [
            {"progress": {"type": "reasoning_summary", "delta": "Checking the workspace."}},
            {"progress": {"type": "output_progress", "output_chars": 11, "draft_strategy": ["s1: cat"]}},
            {"text": "finish done", "metrics": {}},
        ])


if __name__ == "__main__":
    unittest.main()
