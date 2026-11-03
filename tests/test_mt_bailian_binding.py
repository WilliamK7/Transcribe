"""Synthetic CLI identity, confirmation and guarded admission regressions."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.mt_backend import MTBackend, key_fingerprint
from subtitle_tool.mt_bailian_binding import LocalBailianBinding
from subtitle_tool.mt_bailian_quota import BailianQuota, CLIQuotaSource, QuotaSourceError
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_routing import RouteEntry, RouteFailure


def row(model='qwen-test', stop=True):
    return {'model': model, 'type': 'Text', 'remaining': 30000, 'total': 30000,
            'remainingPercent': 100, 'expires': '2099-12-31', 'autoStop': stop}


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config_dir, self.data = root / 'cli', root / 'data'
        self.config_dir.mkdir()
        self.data.mkdir()
        self.config_path = self.config_dir / 'config.json'
        self.cli_config = {'access_token': 'private-console-token', 'console_site': 'domestic',
                           'console_region': 'cn-beijing', 'console_switch_agent': 12345,
                           'workspace_id': 'ws-test', 'api_key': 'private-cli-key'}
        self.write_cli()
        self.key = 'private-asr-key'
        self.scope = ['ws-test', 'beijing']
        self.fingerprint = key_fingerprint(self.key)
        self.route = RouteEntry('free', MTBackend('bailian',
            'https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
            'qwen-test', self.fingerprint, 5), 'free')
        self.control = MTControl(threading.Event())
        self.source = CLIQuotaSource(config_dir=self.config_dir)
        self.source.query = mock.Mock(return_value=json.dumps([row()]).encode())
        self.binding = LocalBailianBinding(self.data, source=self.source, get_key=lambda: self.key,
                                           get_scope=lambda: tuple(self.scope))
        self.accounts = mock.patch.dict('subtitle_tool.mt_bailian_quota._ACCOUNTS', {}, clear=True)
        self.accounts.start()
        self.addCleanup(self.accounts.stop)

    def write_cli(self):
        self.config_path.write_text(json.dumps(self.cli_config), encoding='utf-8')

    def bind(self):
        self.binding.confirm_identity(key_fingerprint=self.fingerprint, workspace='ws-test')

    def confirm(self, models=None):
        self.binding.confirm_protection(models or ['qwen-test'], control=self.control, timeout=5)

    def quota(self):
        return BailianQuota(self.control, identity=self.binding.identity,
                            protection=self.binding.protection, source=self.source,
                            observation=self.binding.observe)

    def test_confirmation_stores_only_fingerprints_and_allows_guarded_dispatch(self):
        self.bind()
        self.source.query.assert_not_called()
        self.assertFalse(self.quota().potential(self.route))
        self.confirm()
        saved = (self.data / 'bailian-quota-binding.json').read_text(encoding='utf-8')
        self.assertNotIn(self.key, saved)
        self.assertNotIn('private-console-token', saved)
        self.assertNotIn('private-cli-key', saved)
        self.assertEqual(json.loads(saved)['source'], 'user_confirmed')
        proof = self.binding.identity(self.route)
        self.assertIsNotNone(proof)
        self.assertTrue(self.source.bound_to(proof))
        quota = self.quota()
        self.assertTrue(quota.potential(self.route))
        with quota.reserve(self.route, 5, estimate=100) as reservation:
            reservation.claim()
            reservation.mark_sent()
            reservation.settle({'input_tokens': 20, 'output_tokens': 30})
        self.assertEqual(self.source.query.call_count, 2)

    def test_selected_profile_or_credentials_change_invalidates_without_query(self):
        self.bind()
        self.confirm()
        for change in ({'access_token': 'new-private-token'}, {'api_key': 'new-private-cli-key'},
                       {'workspace_id': 'ws-other'}, {'console_switch_agent': 67890},
                       {'console_site': 'international'}, {'active_config': 'another-profile'}):
            with self.subTest(change=list(change)):
                original = dict(self.cli_config)
                self.cli_config.update(change)
                self.write_cli()
                self.assertIsNone(self.binding.identity(self.route))
                before = self.source.query.call_count
                self.assertFalse(self.quota().potential(self.route))
                self.assertEqual(self.source.query.call_count, before)
                self.cli_config = original
                self.write_cli()
                self.bind()
                self.confirm()
        self.key = 'changed-asr-key'
        self.assertIsNone(self.binding.identity(self.route))
        self.assertFalse(self.quota().potential(self.route))

    def test_named_profile_fingerprint_uses_selected_context(self):
        self.cli_config = {'active_config': 'named', 'named': self.cli_config,
                           'other': {'access_token': 'other-private-token'}}
        self.write_cli()
        self.bind()
        self.assertIsNotNone(self.binding.identity(self.route))
        self.cli_config['other']['access_token'] = 'changed-unselected-token'
        self.write_cli()
        self.assertIsNotNone(self.binding.identity(self.route))
        self.cli_config['named']['access_token'] = 'changed-selected-token'
        self.write_cli()
        self.assertIsNone(self.binding.identity(self.route))

    def test_detected_logout_or_key_rotation_cannot_revive_old_confirmation(self):
        self.bind()
        self.confirm()
        original_token = self.cli_config['access_token']
        self.cli_config['access_token'] = ''
        self.write_cli()
        self.assertIsNone(self.binding.identity(self.route))
        self.cli_config['access_token'] = original_token
        self.write_cli()
        self.assertIsNone(self.binding.identity(self.route))
        self.bind()
        self.confirm()
        original_key = self.key
        self.key = 'rotated-asr-key'
        self.assertIsNone(self.binding.identity(self.route))
        self.key = original_key
        self.assertIsNone(self.binding.identity(self.route))
        self.bind()
        self.assertIsNotNone(self.binding.identity(self.route))

    def test_asr_workspace_and_region_changes_invalidate_confirmation(self):
        self.bind()
        self.confirm()
        for index, value in ((0, 'ws-other'), (1, 'singapore')):
            original = self.scope[index]
            self.scope[index] = value
            self.assertIsNone(self.binding.identity(self.route))
            self.scope[index] = original
            self.assertIsNone(self.binding.identity(self.route))
            with self.assertRaises(ToolError):
                self.confirm()
            self.bind()
            self.confirm()

    def test_confirm_protection_requires_live_observed_true_and_fails_atomically(self):
        self.bind()
        for stop in (False, None, 'unsupported'):
            self.source.query.return_value = json.dumps([row(stop=stop)]).encode()
            with self.assertRaises(ToolError) as caught:
                self.confirm()
            self.assertNotIn('private', str(caught.exception))
            self.assertFalse(self.quota().potential(self.route))
        self.source.query.return_value = json.dumps([row(), row('another', False)]).encode()
        with self.assertRaises(ToolError):
            self.confirm(['qwen-test', 'another'])
        self.assertFalse(self.quota().potential(self.route))

    def test_unexpected_source_errors_are_not_exposed_to_the_user(self):
        self.bind()
        self.source.query.side_effect = RuntimeError('private-console-token')
        with self.assertRaises(ToolError) as error:
            self.confirm()
        self.assertNotIn('private-console-token', str(error.exception))

    def test_context_changes_during_query_must_not_confirm_or_dispatch(self):
        self.bind()
        def swap(**kwargs):
            self.cli_config['access_token'] = 'new-private-token'
            self.write_cli()
            return json.dumps([row()]).encode()
        self.source.query.side_effect = swap
        with self.assertRaises(ToolError):
            self.confirm()
        self.assertFalse(self.quota().potential(self.route))

    def test_rebinding_clears_models_and_bad_credentials_fail_without_output(self):
        self.bind()
        self.confirm()
        self.bind()
        self.assertFalse(self.quota().potential(self.route))
        self.cli_config['access_token'] = ''
        self.write_cli()
        with self.assertRaises(ToolError) as caught:
            self.bind()
        self.assertNotIn('private', str(caught.exception))

    def test_previously_confirmed_model_seen_off_cannot_be_reused(self):
        self.bind()
        self.confirm()
        self.source.query.return_value = json.dumps([row(stop=False)]).encode()
        quota = self.quota()
        with self.assertRaises(RouteFailure):
            with quota.reserve(self.route, 5, estimate=100):
                pass
        self.source.query.return_value = json.dumps([row(stop=True)]).encode()
        self.assertFalse(self.quota().potential(self.route))

    def test_confirmation_query_revokes_other_model_seen_off_even_if_request_fails(self):
        self.bind()
        self.source.query.return_value = json.dumps([row(), row('other')]).encode()
        self.confirm(['qwen-test', 'other'])
        self.source.query.return_value = json.dumps([row(stop=False), row('other', False)]).encode()
        with self.assertRaises(ToolError):
            self.confirm()
        saved = json.loads((self.data / 'bailian-quota-binding.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['confirmed_models'], [])

    def test_temporarily_missing_cli_config_does_not_permanently_invalidate(self):
        self.bind()
        self.confirm()
        temporary = self.config_path.with_suffix('.tmp')
        self.config_path.rename(temporary)
        self.assertIsNone(self.binding.identity(self.route))
        temporary.rename(self.config_path)
        self.assertIsNotNone(self.binding.identity(self.route))
        self.assertTrue(self.quota().potential(self.route))

    def test_real_child_pins_named_profile_and_private_config_directory(self):
        self.cli_config = {'active_config': 'named', 'named': self.cli_config}
        self.write_cli()
        source = CLIQuotaSource(config_dir=self.config_dir)
        binding = LocalBailianBinding(self.data, source=source, get_key=lambda: self.key,
                                      get_scope=lambda: tuple(self.scope))
        binding.confirm_identity(key_fingerprint=self.fingerprint, workspace='ws-test')
        script = ('import json, os, sys; from pathlib import Path; '
                  'cfg=json.loads((Path(os.environ["BAILIAN_CONFIG_DIR"])/"config.json").read_text()); '
                  'print(json.dumps({"profile":sys.argv[-2:]==["--config","named"],'
                  '"config":cfg.get("active_config")=="named"}))')
        with mock.patch.object(source, '_command', return_value=[sys.executable, '-c', script]), \
             mock.patch.dict(os.environ, {'BAILIAN_CONFIG_DIR': str(self.data)}):
            reply = source.query(control=self.control, timeout=3, proof=binding.current_proof())
        self.assertEqual(json.loads(reply), {'profile': True, 'config': True})

    def test_child_context_change_is_rejected_after_process_completes(self):
        source = CLIQuotaSource(config_dir=self.config_dir)
        binding = LocalBailianBinding(self.data, source=source, get_key=lambda: self.key,
                                      get_scope=lambda: tuple(self.scope))
        binding.confirm_identity(key_fingerprint=self.fingerprint, workspace='ws-test')
        script = ('import json, os; from pathlib import Path; '
                  'p=Path(os.environ["BAILIAN_CONFIG_DIR"])/"config.json"; '
                  'cfg=json.loads(p.read_text()); cfg["access_token"]="different-private-token"; '
                  'p.write_text(json.dumps(cfg)); print("[]")')
        with mock.patch.object(source, '_command', return_value=[sys.executable, '-c', script]), \
             self.assertRaises(QuotaSourceError):
            source.query(control=self.control, timeout=3, proof=binding.current_proof())

    def test_unbound_inspection_respects_custom_cli_config_directory(self):
        source = CLIQuotaSource()
        script = ('import os; from pathlib import Path; '
                  'print(Path(os.environ["BAILIAN_CONFIG_DIR"]).name == "cli")')
        with mock.patch.object(source, '_command', return_value=[sys.executable, '-c', script]), \
             mock.patch.dict(os.environ, {'BAILIAN_CONFIG_DIR': str(self.config_dir)}):
            reply = source.query(control=self.control, timeout=3)
        self.assertEqual(reply.strip(), b'True')

    def test_observation_revokes_other_confirmed_models_but_not_unknown(self):
        self.bind()
        self.source.query.return_value = json.dumps([row(), row('other'), row('uncertain')]).encode()
        self.confirm(['qwen-test', 'other', 'uncertain'])
        self.source.query.return_value = json.dumps([row(), row('other', False), row('uncertain', None)]).encode()
        with self.quota().reserve(self.route, 5, estimate=100) as reservation:
            reservation.claim()
            reservation.mark_sent()
        saved = json.loads((self.data / 'bailian-quota-binding.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['confirmed_models'], ['qwen-test', 'uncertain'])

    def test_rebinding_never_reuses_a_previous_context_observation(self):
        self.bind()
        self.confirm()
        quota = self.quota()
        with quota.reserve(self.route, 5, estimate=100) as reservation:
            reservation.claim()
            reservation.mark_sent()
        self.cli_config['access_token'] = 'changed-private-token'
        self.write_cli()
        self.bind()
        self.confirm()
        self.source.query.return_value = json.dumps([row(stop=False)]).encode()
        with self.assertRaises(RouteFailure):
            with quota.reserve(self.route, 5, estimate=100):
                pass


if __name__ == '__main__':
    unittest.main()
