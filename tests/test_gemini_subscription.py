from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import main
from gemini_subscription import (
    _extract_response_text,
    _extract_usage,
    _render_backend_prompt,
    _run_gemini_exec_streaming,
    _skillz_permission_blocked_finish,
    run_gemini_subscription_completion,
)
from runtime_catalog import RUNTIME_PROVIDER_CATALOG, runtime_options_payload, validate_provider_model_selection


class GeminiSubscriptionTests(unittest.TestCase):
    def test_runtime_catalog_keeps_subscription_and_api_providers_distinct(self):
        self.assertIn("gemini", RUNTIME_PROVIDER_CATALOG)
        self.assertIn("gemini-subscription", RUNTIME_PROVIDER_CATALOG)
        subscription = next(
            item for item in runtime_options_payload()["providers"]
            if item["key"] == "gemini-subscription"
        )
        self.assertEqual(subscription["authentication"], "google_oauth_subscription")
        self.assertIsNone(subscription["env_var"])
        self.assertIsNone(subscription["package"])
        self.assertTrue(subscription["accepts_custom_model"])

    def test_gemini_shaped_models_are_valid_for_both_gemini_routes(self):
        validate_provider_model_selection("gemini", "gemini-3.1-pro-preview")
        for model in ("gemini-4-hydrogen", "gemini-4-neon", "gemini-4-carbon", "gemini-4-argon", "gemini-2.5-pro"):
            self.assertIn(model, RUNTIME_PROVIDER_CATALOG["gemini-subscription"]["models"])
            validate_provider_model_selection("gemini-subscription", model)

    def test_backend_prompt_separates_gemini_cli_tools_from_skillz_actions(self):
        prompt = _render_backend_prompt(
            "Return JSON.",
            [{"role": "user", "content": "Inspect the repository."}],
        )
        self.assertIn("Do not use Gemini CLI's built-in shell, file, web, or MCP tools directly", prompt)
        self.assertIn("Gemini CLI's isolated working directory does not prohibit returning Skillz read or mutation actions", prompt)
        self.assertIn("[SKILLZ SYSTEM INSTRUCTIONS]\nReturn JSON.", prompt)
        self.assertIn("[USER]\nInspect the repository.", prompt)

    def test_false_permission_blocked_finish_is_detected(self):
        payload = json.dumps(
            {
                "thought": "I cannot use workspace tools.",
                "action": {
                    "type": "finish",
                    "message": "Blocked: no workspace actions permitted. Goal requires repository inspection.",
                },
            }
        )
        self.assertIn("no workspace actions permitted", _skillz_permission_blocked_finish(payload))
        self.assertEqual(
            _skillz_permission_blocked_finish(
                '{"thought":"done","action":{"type":"finish","message":"Implemented and validated."}}'
            ),
            "",
        )

    @patch("gemini_subscription.quick_gemini_auth_status", return_value={"authenticated": True})
    @patch("gemini_subscription.resolve_gemini_executable", return_value="/fake/gemini")
    @patch.dict(
        os.environ,
        {
            "GEMINI_API_KEY": "must-not-leak",
            "GOOGLE_API_KEY": "also-must-not-leak",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/creds.json",
            "GOOGLE_GENAI_USE_VERTEXAI": "true",
        },
        clear=False,
    )
    def test_completion_is_isolated_headless_and_drops_api_credentials(
        self,
        _resolve,
        _auth,
    ):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs["env"]
            captured["input"] = kwargs["input"]
            captured["cwd"] = kwargs["cwd"]
            lines = [
                json.dumps({"type": "init", "session_id": "s1", "model": "gemini-2.5-pro"}),
                json.dumps({"type": "message", "role": "assistant", "content": '{"action":', "delta": True}),
                json.dumps({"type": "message", "role": "assistant", "content": '"finish"}', "delta": True}),
                json.dumps(
                    {
                        "type": "result",
                        "status": "success",
                        "stats": {"input_tokens": 120, "output_tokens": 8, "cached": 20},
                    }
                ),
            ]
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(lines), stderr="")

        with patch("gemini_subscription.subprocess.run", side_effect=fake_run):
            text, metrics = run_gemini_subscription_completion(
                model="gemini-2.5-pro",
                thinking_mode="medium",
                system="Return JSON only.",
                messages=[{"role": "user", "content": "Finish."}],
            )

        self.assertEqual(text, '{"action":"finish"}')
        self.assertIn("--output-format", captured["args"])
        self.assertEqual(captured["args"][captured["args"].index("--output-format") + 1], "stream-json")
        self.assertEqual(captured["args"][captured["args"].index("--model") + 1], "gemini-2.5-pro")
        self.assertEqual(captured["args"][captured["args"].index("-e") + 1], "none")
        self.assertNotIn("GEMINI_API_KEY", captured["env"])
        self.assertNotIn("GOOGLE_API_KEY", captured["env"])
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", captured["env"])
        self.assertNotIn("GOOGLE_GENAI_USE_VERTEXAI", captured["env"])
        self.assertEqual(metrics["provider"], "gemini-subscription")
        self.assertEqual(metrics["auth_mode"], "google_oauth")
        self.assertEqual(metrics["billing_mode"], "google_oauth_subscription")
        self.assertEqual(metrics["usage"]["input_tokens"], 120)
        self.assertEqual(metrics["usage"]["output_tokens"], 8)
        self.assertEqual(metrics["usage"]["cached_tokens"], 20)
        self.assertEqual(metrics["usage"]["total_tokens"], 128)

    def test_model_client_preserves_metrics_contract(self):
        payload = {
            "provider": "gemini-subscription",
            "model": "gemini-2.5-pro",
            "auth_mode": "google_oauth",
            "billing_mode": "google_oauth_subscription",
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        }
        with patch.object(main, "run_gemini_subscription_completion", return_value=("done", payload)):
            client = main.create_model_client(
                provider="gemini-subscription",
                model="gemini-2.5-pro",
            )
            self.assertEqual(client.complete("system", "prompt"), "done")
            self.assertEqual(client.get_last_metrics(), payload)

    @patch("gemini_subscription.quick_gemini_auth_status", return_value={"authenticated": True})
    @patch("gemini_subscription.resolve_gemini_executable", return_value="/fake/gemini")
    def test_completion_rejects_false_permission_finish(self, _resolve, _auth):
        def fake_run(args, **_kwargs):
            event = {
                "type": "message",
                "role": "assistant",
                "content": json.dumps(
                    {
                        "thought": "Workspace access is unavailable.",
                        "action": {
                            "type": "finish",
                            "message": "Blocked: no workspace actions permitted.",
                        },
                    }
                ),
            }
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(event), stderr="")

        with patch("gemini_subscription.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "false workspace-permission block"):
                run_gemini_subscription_completion(
                    model="gemini-2.5-pro",
                    thinking_mode="medium",
                    system="Return a Skillz action.",
                    messages=[{"role": "user", "content": "Inspect the workspace."}],
                )

    def test_usage_extraction_accepts_gemini_stats_and_camel_case(self):
        usage = _extract_usage([
            {"type": "result", "stats": {"promptTokenCount": 9, "candidatesTokenCount": 4, "cachedContentTokenCount": 3}}
        ])
        self.assertEqual(usage, {"input_tokens": 9, "output_tokens": 4, "cached_tokens": 3, "total_tokens": 13})

    def test_extract_response_text_falls_back_to_result_response(self):
        self.assertEqual(
            _extract_response_text([{"type": "result", "response": "final text"}], ""),
            "final text",
        )

    def test_jsonl_lifecycle_events_stream_before_process_completion(self):
        events = []
        script = (
            "import json, sys; "
            "sys.stdin.read(); "
            "print(json.dumps({'type': 'init', 'model': 'gemini-2.5-pro'}), flush=True); "
            "print(json.dumps({'type': 'message', 'role': 'assistant', 'content': 'ok'}), flush=True); "
            "print(json.dumps({'type': 'result', 'stats': {'input_tokens': 3}}), flush=True)"
        )

        result = _run_gemini_exec_streaming(
            [sys.executable, "-c", script],
            prompt="test prompt",
            cwd=os.getcwd(),
            timeout=5,
            env=os.environ,
            progress_callback=events.append,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual([event["type"] for event in events], ["init", "message", "result"])
        self.assertTrue(all("skillz_elapsed_s" in event for event in events))

    @patch("gemini_subscription.quick_gemini_auth_status", return_value={"authenticated": True})
    @patch("gemini_subscription.resolve_gemini_executable", return_value="/fake/gemini")
    def test_subscription_streams_thought_items_before_final_message(self, _resolve, _auth):
        summaries = []

        def stream(args, *, progress_callback, **kwargs):
            progress_callback({"type": "thought", "content": "Inspecting repository layout."})
            assert summaries == ["Inspecting repository layout.\n\n"]
            stdout = json.dumps({"type": "message", "role": "assistant", "content": "finish done"})
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        with patch("gemini_subscription._run_gemini_exec_streaming", side_effect=stream):
            client = main.GeminiSubscriptionModelClient("gemini-2.5-pro")
            result = client.complete_messages_with_progress("system", [{"role": "user", "content": "Inspect."}], summaries.append)
        self.assertEqual(result, "finish done")

    def test_streaming_early_exit_surfaces_stderr_instead_of_broken_pipe(self):
        script = "import sys; sys.stderr.write('Real CLI startup error\\n'); sys.stderr.flush(); sys.exit(1)"
        result = _run_gemini_exec_streaming(
            [sys.executable, "-c", script],
            prompt="x" * 200_000,
            cwd=os.getcwd(),
            timeout=5,
            env=os.environ,
            progress_callback=lambda _event: None,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Real CLI startup error", result.stderr)


if __name__ == "__main__":
    unittest.main()
