from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gemini_subscription


class GeminiDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillz gemini discovery ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.addCleanup(patch.stopall)
        patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": str(self.root / "Local"),
                "APPDATA": str(self.root / "Roaming"),
                "GEMINI_CLI_PATH": "",
                "GEMINI_INSTALL_DIR": "",
                "GEMINI_CLI_HOME": str(self.home),
            },
        ).start()
        patch.object(Path, "home", return_value=self.home).start()
        self.which = patch.object(gemini_subscription.shutil, "which", return_value=None).start()

    def executable(self, relative: str) -> str:
        candidate = self.root / relative
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"fixture")
        return str(candidate)

    def test_explicit_path_with_spaces_keeps_precedence(self):
        expected = self.executable("custom install/gemini")
        os.environ["GEMINI_CLI_PATH"] = expected
        self.which.return_value = self.executable("path-install/gemini")
        self.assertEqual(gemini_subscription.resolve_gemini_executable(), expected)

    def test_missing_saved_path_does_not_silently_fallback(self):
        os.environ["GEMINI_CLI_PATH"] = str(self.root / "moved" / "gemini")
        self.which.return_value = self.executable("path-install/gemini")
        with self.assertRaisesRegex(RuntimeError, "configured Gemini CLI was not found"):
            gemini_subscription.resolve_gemini_executable()

    def test_windows_discovers_npm_bundle_without_path(self):
        expected = self.executable("Roaming/npm/node_modules/@google/gemini-cli/bundle/gemini.js")
        with patch.object(gemini_subscription.sys, "platform", "win32"):
            self.assertEqual(gemini_subscription.resolve_gemini_executable(), expected)

    def test_macos_discovers_local_bin_without_path(self):
        expected = self.executable("home/.local/bin/gemini")
        with patch.object(gemini_subscription.sys, "platform", "darwin"):
            self.assertEqual(gemini_subscription.resolve_gemini_executable(), expected)

    def test_missing_installation_has_actionable_error(self):
        with patch.object(gemini_subscription.sys, "platform", "linux"):
            status = gemini_subscription.gemini_subscription_status()
        self.assertFalse(status["available"])
        self.assertFalse(status["authenticated"])
        self.assertIn("GEMINI_CLI_PATH", status["error"])

    def test_auth_status_detects_oauth_session_without_reading_tokens(self):
        exe = self.executable("bin/gemini")
        gemini_dir = self.home / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        (gemini_dir / "oauth_creds.json").write_text('{"access_token":"secret"}', encoding="utf-8")
        (gemini_dir / "google_accounts.json").write_text(json.dumps({"active": "dev@example.com"}), encoding="utf-8")

        status = gemini_subscription.quick_gemini_auth_status(exe)
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["auth_mode"], "google_oauth")
        self.assertEqual(status["email"], "dev@example.com")
        self.assertNotIn("secret", json.dumps(status))

    def test_auth_status_rejects_api_key_selected_auth_mode(self):
        exe = self.executable("bin/gemini")
        gemini_dir = self.home / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        (gemini_dir / "oauth_creds.json").write_text('{"access_token":"secret"}', encoding="utf-8")
        (gemini_dir / "settings.json").write_text(
            json.dumps({"security": {"auth": {"selectedType": "gemini-api-key"}}}),
            encoding="utf-8",
        )

        status = gemini_subscription.quick_gemini_auth_status(exe)
        self.assertFalse(status["authenticated"])
        self.assertIn("gemini-api-key", status["status_text"])

    def test_sign_out_clears_oauth_session_and_preserves_settings(self):
        exe = self.executable("bin/gemini")
        os.environ["GEMINI_CLI_PATH"] = exe
        gemini_dir = self.home / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        oauth_file = gemini_dir / "oauth_creds.json"
        accounts_file = gemini_dir / "google_accounts.json"
        settings_file = gemini_dir / "settings.json"
        oauth_file.write_text('{"access_token":"secret"}', encoding="utf-8")
        accounts_file.write_text(json.dumps({"active": "dev@example.com"}), encoding="utf-8")
        settings_file.write_text('{"theme":"dark"}', encoding="utf-8")

        with patch.object(gemini_subscription, "_cli_version", return_value="0.71.0"):
            status = gemini_subscription.sign_out_gemini_google()

        self.assertTrue(status["available"])
        self.assertFalse(status["authenticated"])
        self.assertFalse(oauth_file.exists())
        self.assertFalse(accounts_file.exists())
        self.assertTrue(settings_file.exists())


if __name__ == "__main__":
    unittest.main()
