from __future__ import annotations

import unittest
from types import SimpleNamespace

import main
from main import OpenAIModelClient, TokenUsageEstimator


class FakeResponses:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        usage = SimpleNamespace(
            input_tokens=2048,
            output_tokens=128,
            total_tokens=2176,
            input_tokens_details=SimpleNamespace(cached_tokens=1024),
            output_tokens_details=SimpleNamespace(reasoning_tokens=64),
        )
        return SimpleNamespace(output_text="ok", usage=usage)


class OpenAIPromptCacheTests(unittest.TestCase):
    def _client(self, model: str = "gpt-5.5") -> OpenAIModelClient:
        client = OpenAIModelClient.__new__(OpenAIModelClient)
        client.client = SimpleNamespace(responses=FakeResponses())
        client.model = model
        client.thinking_mode = "low"
        client.verbosity = "medium"
        client.backoff = main.BackoffStrategy()
        client.prompt_cache_key = ""
        return client

    def test_responses_call_includes_cache_key_and_24h_retention_for_gpt55(self) -> None:
        client = self._client("gpt-5.5")
        client.set_prompt_cache_key("treeloop:test")

        output = client._do_complete_messages("system", [{"role": "user", "content": "hello"}])

        self.assertEqual(output, "ok")
        kwargs = client.client.responses.kwargs
        self.assertEqual(kwargs["prompt_cache_key"], "treeloop:test")
        self.assertEqual(kwargs["prompt_cache_retention"], "24h")
        metrics = client.get_last_metrics()
        self.assertEqual(metrics["usage"]["cached_tokens"], 1024)
        self.assertEqual(metrics["usage"]["reasoning_tokens"], 64)

    def test_non_gpt55_uses_cache_key_without_24h_retention(self) -> None:
        client = self._client("gpt-5.4")
        client.set_prompt_cache_key("treeloop:test")

        client._do_complete_messages("system", [{"role": "user", "content": "hello"}])

        kwargs = client.client.responses.kwargs
        self.assertEqual(kwargs["prompt_cache_key"], "treeloop:test")
        self.assertNotIn("prompt_cache_retention", kwargs)

    def test_usage_summary_includes_cached_tokens(self) -> None:
        snapshot = TokenUsageEstimator().estimate(
            provider="openai",
            model="gpt-5.5",
            usage={"input_tokens": 2048, "output_tokens": 128, "total_tokens": 2176, "cached_tokens": 1024},
        )

        self.assertEqual(snapshot.cached_tokens, 1024)
        self.assertIn("cached=1,024", TokenUsageEstimator().render_cli_summary(snapshot))


if __name__ == "__main__":
    unittest.main()
