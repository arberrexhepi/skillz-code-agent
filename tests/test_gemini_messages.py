from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main as agent
from main import GeminiModelClient


class FakeGeminiTypes:
    class GenerateContentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class ThinkingConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Part:
        def __init__(self, *, text):
            self.text = text

    class Content:
        def __init__(self, *, role, parts):
            self.role = role
            self.parts = parts


class FakeGeminiModels:
    def __init__(self):
        self.last_kwargs = {}

    def generate_content(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            text="done",
            usage_metadata=SimpleNamespace(
                prompt_token_count=300,
                candidates_token_count=25,
                total_token_count=325,
                thoughts_token_count=9,
                cached_content_token_count=210,
            ),
        )


class GeminiMessageTests(unittest.TestCase):
    def _client(self) -> GeminiModelClient:
        client = GeminiModelClient.__new__(GeminiModelClient)
        client.model = "gemini-3-flash-preview"
        client.thinking_mode = "auto"
        client.backoff = None
        client.client = SimpleNamespace(models=FakeGeminiModels())
        return client

    def test_complete_messages_preserves_roles_and_cached_usage(self):
        client = self._client()
        with patch("main.genai_types", FakeGeminiTypes):
            text = client._do_complete_messages(
                "system rules",
                [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "second"},
                    {"role": "user", "content": "third"},
                ],
            )

        self.assertEqual(text, "done")
        request = client.client.models.last_kwargs
        self.assertEqual(request["model"], "gemini-3-flash-preview")
        self.assertEqual([item.role for item in request["contents"]], ["user", "model", "user"])
        self.assertEqual(request["contents"][1].parts[0].text, "second")
        self.assertEqual(request["config"].kwargs["system_instruction"], "system rules")
        self.assertEqual(request["config"].kwargs["automatic_function_calling"], {"disable": True})
        usage = client.get_last_metrics()["usage"]
        self.assertEqual(usage["input_tokens"], 300)
        self.assertEqual(usage["output_tokens"], 25)
        self.assertEqual(usage["reasoning_tokens"], 9)
        self.assertEqual(usage["cached_tokens"], 210)

    @unittest.skipIf(agent.genai is None, "google-genai is not installed")
    def test_sdk_makes_one_request_without_afc_warning_for_both_completion_paths(self):
        # Exercise the installed SDK's generate_content method, replacing only
        # the network boundary. No credentials or model usage are required.
        from google.genai.models import Models
        client = self._client()
        client.client = agent.genai.Client(api_key="fixture-key")
        response = SimpleNamespace(text="done", usage_metadata=None)
        try:
            for messages in (False, True):
                with self.subTest(messages=messages), patch.object(Models, "_logged_afc_warning", False), patch.object(Models, "_generate_content", return_value=response) as generate, self.assertNoLogs("google_genai.models", level="WARNING"):
                    text = client._do_complete_messages("rules", [{"role": "user", "content": "hello"}]) if messages else client._do_complete("rules", "hello")
                    self.assertEqual(text, "done")
                    generate.assert_called_once()
                    self.assertTrue(generate.call_args.kwargs["config"].automatic_function_calling.disable)
        finally:
            client.client.close()


if __name__ == "__main__":
    unittest.main()
