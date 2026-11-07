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
from subtitle_tool.mt_backend import MTBackend, key_fingerprint, qiniu_free_active
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_transport import HTTPReply, ProcessTransport
from tests.fixtures import configured_backend, TemporaryTest


class QiniuContractTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.backend = MTBackend('qiniu', 'https://api.qnaigc.com/v1', 'qiniu/conatus-free',
                                 key_fingerprint('qiniu-synthetic'), 5)


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
            backend = configured_backend(config, manifest)
            self.assertEqual(backend.credential(config), 'qiniu-synthetic')
            config.set_qiniu_key('changed-qiniu-synthetic')
            with self.assertRaises(ToolError) as error:
                backend.credential(config)
            self.assertEqual(error.exception.code, 'mt_account_changed')


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
