from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_subscription


class CodexDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillz codex discovery ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "OpenAI" / "Codex" / "bin"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "APPDATA": str(self.root / 'Roaming'), "CODEX_CLI_PATH": "", "CODEX_INSTALL_DIR": "", "PROCESSOR_ARCHITEW6432": ""}).start()
        patch.object(Path, 'home', return_value=self.root / 'home').start()
        patch.object(codex_subscription.platform, 'machine', return_value='AMD64').start()
        patch.object(codex_subscription, '_MACOS_APPLICATIONS', self.root / 'Applications').start()
        patch.object(codex_subscription.sys, "platform", "win32").start()
        self.which = patch.object(codex_subscription.shutil, "which", return_value=None).start()

    def executable(self, relative, mtime=100):
        candidate = self.bin / relative
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"fixture")
        os.utime(candidate, (mtime, mtime))
        return str(candidate)

    def test_windows_finds_versioned_bundle_without_path(self):
        expected = self.executable("runtime-hash/codex.exe")
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_windows_prefers_newest_binary_over_hash_order_and_legacy(self):
        self.executable("zz-old/codex.exe", 100)
        expected = self.executable("aa-new/codex.exe", 200)
        self.executable("codex.exe", 300)
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_windows_supports_legacy_unversioned_bundle(self):
        expected = self.executable("codex.exe")
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_incomplete_runtime_directory_is_ignored(self):
        (self.bin / "partial-runtime").mkdir(parents=True)
        expected = self.executable("complete-runtime/codex.exe")
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_discovery_refreshes_after_runtime_update(self):
        old = self.executable("old/codex.exe", 100)
        self.assertEqual(codex_subscription.resolve_codex_executable(), old)
        new = self.executable("new/codex.exe", 200)
        self.assertEqual(codex_subscription.resolve_codex_executable(), new)

    def test_path_installation_keeps_precedence(self):
        expected = self.executable("path-install/codex.exe", 100)
        self.which.return_value = expected
        self.executable("bundled/codex.exe", 200)
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_explicit_executable_with_spaces_keeps_precedence(self):
        expected = self.executable("custom install/codex.exe", 100)
        os.environ["CODEX_CLI_PATH"] = expected
        self.which.return_value = self.executable("path-install/codex.exe", 200)
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_missing_saved_path_does_not_silently_use_a_different_cli(self):
        os.environ["CODEX_CLI_PATH"] = str(self.root / "moved" / "codex.exe")
        self.executable("fallback/codex.exe")
        with self.assertRaisesRegex(RuntimeError, "configured Codex CLI was not found"):
            codex_subscription.resolve_codex_executable()

    def test_missing_windows_installation_has_actionable_error(self):
        status = codex_subscription.codex_subscription_status()
        self.assertFalse(status["available"])
        self.assertIn("CODEX_CLI_PATH", status["error"])

    def test_disappearing_runtime_does_not_hide_legacy_installation(self):
        self.executable("removed/codex.exe")
        expected = self.executable("codex.exe")
        original_stat = Path.stat

        def stat(candidate, *args, **kwargs):
            if candidate.parent.name == "removed":
                raise FileNotFoundError(str(candidate))
            return original_stat(candidate, *args, **kwargs)

        with patch.object(Path, "stat", stat):
            self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_macos_bundle_resolution_is_preserved(self):
        expected = self.executable("mac-bundle/codex")
        with patch.object(codex_subscription.sys, "platform", "darwin"), patch.object(
            codex_subscription, "_MACOS_BUNDLED_CODEX", Path(expected)
        ):
            self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_linux_does_not_select_a_windows_bundle(self):
        self.executable("codex.exe")
        with patch.object(codex_subscription.sys, "platform", "linux"):
            with self.assertRaisesRegex(RuntimeError, "Codex CLI was not found"):
                codex_subscription.resolve_codex_executable()

    def create_file(self, candidate):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b'fixture')
        return str(candidate)

    def test_current_macos_bundle_without_shell_path(self):
        expected = self.create_file(self.root / 'Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
        with patch.object(codex_subscription.sys, 'platform', 'darwin'):
            self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_macos_user_applications_and_legacy_layout(self):
        expected = self.create_file(Path.home() / 'Applications/Codex.app/Contents/Resources/codex')
        with patch.object(codex_subscription.sys, 'platform', 'darwin'):
            self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_macos_standalone_default_without_shell_path(self):
        expected = self.create_file(Path.home() / '.local/bin/codex')
        with patch.object(codex_subscription.sys, 'platform', 'darwin'):
            self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_windows_standalone_default_without_shell_path(self):
        expected = self.create_file(self.root / 'Programs/OpenAI/Codex/bin/codex.exe')
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_custom_standalone_install_dir(self):
        directory = self.root / 'custom standalone'
        os.environ['CODEX_INSTALL_DIR'] = str(directory)
        expected = self.create_file(directory / 'codex.exe')
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_windows_npm_path_shim_resolves_optional_native_package(self):
        prefix = self.root / 'npm prefix'
        self.which.return_value = self.create_file(prefix / 'codex.cmd')
        expected = self.create_file(prefix / 'node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe')
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_windows_npm_nested_optional_native_package(self):
        prefix = self.root / 'Roaming/npm'
        expected = self.create_file(prefix / 'node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe')
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_windows_npm_arm64_legacy_vendor_layout(self):
        prefix = self.root / 'Roaming/npm'
        expected = self.create_file(prefix / 'node_modules/@openai/codex/vendor/aarch64-pc-windows-msvc/bin/codex.exe')
        with patch.object(codex_subscription.platform, 'machine', return_value='ARM64'):
            self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_incomplete_npm_shim_falls_back_to_native_desktop_install(self):
        self.which.return_value = self.create_file(self.root / 'npm/codex.cmd')
        expected = self.executable('desktop/codex.exe')
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_windows_arm64_can_use_x64_npm_install(self):
        expected = self.create_file(self.root / 'Roaming/npm/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe')
        with patch.object(codex_subscription.platform, 'machine', return_value='ARM64'):
            self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_windows_arm64_discovery_from_an_emulated_x64_host(self):
        os.environ['PROCESSOR_ARCHITEW6432'] = 'ARM64'
        expected = self.create_file(self.root / 'Roaming/npm/node_modules/@openai/codex-win32-arm64/vendor/aarch64-pc-windows-msvc/bin/codex.exe')
        self.assertEqual(codex_subscription.resolve_codex_executable(), expected)

    def test_windows_rejects_explicit_shell_launcher(self):
        os.environ['CODEX_CLI_PATH'] = self.create_file(self.root / 'codex.cmd')
        with self.assertRaisesRegex(RuntimeError, 'Choose codex.exe'):
            codex_subscription.resolve_codex_executable()


if __name__ == "__main__":
    unittest.main()
