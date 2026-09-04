from __future__ import annotations

import unittest
import unittest.mock as mock
from types import SimpleNamespace

from main import BackoffStrategy, LocalModelClient, OllamaModelClient, create_model_client
from runtime_catalog import supported_provider_keys


class LocalModelClientTests(unittest.TestCase):
    def test_clone_preserves_backoff_configuration(self) -> None:
        with mock.patch("main.OpenAI", return_value=object()):
            client = LocalModelClient(
                model="local-model",
                thinking_mode="high",
                verbosity="low",
            )

        client.backoff = BackoffStrategy(enabled=False, token_limit_k=321)

        with mock.patch("main.OpenAI", return_value=object()):
            clone = client.clone()

        self.assertIsInstance(clone, LocalModelClient)
        self.assertFalse(clone.backoff.enabled)
        self.assertEqual(clone.backoff.token_limit_k, 321)
        self.assertEqual(clone.model, client.model)
        self.assertEqual(clone.thinking_mode, client.thinking_mode)
        self.assertEqual(clone.verbosity, client.verbosity)

    def test_ollama_clone_preserves_backoff_configuration(self) -> None:
        with mock.patch("main.OpenAI", return_value=object()):
            client = OllamaModelClient(
                model="gemma4:e2b",
                thinking_mode="high",
                verbosity="low",
                base_url="http://127.0.0.1:11434/v1",
                provider_name="ollama-local",
            )

        client.backoff = BackoffStrategy(enabled=False, token_limit_k=321)

        with mock.patch("main.OpenAI", return_value=object()):
            clone = client.clone()

        self.assertIsInstance(clone, OllamaModelClient)
        self.assertFalse(clone.backoff.enabled)
        self.assertEqual(clone.backoff.token_limit_k, 321)
        self.assertEqual(clone.model, client.model)
        self.assertEqual(clone.thinking_mode, client.thinking_mode)
        self.assertEqual(clone.verbosity, client.verbosity)
        self.assertEqual(clone.base_url, client.base_url)
        self.assertEqual(clone.provider_name, client.provider_name)

    def test_create_model_client_supports_ollama_local(self) -> None:
        with mock.patch("main.OpenAI", return_value=object()):
            client = create_model_client(provider="ollama-local", model="gemma4:e2b")

        self.assertIsInstance(client, OllamaModelClient)
        self.assertEqual(client.provider_name, "ollama-local")

    def test_create_model_client_supports_ollama_runpod(self) -> None:
        with mock.patch("main.OpenAI", return_value=object()):
            client = create_model_client(provider="ollama-runpod", model="gemma4:e2b")

        self.assertIsInstance(client, OllamaModelClient)
        self.assertEqual(client.provider_name, "ollama-runpod")
        self.assertEqual(client.base_url, "https://zql0xy4x10v0sp-11434.proxy.runpod.net/v1")

    def test_ollama_base_url_normalizes_chat_completions_suffix(self) -> None:
        captured: dict[str, str] = {}

        def fake_openai(*, api_key: str, base_url: str, **kwargs):
            captured["base_url"] = base_url
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: None)))

        with mock.patch("main.OpenAI", side_effect=fake_openai):
            OllamaModelClient(
                model="gemma4:e2b",
                base_url="https://zql0xy4x10v0sp-11434.proxy.runpod.net/v1/chat/completions",
            )

        self.assertEqual(captured["base_url"], "https://zql0xy4x10v0sp-11434.proxy.runpod.net/v1")

    def test_supported_provider_keys_includes_ollama(self) -> None:
        self.assertIn("ollama-local", supported_provider_keys())
        self.assertIn("ollama-runpod", supported_provider_keys())
