"""Synthetic CLI identity, confirmation and guarded admission regressions."""
from __future__ import annotations

import json
import os
import subprocess
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

    def enable_openapi(self):
        self.cli_config.update(access_key_id='private-ak', access_key_secret='private-sk')
        self.write_cli()

    def test_openapi_binding_survives_console_rotation_without_reconfirming_models(self):
        self.enable_openapi()
        self.bind()
        self.confirm()
        proof = self.binding.identity(self.route)
        self.assertEqual(proof.context_version, 2)
        for token in ('another-account-browser-token', '', None):
            self.cli_config['access_token'] = token
            self.write_cli()
            self.assertEqual(self.binding.identity(self.route), proof)
            self.assertTrue(self.binding.protection(self.route, None))
        saved = self.binding.path.read_text(encoding='utf-8')
        self.assertEqual(json.loads(saved)['version'], 2)
        for private in ('private-ak', 'private-sk', 'private-console-token'):
            self.assertNotIn(private, saved)

    def test_openapi_binding_ignores_cli_preferences_and_unused_model_credentials(self):
        self.enable_openapi()
        self.bind()
        self.confirm()
        proof = self.binding.current_proof()
        self.cli_config.update(language='en-US', telemetry=False, output='json',
                               default_image_model='other-image-model', api_key='other-model-key',
                               base_url='https://other-model.example/v1', timeout=30)
        self.write_cli()
        self.assertEqual(self.binding.current_proof(), proof)
        self.assertTrue(self.binding.protection(self.route, None))


    def test_openapi_identity_changes_revoke_confirmation_even_if_restored(self):
        self.enable_openapi()
        for field, value in (('access_key_id', 'other-ak'), ('access_key_secret', 'other-sk'),
                             ('security_token', 'new-sts'), ('console_switch_agent', 67890),
                             ('console_site', 'international'), ('workspace_id', 'other-ws')):
            with self.subTest(field=field):
                self.bind()
                self.confirm()
                old = dict(self.cli_config)
                self.cli_config[field] = value
                self.write_cli()
                self.assertIsNone(self.binding.identity(self.route))
                self.cli_config = old
                self.write_cli()
                self.assertIsNone(self.binding.identity(self.route))
        self.bind()
        self.confirm()
        old = self.cli_config.pop('access_key_secret')
        self.write_cli()
        self.assertIsNone(self.binding.identity(self.route))
        self.cli_config['access_key_secret'] = old
        self.write_cli()
        self.assertIsNone(self.binding.identity(self.route))

    def test_legacy_binding_does_not_silently_adopt_new_openapi_credentials(self):
        self.bind()
        self.confirm()
        self.assertEqual(json.loads(self.binding.path.read_text())['version'], 1)
        self.enable_openapi()
        self.assertIsNone(self.binding.identity(self.route))
        self.bind()
        self.assertEqual(json.loads(self.binding.path.read_text())['version'], 2)
        self.assertFalse(self.binding.protection(self.route, None))

    def test_old_binding_that_already_contains_openapi_still_uses_old_token_identity(self):
        self.enable_openapi()
        self.bind()
        record = json.loads(self.binding.path.read_text())
        record.update(version=1, cli_fingerprint=self.source._context(version=1)[2])
        self.binding.path.write_text(json.dumps(record))
        proof = self.binding.current_proof()
        self.assertIsNotNone(proof)
        self.assertEqual(proof.context_version, 1)
        self.cli_config['access_token'] = 'new-token'
        self.write_cli()
        self.assertIsNone(self.binding.current_proof())

    def test_openapi_incomplete_credentials_cannot_create_binding(self):
        self.enable_openapi()
        for value in ('', None, [], 'with whitespace'):
            self.cli_config['access_key_secret'] = value
            self.write_cli()
            with self.subTest(value=value), self.assertRaises(ToolError):
                self.bind()

    def test_official_legacy_openapi_field_aliases_bind_and_use_same_snapshot(self):
        self.cli_config.update(openapi_access_key_id='legacy-ak', openapi_access_key_secret='legacy-sk')
        self.write_cli()
        self.bind()
        proof = self.binding.current_proof()
        self.assertEqual(proof.context_version, 2)
        self.assertEqual(json.loads(self.source._renewal_input(proof))['credentials'],
                         {'accessKeyId': 'legacy-ak', 'accessKeySecret': 'legacy-sk'})
        self.cli_config.update(access_key_id='new-ak', access_key_secret='')
        self.write_cli()
        self.bind()
        proof = self.binding.current_proof()
        self.assertEqual(json.loads(self.source._renewal_input(proof))['credentials'],
                         {'accessKeyId': 'new-ak', 'accessKeySecret': 'legacy-sk'})
        del self.cli_config['openapi_access_key_secret']
        self.write_cli()
        with self.assertRaises(ToolError):
            self.bind()

    def test_openapi_child_uses_private_pipe_and_only_its_own_cached_token(self):
        self.enable_openapi()
        self.bind()
        proof = self.binding.current_proof()
        source = CLIQuotaSource(config_dir=self.config_dir)
        script = ('import json,sys,os; r=json.load(sys.stdin); '
                  'assert r["credentials"]["accessKeyId"]=="private-ak"; '
                  'assert r["credentials"]["accessKeySecret"]=="private-sk"; '
                  'assert r["switchAgent"]==12345; '
                  'assert r["token"]==({expected}); '
                  'assert "DASHSCOPE_API_KEY" not in os.environ; '
                  'print(json.dumps({{"token":"private-generated-token","observation":[]}}))')
        popen = subprocess.Popen
        def capture(command, **kwargs):
            self.assertEqual(kwargs['stdin'], subprocess.PIPE)
            self.assertEqual(kwargs['creationflags'], subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return popen(command, **kwargs)
        for expected in ('None', repr('private-generated-token')):
            child = [sys.executable, '-c', script.format(expected=expected)]
            with mock.patch.object(source, '_renewal_command', return_value=child), \
                 mock.patch('subtitle_tool.mt_bailian_quota.subprocess.Popen', side_effect=capture), \
                 mock.patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'unrelated-private-key'}):
                self.assertEqual(source.query(control=self.control, timeout=3, proof=proof), b'[]')
            self.cli_config['access_token'] = 'unrelated-browser-account-token'
            self.write_cli()
    def test_openapi_token_cache_is_not_shared_with_another_confirmed_context(self):
        self.enable_openapi()
        self.bind()
        source = CLIQuotaSource(config_dir=self.config_dir)
        script = ('import json,sys; r=json.load(sys.stdin); assert r["token"] is None; '
                  'print(json.dumps({"token":"generated-token","observation":[]}))')
        with mock.patch.object(source, '_renewal_command', return_value=[sys.executable, '-c', script]):
            source.query(control=self.control, timeout=3, proof=self.binding.current_proof())
            self.cli_config['access_key_id'] = 'new-account-ak'
            self.write_cli()
            self.bind()
            source.query(control=self.control, timeout=3, proof=self.binding.current_proof())

    def test_openapi_child_identity_change_and_invalid_reply_do_not_publish_token(self):
        self.enable_openapi()
        self.bind()
        source = CLIQuotaSource(config_dir=self.config_dir)
        proof = self.binding.current_proof()
        script = ('import json,sys; from pathlib import Path; json.load(sys.stdin); '
                  'p=Path(sys.argv[1]); c=json.loads(p.read_text()); c["access_key_id"]="other-ak"; '
                  'p.write_text(json.dumps(c)); '
                  'print(json.dumps({"token":"private-untrusted-token","observation":[]}))')
        child = [sys.executable, '-c', script, str(self.config_path)]
        with mock.patch.object(source, '_renewal_command', return_value=child), self.assertRaises(QuotaSourceError):
            source.query(control=self.control, timeout=3, proof=proof)
        self.assertIsNone(source._token)
        self.write_cli()
        for reply in ('{"token":"private-untrusted-token","observation":{}}',
                      '{"token":"one","token":"two","observation":[]}',
                      '{"token":null,"observation":[]}'):
            script = 'import sys; sys.stdin.read(); print(' + repr(reply) + ')'
            with mock.patch.object(source, '_renewal_command', return_value=[sys.executable, '-c', script]), \
                 self.assertRaises(QuotaSourceError):
                source.query(control=self.control, timeout=3, proof=proof)
            self.assertIsNone(source._token)

    def test_openapi_child_cancel_and_timeout_reap_process_with_pending_stdin(self):
        self.enable_openapi()
        self.cli_config['security_token'] = 'x' * 16000
        self.write_cli()
        self.bind()
        proof = self.binding.current_proof()
        source = CLIQuotaSource(config_dir=self.config_dir)
        popen, children = subprocess.Popen, []
        def capture(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child
        for cancel in (False, True):
            control = MTControl(threading.Event())
            timer = threading.Timer(.2, control.cancel_user) if cancel else None
            if timer:
                timer.start()
            try:
                with mock.patch.object(source, '_renewal_command',
                                       return_value=[sys.executable, '-c', 'import time; time.sleep(30)']), \
                     mock.patch('subtitle_tool.mt_bailian_quota.subprocess.Popen', side_effect=capture), \
                     self.assertRaises((QuotaSourceError, ToolError)):
                    source.query(control=control, timeout=2 if cancel else .3, proof=proof)
            finally:
                if timer:
                    timer.cancel()
            self.assertIsNotNone(children[-1].poll())

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
