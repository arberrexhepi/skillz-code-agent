from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from codex_subscription import (
    _extract_usage,
    _render_backend_prompt,
    run_codex_subscription_completion,
)
from runtime_catalog import RUNTIME_PROVIDER_CATALOG, runtime_options_payload, validate_provider_model_selection


class CodexSubscriptionTests(unittest.TestCase):
    def test_runtime_catalog_keeps_subscription_and_api_providers_distinct(self):
        self.assertIn("openai", RUNTIME_PROVIDER_CATALOG)
        self.assertIn("codex-subscription", RUNTIME_PROVIDER_CATALOG)
        subscription = next(
            item for item in runtime_options_payload()["providers"]
            if item["key"] == "codex-subscription"
        )
        self.assertEqual(subscription["authentication"], "chatgpt_subscription")
        self.assertFalse(subscription["accepts_custom_model"])
        self.assertNotEqual(subscription["default_model"], RUNTIME_PROVIDER_CATALOG["openai"]["default_model"])

    def test_openai_shaped_models_are_valid_for_both_openai_routes(self):
        validate_provider_model_selection("openai", "gpt-5.4")
        validate_provider_model_selection("codex-subscription", "gpt-5.6-terra")

    def test_backend_prompt_prohibits_codex_from_becoming_a_second_tool_agent(self):
        prompt = _render_backend_prompt(
            "Return JSON.",
            [{"role": "user", "content": "Inspect the repository."}],
        )
        self.assertIn("Do not inspect files, run commands, call tools, or modify the workspace", prompt)
        self.assertIn("[SKILLZ SYSTEM INSTRUCTIONS]\nReturn JSON.", prompt)
        self.assertIn("[USER]\nInspect the repository.", prompt)

    @patch("codex_subscription.quick_chatgpt_auth_status", return_value={"authenticated": True})
    @patch("codex_subscription.resolve_codex_executable", return_value="/fake/codex")
    @patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "must-not-leak", "CODEX_API_KEY": "also-must-not-leak"},
        clear=False,
    )
    def test_completion_is_ephemeral_read_only_and_drops_api_credentials(
        self,
        _resolve,
        _auth,
    ):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs["env"]
            captured["input"] = kwargs["input"]
            output_index = args.index("--output-last-message") + 1
            Path(args[output_index]).write_text('{"action":"finish"}', encoding="utf-8")
            event = {
                "type": "turn.completed",
                "usage": {"input_tokens": 120, "output_tokens": 8, "cached_input_tokens": 20},
            }
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(event), stderr="")

        with patch("codex_subscription.subprocess.run", side_effect=fake_run):
            text, metrics = run_codex_subscription_completion(
                model="gpt-5.6-terra",
                thinking_mode="medium",
                system="Return JSON only.",
                messages=[{"role": "user", "content": "Finish."}],
            )

        self.assertEqual(text, '{"action":"finish"}')
        self.assertIn("--ephemeral", captured["args"])
        self.assertEqual(captured["args"][captured["args"].index("--sandbox") + 1], "read-only")
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotIn("CODEX_API_KEY", captured["env"])
        self.assertEqual(metrics["billing_mode"], "chatgpt_subscription")
        self.assertEqual(metrics["usage"]["cached_tokens"], 20)

    def test_model_client_preserves_metrics_contract(self):
        payload = {
            "provider": "codex-subscription",
            "model": "gpt-5.6-terra",
            "billing_mode": "chatgpt_subscription",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        with patch.object(main, "run_codex_subscription_completion", return_value=("done", payload)):
            client = main.create_model_client(
                provider="codex-subscription",
                model="gpt-5.6-terra",
            )
            self.assertEqual(client.complete("system", "prompt"), "done")
            self.assertEqual(client.get_last_metrics(), payload)

    def test_usage_extraction_accepts_camel_case_codex_events(self):
        usage = _extract_usage([
            {"type": "turn.completed", "payload": {"usage": {"inputTokens": 9, "outputTokens": 4}}}
        ])
        self.assertEqual(usage, {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13})


if __name__ == "__main__":
    unittest.main()
