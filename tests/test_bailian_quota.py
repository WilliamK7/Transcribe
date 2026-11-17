"""Offline quota schema and account-context boundaries; no real credentials."""
import json
import subprocess
import sys
import threading
from datetime import date
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from subtitle_tool.bailian_quota import CLIQuotaSource, ModelAccessSource, QuotaSourceError, VerifiedIdentity, parse_observation
from subtitle_tool.common import atomic_json
from subtitle_tool.mt_admission import ModelEvaluator
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_transport import HTTPReply
from tests.fixtures import TemporaryTest
from tests.test_bailian_free import BailianFreeFixture


class ModelAccessTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = SimpleNamespace(settings=SimpleNamespace(workspace_id='offline-space'),
                                      get_key=lambda: 'synthetic-key')
        self.control = MTControl(threading.Event())

    def limits_reply(self, rows):
        return HTTPReply(200, json.dumps({'output': {'quotas': rows}}).encode())

    def test_unreadable_permission_catalog_does_not_block_model_limits(self):
        calls = []
        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if urlsplit(url).path == '/api/v1/models/permissions':
                return HTTPReply(403, b'{"message":"private catalog error"}')
            return self.limits_reply([{'model': 'trial-model', 'model_limit': {'request_limit': 60}}])
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport.request', side_effect=request):
            limits = ModelAccessSource().inspect(self.config, 'trial-model', self.control)
        self.assertEqual(limits, {'rpm': 60, 'tpm': None, 'workspace_rpm': None, 'workspace_tpm': None})
        self.assertEqual(len(calls), 1)
        method, url, kwargs = calls[0]
        self.assertEqual(method, 'GET')
        self.assertEqual(urlsplit(url).netloc, 'offline-space.cn-beijing.maas.aliyuncs.com')
        self.assertEqual(urlsplit(url).path, '/api/v1/models/limits')
        self.assertEqual(parse_qs(urlsplit(url).query), {'model': ['trial-model'], 'page_size': ['100']})
        self.assertEqual(kwargs, {'headers': {'Authorization': 'Bearer synthetic-key'},
                                  'control': self.control, 'timeout': 15, 'drain': False})

    def test_limits_preserve_model_and_shared_ceiling_in_requests_per_minute(self):
        rows = [{'model': 'trial-model',
                 'model_limit': {'request_limit': 2, 'request_limit_period': 1,
                                 'usage_limit': 20000, 'usage_limit_period': 30},
                 'workspace_limit': {'request_limit': 40, 'request_limit_period': 60,
                                     'usage_limit': 150000, 'usage_limit_period': 300}},
                {'model': 'different-model', 'model_limit': {'request_limit': 1, 'usage_limit': 1}}]
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport.request', return_value=self.limits_reply(rows)):
            limits = ModelAccessSource().inspect(self.config, 'trial-model', self.control)
        self.assertEqual(limits, {'rpm': 40, 'tpm': 30000, 'workspace_rpm': 40, 'workspace_tpm': 30000})

    def test_limits_failure_still_blocks_admission_without_leaking_response(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), mock.patch('subtitle_tool.mt_transport.ProcessTransport.request',
                    return_value=HTTPReply(status, b'private-key-or-service-message')) as request:
                with self.assertRaises(QuotaSourceError) as raised:
                    ModelAccessSource().inspect(self.config, 'trial-model', self.control)
                self.assertEqual(str(raised.exception), 'Quota query failed')
                self.assertEqual(urlsplit(request.call_args.args[1]).path, '/api/v1/models/limits')


class ModelAccessAdmissionTests(BailianFreeFixture):
    def test_readable_limits_do_not_admit_model_when_inference_is_forbidden(self):
        self.rows = self.rows[:1]
        self.guard.access = ModelAccessSource()
        calls = []
        def request(method, url, **kwargs):
            calls.append((method, url))
            if method == 'GET' and urlsplit(url).path == '/api/v1/models/limits':
                return HTTPReply(200, json.dumps({'output': {'quotas': [
                    {'model': 'trial-a', 'model_limit': {'request_limit': 60, 'usage_limit': 1000000}}
                ]}}).encode())
            return HTTPReply(403, b'{"error":{"code":"permission_denied","message":"private error"}}')
        self.guard.evaluator = ModelEvaluator(self.config, self.guard.control, judge='independent/judge',
                                             transport=SimpleNamespace(request=request))
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport.request', side_effect=request):
            self.guard.refresh()
        self.assertEqual([method for method, _ in calls], ['GET', 'POST'])
        self.assertFalse(self.active())
        self.assertEqual(self.guard.state['models']['trial-a']['status'], 'admission_rejected')
        self.assertTrue(all(p.get('off') for p in self.guard.admin.registry()['providers']))
        self.assertNotIn('private error', self.guard.trial_state.path.read_text(encoding='utf-8'))


