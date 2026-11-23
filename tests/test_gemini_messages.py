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
        self.assertNotIn("max_output_tokens", request["config"].kwargs)
        self.assertEqual(request["config"].kwargs["automatic_function_calling"], {"disable": True})
        self.assertEqual(request["config"].kwargs["tool_config"], {"function_calling_config": {"mode": "NONE"}})
        usage = client.get_last_metrics()["usage"]
        self.assertEqual(usage["input_tokens"], 300)
        self.assertEqual(usage["output_tokens"], 25)
        self.assertEqual(usage["reasoning_tokens"], 9)
        self.assertEqual(usage["cached_tokens"], 210)

    def test_modern_model_tags_use_the_same_effort_controls(self):
        client = self._client()
        with patch("main.genai_types", FakeGeminiTypes):
            for tag in ("gemini-3.8-flash", "gemini-4-hydrogen", "models/gemini-5-example", "custom-thinking-alias"):
                client.model = tag
                for effort in ("low", "medium", "high", "xhigh", "auto"):
                    with self.subTest(tag=tag, effort=effort):
                        client.thinking_mode = effort
                        config = client._generate_config("system", include_summaries=True).kwargs
                        expected = {"include_thoughts": True}
                        if effort != "auto":
                            expected["thinking_level"] = effort
                        self.assertEqual(config["thinking_config"].kwargs, expected)
                        self.assertNotIn("max_output_tokens", config)

    def test_trailing_stream_chunk_cannot_overwrite_protocol_failure(self):
        client = self._client()
        part = SimpleNamespace(text="finish premature", thought=False)
        chunks = [SimpleNamespace(candidates=[SimpleNamespace(
            finish_reason=reason, content=SimpleNamespace(parts=[part] if reason == "MALFORMED_RESPONSE" else [])
        )], prompt_feedback=None) for reason in ("MALFORMED_RESPONSE", "STOP")]
        client.client.models.generate_content_stream = lambda **kwargs: iter(chunks)
        with patch("main.genai_types", FakeGeminiTypes):
            text = client._stream_gemini_messages("system", [], lambda _: None, None)
        self.assertEqual(text, "finish premature")
        self.assertEqual(client.get_last_metrics()["response"]["candidates"][0]["finish_reason"], "MALFORMED_RESPONSE")

    @unittest.skipIf(agent.genai is None, "google-genai is not installed")
    def test_sdk_makes_one_request_without_afc_warning_for_both_completion_paths(self):
        # Exercise the installed SDK's generate_content method, replacing only
        # the network boundary. No credentials or model usage are required.
        from google.genai.models import Models
        client = self._client()
        client.client = agent.genai.Client(api_key="fixture-key")
        response = agent.genai_types.GenerateContentResponse(candidates=[
            agent.genai_types.Candidate(
                content=agent.genai_types.Content(parts=[agent.genai_types.Part(text="done")]),
                finish_reason="STOP",
            ),
        ])
        try:
            for messages in (False, True):
                with self.subTest(messages=messages), patch.object(Models, "_generate_content", return_value=response) as generate, self.assertNoLogs("google_genai.models", level="WARNING"):
                    text = client._do_complete_messages("rules", [{"role": "user", "content": "hello"}]) if messages else client._do_complete("rules", "hello")
                    self.assertEqual(text, "done")
                    generate.assert_called_once()
                    self.assertTrue(generate.call_args.kwargs["config"].automatic_function_calling.disable)
                    self.assertEqual(generate.call_args.kwargs["config"].tool_config.function_calling_config.mode.value, "NONE")
        finally:
            client.client.close()


if __name__ == "__main__":
    unittest.main()
