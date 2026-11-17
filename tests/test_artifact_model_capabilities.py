import unittest
from unittest.mock import patch

from artifact_model_host import capabilities


class ArtifactCodexReadinessTests(unittest.TestCase):
    selection = {'provider': 'codex-subscription', 'model': 'fixture'}

    def test_missing_cli_is_reported_before_a_model_call(self):
        with patch('codex_subscription.resolve_codex_executable', side_effect=RuntimeError('CLI missing')), patch('codex_subscription.quick_chatgpt_auth_status') as auth:
            result = capabilities(self.selection)
        self.assertFalse(result['sdkReady'])
        self.assertFalse(result['keyReady'])
        self.assertEqual(result['detail'], 'CLI missing')
        auth.assert_not_called()

    def test_cli_requires_chatgpt_sign_in(self):
        with patch('codex_subscription.resolve_codex_executable', return_value='fixture-cli'), patch('codex_subscription.quick_chatgpt_auth_status', return_value={'authenticated': False}):
            result = capabilities(self.selection)
        self.assertTrue(result['sdkReady'])
        self.assertFalse(result['keyReady'])
        self.assertIn('Sign in', result['detail'])

    def test_signed_in_cli_is_ready_without_a_model_call(self):
        with patch('codex_subscription.resolve_codex_executable', return_value='fixture-cli'), patch('codex_subscription.quick_chatgpt_auth_status', return_value={'authenticated': True}):
            result = capabilities(self.selection)
        self.assertTrue(result['sdkReady'])
        self.assertTrue(result['keyReady'])

    def test_failed_cli_status_is_actionable(self):
        with patch('codex_subscription.resolve_codex_executable', return_value='fixture-cli'), patch('codex_subscription.quick_chatgpt_auth_status', side_effect=RuntimeError('Executable cannot start')):
            result = capabilities(self.selection)
        self.assertFalse(result['sdkReady'])
        self.assertFalse(result['keyReady'])
        self.assertIn('cannot start', result['detail'])
