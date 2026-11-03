"""Offline regressions for Qiniu's independently authenticated wire contract."""
from __future__ import annotations

import json
import os
import threading
import io
import base64
from datetime import datetime, timezone
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.config import Config
from subtitle_tool.mt import batch_payload, validate_batch_input
from subtitle_tool.mt_backend import MTBackend, configured_mt, key_fingerprint, qiniu_free_active, saved_mt
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_runtime import BoundedMTHttp
from subtitle_tool.mt_transport import HTTPReply, ProcessTransport
from tests.fixtures import SavedResultFixture, TemporaryTest, result


class QiniuContractTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.backend = MTBackend('qiniu', 'https://api.qnaigc.com/v1', 'qiniu/conatus-free',
                                 key_fingerprint('qiniu-synthetic'), 4)

    def test_qiniu_payload_is_json_object_without_other_provider_controls(self):
        payload = batch_payload(['first', 'second'], **self.backend.request_options())
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['max_tokens'], 4096)
        for field in ('provider', 'plugins', 'enable_thinking', 'seed'):
            self.assertNotIn(field, payload)
        self.assertEqual(payload['model'], 'qiniu/conatus-free')

    def test_new_input_limit_counts_complete_serialized_payload(self):
        validate_batch_input(['x' * 10000], request_options=self.backend.request_options())
        with self.assertRaises(ToolError) as error:
            validate_batch_input(['x' * 33000], request_options=self.backend.request_options())
        self.assertEqual(error.exception.code, 'mt_input_budget')

    def test_beijing_activity_boundaries_and_invalid_naive_time(self):
        def utc(month, day, hour, minute=0):
            return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)
        self.assertFalse(qiniu_free_active(utc(10, 23, 15, 59)))
        self.assertTrue(qiniu_free_active(utc(10, 23, 16)))
        self.assertTrue(qiniu_free_active(utc(11, 30, 15, 59)))
        self.assertFalse(qiniu_free_active(utc(11, 30, 16)))
        with self.assertRaises(ValueError):
            qiniu_free_active(datetime(2026, 10, 24))

    def test_credentials_and_snapshot_are_isolated_from_other_providers(self):
        config = Config(self.path)
        config.set_qiniu_key('qiniu-synthetic')
        config.set_key('bailian-synthetic')
        (config.data / 'mt-key.txt').write_text('openrouter-synthetic', encoding='utf-8')
        config.settings.mt_provider = 'qiniu'
        config.settings.mt_model = 'deepseek/deepseek-v4.1-flash'
        manifest = {'region': 'beijing', 'access_mode': 'workspace', 'workspace_id': 'space',
                    'account': key_fingerprint('bailian-synthetic')}
        with mock.patch.dict(os.environ, {'QINIU_API_KEY': '', 'OPENAI_API_KEY': '', 'DASHSCOPE_API_KEY': ''}):
            backend = configured_mt(config, manifest)
            self.assertEqual(backend.credential(config), 'qiniu-synthetic')
            self.assertEqual(saved_mt({**manifest, 'mt_snapshot': backend.snapshot(),
                                       'translation_strategy_version': 4}), backend)
            config.set_qiniu_key('changed-qiniu-synthetic')
            with self.assertRaises(ToolError) as error:
                backend.credential(config)
            self.assertEqual(error.exception.code, 'mt_account_changed')

    def test_qiniu_does_not_query_openrouter_quota_and_retry_is_bounded(self):
        class Transport:
            def __init__(self):
                self.calls = []
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs['timeout']))
                return HTTPReply(429, b'{}')
        transport = Transport()
        control = MTControl(threading.Event())
        http = BoundedMTHttp(self.backend, control, transport=transport, stop_on_failure=False)
        with mock.patch.object(http.recovery, 'failure', return_value=0), \
             mock.patch.object(http.pacer, 'throttle', return_value=(0, 0)), \
             self.assertRaises(ToolError) as error:
            http.json('POST', self.backend.base_url + '/chat/completions', json={})
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(all(method == 'POST' and timeout == 60 for method, _, timeout in transport.calls))
        self.assertNotIn('quota', error.exception.code)

    def test_network_worker_does_not_inherit_any_provider_credentials(self):
        class Process:
            returncode = 0
            stdin = stdout = None
            def communicate(self, *args, **kwargs):
                return json.dumps({'status': 200, 'body': base64.b64encode(b'{}').decode()}).encode(), b''
            def poll(self):
                return 0
        with mock.patch.dict(os.environ, {'QINIU_API_KEY': 'synthetic-qiniu',
                                         'OPENAI_API_KEY': 'synthetic-openai',
                                         'DASHSCOPE_API_KEY': 'synthetic-bailian'}), \
             mock.patch('subtitle_tool.mt_transport.subprocess.Popen', return_value=Process()) as popen:
            ProcessTransport().request('POST', 'https://example.invalid/v1/chat/completions',
                                       headers={}, json={}, control=MTControl(threading.Event()))
        environment = popen.call_args.kwargs['env']
        self.assertNotIn('QINIU_API_KEY', environment)
        self.assertNotIn('OPENAI_API_KEY', environment)
        self.assertNotIn('DASHSCOPE_API_KEY', environment)

    def test_dispatch_callback_runs_once_immediately_before_sending_payload(self):
        events = []
        class Process:
            returncode = 0
            stdin = stdout = None
            def communicate(self, *args, **kwargs):
                events.append('send' if args and args[0] is not None else 'reap')
                return json.dumps({'status': 200, 'body': base64.b64encode(b'{}').decode()}).encode(), b''
            def poll(self):
                return 0
        with mock.patch('subtitle_tool.mt_transport.subprocess.Popen', return_value=Process()):
            ProcessTransport().request('POST', 'https://example.invalid/v1/chat/completions', headers={}, json={},
                control=MTControl(threading.Event()), on_dispatch=lambda: events.append('dispatch'))
        self.assertEqual(events[:2], ['dispatch', 'send'])
        self.assertEqual(events.count('dispatch'), 1)

    def test_cli_configure_uses_hidden_qiniu_input_without_changing_other_keys(self):
        from subtitle_tool.main import main
        config = Config(self.path)
        config.set_key('bailian-synthetic')
        with mock.patch('subtitle_tool.main.Config', return_value=config), \
             mock.patch('subtitle_tool.main.sys.stdin.isatty', return_value=True), \
             mock.patch('subtitle_tool.main.getpass.getpass', return_value='qiniu-synthetic') as getpass, \
             mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(main(['--configure-mt', 'qiniu']), 0)
        self.assertEqual(getpass.call_count, 1)
        self.assertNotIn('qiniu-synthetic', stderr.getvalue())
        self.assertEqual(config.get_qiniu_key(), 'qiniu-synthetic')
        self.assertEqual(config.get_key(), 'bailian-synthetic')
        self.assertEqual(config.settings.mt_provider, 'pool')

    def test_cli_check_sends_only_synthetic_text_and_prints_no_response(self):
        from subtitle_tool.main import main
        config = Config(self.path)
        config.set_qiniu_key('qiniu-synthetic')
        calls = []
        class FakeCloud:
            def __init__(self, key, backend, cancel, **kwargs):
                self.backend = backend
            def translate_batch(self, fragments, source_lang):
                calls.append((self.backend.provider, fragments, source_lang))
                return object()
        with mock.patch('subtitle_tool.main.Config', return_value=config), \
             mock.patch('subtitle_tool.api.OpenAICompatible', FakeCloud), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(main(['--check-mt', 'qiniu']), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'qiniu')
        self.assertNotIn('qiniu-synthetic', stdout.getvalue())

    def test_cli_check_default_pool_uses_synthetic_input_and_does_not_create_a_job(self):
        from subtitle_tool.main import main
        config = Config(self.path)
        config.settings.workspace_id = 'space'
        config.set_key('bailian-synthetic')
        config.set_qiniu_key('qiniu-synthetic')
        calls = []
        class Transport:
            def request(self, method, url, **kwargs):
                data = json.loads(kwargs['json']['messages'][1]['content'])
                calls.append((url, data))
                content = json.dumps({'translations': {'0': '缓存保存先前计算的结果。'}})
                response = {'model': kwargs['json']['model'], 'choices': [
                    {'finish_reason': 'stop', 'message': {'content': content}}]}
                return HTTPReply(200, json.dumps(response).encode('utf-8'))
        with mock.patch('subtitle_tool.main.Config', return_value=config), \
             mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()), \
             mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=True), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(main(['--check-mt', '--mt-paid', 'deny']), 0)
        self.assertEqual(len(calls), 1)
        self.assertIn('previously computed', calls[0][1]['sentences'][0]['text'])
        self.assertNotIn('qiniu-synthetic', stdout.getvalue())
        self.assertFalse((config.data / 'jobs.sqlite').exists())


class QiniuPipelineTests(SavedResultFixture):
    def setUp(self):
        super().setUp()
        self.config.set_qiniu_key('qiniu-synthetic')
        self.config.settings.mt_provider = 'qiniu'
        self.config.settings.mt_model = 'deepseek/deepseek-v4.1-flash'
        self.config.settings.mt_workers = 1

    def test_reflow_resumes_saved_identity_and_commits_complete_numbered_group(self):
        from subtitle_tool.engine import Engine
        self.save_result(result(('The cache avoids repeated computation.', 'en')))
        Engine(self.config, self.store).reflow(self.job_id)
        self.config.settings.mt_provider = 'bailian'
        calls = []
        class Transport:
            def request(self, method, url, **kwargs):
                payload = kwargs['json']
                calls.append((method, url, payload))
                targets = json.loads(payload['messages'][1]['content'])['targets']
                content = json.dumps({'translations': {key: '缓存避免重复计算。' for key in targets}})
                body = {'model': 'routed-synthetic', 'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
                return HTTPReply(200, json.dumps(body).encode())
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', return_value=Transport()):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'completed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 'https://api.qnaigc.com/v1/chat/completions')
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertEqual(self.store.job(self.job_id)['manifest']['mt_snapshot']['provider'], 'qiniu')
