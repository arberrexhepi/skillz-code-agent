import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from main import MetaModelClient, create_model_client
from runtime_catalog import RUNTIME_PROVIDER_CATALOG, runtime_options_payload, validate_provider_model_selection


class FakeResponses:
    def __init__(self) -> None:
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            output_text="ok",
            usage=SimpleNamespace(
                input_tokens=12,
                output_tokens=3,
                total_tokens=15,
                input_tokens_details=SimpleNamespace(cached_tokens=8),
                output_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ),
        )


class FakeOpenAI:
    calls = []

    def __init__(self, **kwargs) -> None:
        self.__class__.calls.append(kwargs)
        self.responses = FakeResponses()


class FakeRawResponses:
    def __init__(self) -> None:
        self.last_kwargs = None
        self.with_raw_response = self

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        parsed = SimpleNamespace(
            output_text="ok",
            usage=SimpleNamespace(input_tokens=4, output_tokens=1, total_tokens=5),
        )
        return SimpleNamespace(
            parse=lambda: parsed,
            headers={
                "x-ratelimit-limit-tokens": "4000000",
                "x-ratelimit-remaining-tokens": "3999995",
                "x-request-id": "request-success",
            },
        )


class FakeRawOpenAI(FakeOpenAI):
    def __init__(self, **kwargs) -> None:
        self.__class__.calls.append(kwargs)
        self.responses = FakeRawResponses()


class MetaModelClientTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeOpenAI.calls = []

    def test_meta_provider_catalog_uses_official_model_api_settings(self):
        meta = RUNTIME_PROVIDER_CATALOG["meta"]
        self.assertEqual(meta["env_var"], "META_AI_API_KEY")
        self.assertEqual(meta["default_model"], "muse-spark-1.2")
        self.assertIn("muse-spark-1.2-contributor", meta["models"])

    def test_meta_model_prefix_is_rejected_for_other_providers(self):
        with self.assertRaisesRegex(ValueError, "looks like a meta model"):
            validate_provider_model_selection("openai", "muse-spark-1.2")

    def test_extension_and_runtime_options_expose_meta_selection(self):
        package = json.loads((Path(__file__).parent / "vscode-extension" / "package.json").read_text())
        provider_setting = package["contributes"]["configuration"]["properties"]["skillzAgent.provider"]
        self.assertIn("meta", provider_setting["enum"])

        meta = next(item for item in runtime_options_payload()["providers"] if item["key"] == "meta")
        self.assertEqual(meta["default_model"], "muse-spark-1.2")
        self.assertIn("muse-spark-1.2", meta["suggested_models"])

    @patch.dict(os.environ, {"META_AI_API_KEY": "meta-key"}, clear=True)
    @patch.object(main, "OpenAI", FakeOpenAI)
    def test_client_uses_meta_base_url_and_responses_api_without_verbosity(self):
        client = create_model_client(
            provider="meta",
            model="muse-spark-1.2",
            thinking_mode="high",
            verbosity="high",
        )

        self.assertIsInstance(client, MetaModelClient)
        self.assertEqual(
            FakeOpenAI.calls[-1],
            {"api_key": "meta-key", "base_url": "https://api.meta.ai/v1", "max_retries": 0},
        )
        self.assertEqual(client.complete("system", "prompt"), "ok")
        request = client.client.responses.last_kwargs
        self.assertEqual(request["model"], "muse-spark-1.2")
        self.assertEqual(request["reasoning"], {"effort": "high"})
        self.assertNotIn("text", request)
        self.assertEqual(client.get_last_metrics()["provider"], "meta")
        self.assertEqual(client.get_last_metrics()["usage"]["cached_tokens"], 8)

    @patch.dict(os.environ, {"META_AI_API_KEY": "meta-key"}, clear=True)
    @patch.object(main, "OpenAI", FakeOpenAI)
    def test_meta_rejects_unsupported_none_reasoning_mode(self):
        with self.assertRaisesRegex(ValueError, "does not support thinking mode 'none'"):
            MetaModelClient(model="muse-spark-1.2", thinking_mode="none")

    @patch.dict(os.environ, {"META_AI_API_KEY": "meta-key"}, clear=True)
    @patch.object(main, "OpenAI", FakeRawOpenAI)
    def test_success_metrics_capture_rate_limit_headers(self):
        client = MetaModelClient(model="muse-spark-1.2", thinking_mode="low")

        self.assertEqual(client.complete("system", "prompt"), "ok")

        metrics = client.get_last_metrics()
        self.assertEqual(metrics["rate_limit_headers"]["x-ratelimit-limit-tokens"], "4000000")
        self.assertEqual(metrics["response_headers"]["x-request-id"], "request-success")

    @patch.dict(os.environ, {}, clear=True)
    @patch.object(main, "OpenAI", FakeOpenAI)
    def test_meta_reports_the_dotenv_credential_name_when_missing(self):
        with self.assertRaisesRegex(RuntimeError, "META_AI_API_KEY"):
            MetaModelClient(model="muse-spark-1.2")


if __name__ == "__main__":
    unittest.main()