class QuotaObservationTests(TemporaryTest):
    def row(self, **changes):
        return dict({'model': 'trial-model', 'type': 'Text', 'remaining': 100, 'total': 1000,
                     'remainingPercent': 10, 'expires': '2099-01-01'}, **changes)

    def test_ambiguous_or_malformed_quota_is_never_admitted(self):
        for changes in ({'remaining': True}, {'remaining': 1001}, {'remaining': -1}, {'remaining': 0},
                        {'total': 0}, {'type': 'Audio'},
                        {'expires': '2099-02-30'}, {'remainingPercent': None}):
            with self.subTest(changes=changes):
                value = parse_observation(json.dumps([self.row(**changes)]).encode())['trial-model']
                self.assertFalse(value.usable(date(2026, 11, 10)))
        duplicate = json.dumps([self.row(), self.row(remaining=0)]).encode()
        self.assertEqual(parse_observation(duplicate), {})
        with self.assertRaises(ValueError):
            parse_observation(b'[{"model":"trial-model","remaining":0,"remaining":100}]')

    def source(self):
        self.cli_path = self.path / 'config.json'
        self.context = {'access_key_id': 'synthetic-id', 'access_key_secret': 'synthetic-secret',
                        'console_region': 'cn-beijing', 'console_site': 'domestic',
                        'workspace_id': 'offline-space', 'access_token': 'unrelated-token'}
        atomic_json(self.cli_path, self.context)
        source = CLIQuotaSource(config_dir=self.path)
        proof = VerifiedIdentity('offline-space', 'a' * 64, 'cn-beijing',
                                 source._context()[2])
        return source, proof

    def run_query(self, source, proof, script, *, timeout=3):
        children = []
        popen = subprocess.Popen

        def start(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        try:
            with mock.patch.object(source, '_command', return_value=[sys.executable, '-I', '-c', script]), \
                    mock.patch('subtitle_tool.bailian_quota.subprocess.Popen', side_effect=start):
                return source.query(control=MTControl(threading.Event()), timeout=timeout, proof=proof)
        finally:
            for child in children:
                running = child.poll() is None
                if running:
                    child.kill()
                    child.wait()
                self.assertFalse(running, 'Quota query returned before reaping its subprocess')

    def test_query_uses_confirmed_credentials_and_reuses_renewed_token(self):
        row = self.row()
        source, proof = self.source()
        script = ('import json, sys\nrequest = json.load(sys.stdin)\n'
                  'assert request["credentials"]["accessKeyId"] == "synthetic-id"\n'
                  'print(' + repr(json.dumps({'token': 'renewed-token', 'observation': [row]})) + ')')
        result = self.run_query(source, proof, script)
        self.assertEqual(json.loads(result), [row])
        self.assertEqual(json.loads(source._query_input(proof))['token'], 'renewed-token')

    def test_query_sanitizes_cli_failure_and_reaps_timed_out_process(self):
        source, proof = self.source()
        for script, timeout in (
                ('import sys; sys.stderr.write("private" * 65536); sys.exit(3)', 3),
                ('import time; time.sleep(30)', .3)):
            with self.subTest(timeout=timeout), self.assertRaises(QuotaSourceError) as error:
                self.run_query(source, proof, script, timeout=timeout)
            self.assertEqual(str(error.exception), 'Quota query failed')

    def test_renewal_binding_tracks_exact_credentials_and_scope(self):
        source, proof = self.source()
        self.assertTrue(source.bound_to(proof))
        for field, value in (('access_key_secret', 'changed-secret'), ('workspace_id', 'other-space'),
                             ('console_switch_agent', 123), ('console_site', 'international')):
            with self.subTest(field=field):
                atomic_json(self.cli_path, dict(self.context, **{field: value}))
                self.assertFalse(source.bound_to(proof))

    def test_unrelated_browser_token_does_not_rebind_ak_sk_identity(self):
        source, proof = self.source()
        atomic_json(self.cli_path, dict(self.context, access_token='renewed-unrelated-token'))
        self.assertTrue(source.bound_to(proof))
        self.assertNotIn(b'renewed-unrelated-token', source._query_input(proof))

    def test_invalid_selected_profile_is_sanitized_and_unbound(self):
        source, proof = self.source()
        atomic_json(self.cli_path, dict(self.context, active_config='missing-private-profile'))
        self.assertFalse(source.bound_to(proof))
        with self.assertRaises(QuotaSourceError) as raised:
            source._query_input(proof)
        self.assertNotIn('private', str(raised.exception))
