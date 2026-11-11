"""Offline gateway identity, fee isolation and request regressions."""
from datetime import datetime, timezone
from subtitle_tool.mt_backend import qiniu_free_active
import json
import os
from unittest import mock

from subtitle_tool.common import ToolError, atomic_json
from subtitle_tool.config import Config
from subtitle_tool.mt_backend import MTBackend, key_fingerprint, normalize_mt_url
from subtitle_tool.mt_pool import configured_route, saved_route
from tests.fixtures import set_asr_account, TemporaryTest


class MagpieTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        set_asr_account(self.config, 'synthetic-asr')
        self.config.settings.workspace_id = 'offline-space'
        self.identity = {'region': 'beijing', 'workspace_id': 'offline-space',
                         'access_mode': 'workspace', 'account': key_fingerprint('synthetic-asr')}

    def test_gateway_defaults_and_saved_identity(self):
        route = configured_route(self.config, self.identity)
        for entry in route['entries']:
            backend = MTBackend.from_snapshot(entry['backend'])
            self.assertEqual(backend.provider, 'magpie')
            self.assertEqual(backend.base_url, 'http://127.0.0.1:3425/v1')
            self.assertEqual(backend.credential(self.config), 'magpie-transcribe')
        self.assertEqual(saved_route({**self.identity, 'translation_strategy_version': 6,
                                      'mt_route_snapshot': route}), route)


    def test_remote_gateway_requires_its_own_credential(self):
        self.config.settings.mt_base_url = 'https://gateway.example/v1'
        with self.assertRaises(ToolError):
            configured_route(self.config, self.identity)
        with mock.patch.dict(os.environ, {'MAGPIE_API_KEY': 'synthetic-gateway'}):
            route = configured_route(self.config, self.identity)
            self.assertEqual(route['entries'][0]['backend']['key_fingerprint'], key_fingerprint('synthetic-gateway'))

    def test_gateway_key_change_blocks_resume_identity(self):
        route = configured_route(self.config, self.identity)
        backend = MTBackend.from_snapshot(route['entries'][0]['backend'])
        with mock.patch.dict(os.environ, {'MAGPIE_API_KEY': 'different-gateway'}), self.assertRaises(ToolError) as error:
            backend.credential(self.config)
        self.assertEqual(error.exception.code, 'mt_account_changed')





    def test_local_registry_cannot_validate_another_gateway(self):
        from subtitle_tool.magpie import validate_group
        registry = self.path / 'gateway'
        registry.mkdir(exist_ok=True)
        self.config.settings.magpie_data_dir = str(registry)
        atomic_json(registry / 'providers.json', {'providers': [
            {'id': 'free', 'chat': 'https://openrouter.ai/api/v1'}],
            'groups': [{'id': 'free', 'members': ['free/openrouter/free']}]})
        for base in ('https://another.example/v1', 'http://127.0.0.1:9999/v1', 'http://127.0.0.1:3425/other/v1'):
            with self.subTest(base=base):
                validate_group(self.config.settings, 'group/free', base_url=base)
        atomic_json(registry / 'settings.json', {'port': 9999})
        validate_group(self.config.settings, 'group/free', base_url='http://127.0.0.1:9999/v1')

    def test_expired_imported_promotion_is_paused_in_magpie_and_other_free_members_remain(self):
        from subtitle_tool.magpie import validate_group
        folder = self.path / 'portable'
        registry = folder / 'data'
        registry.mkdir(parents=True)
        (folder / '.portable').touch()
        (folder / 'magpie.com').touch()
        self.config.settings.magpie_data_dir = str(registry)
        raw = {'providers': [
            {'id': 'transcribe-qiniu-free', 'chat': 'https://api.qnaigc.com/v1', 'models': ['qiniu/conatus-free']},
            {'id': 'router', 'chat': 'https://openrouter.ai/api/v1'}],
            'groups': [{'id': 'free', 'members': ['transcribe-qiniu-free/qiniu/conatus-free', 'router/openrouter/free']}]}
        atomic_json(registry / 'providers.json', raw)
        def pause(command, **kwargs):
            self.assertEqual(command[1:], ['provider', 'off', 'transcribe-qiniu-free'])
            raw['providers'][0]['off'] = True
            atomic_json(registry / 'providers.json', raw)
            return mock.Mock(returncode=0)
        with (mock.patch('subtitle_tool.mt_backend.qiniu_free_active', return_value=False),
              mock.patch('subtitle_tool.magpie.subprocess.run', side_effect=pause) as command):
            validate_group(self.config.settings, 'group/free')
            validate_group(self.config.settings, 'group/free')
        self.assertEqual(command.call_count, 1)
        self.assertNotIn('off', raw['providers'][1])

    def test_beijing_activity_boundaries_and_invalid_naive_time(self):
        def utc(month, day, hour, minute=0):
            return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)
        self.assertFalse(qiniu_free_active(utc(10, 23, 15, 59)))
        self.assertTrue(qiniu_free_active(utc(10, 23, 16)))
        self.assertTrue(qiniu_free_active(utc(11, 30, 15, 59)))
        self.assertFalse(qiniu_free_active(utc(11, 30, 16)))
        with self.assertRaises(ValueError):
            qiniu_free_active(datetime(2026, 10, 24))

    def test_urls_preserve_prefix_and_normalize_host_port_and_slashes(self):
        for value, expected in [("https://EXAMPLE.test:443/vendor/api/v1///", "https://example.test/vendor/api/v1"),
                                ("http://localhost:8080/v1/", "http://localhost:8080/v1"),
                                ("http://127.0.0.2/v1", "http://127.0.0.2/v1"),
                                ("http://[::1]:8000/v1/", "http://[::1]:8000/v1")]:
            with self.subTest(value=value):
                self.assertEqual(normalize_mt_url(value), expected)
        for value in ["", None, "http://example.test/v1", "ftp://localhost/v1", "https://user:password@example.test/v1",
                      "https://example.test/v1?token=secret", "https://example.test/v1?", "https://example.test/v1#",
                      "https://example.test/v1/chat/completions/", "https://example.test/v1/completions",
                      "https://example.test/v1/chat/%63ompletions", "https://example.test:bad/v1", "https://[broken",
                      "https://example.test/a/../v1", "https://example.test/a/%2e%2e/v1", "https://example.test\\@other.test/v1",
                      "https://example.test/v1\n"]:
            with self.subTest(value=value), self.assertRaises(ToolError) as error:
                normalize_mt_url(value)
            self.assertNotIn("password", str(error.exception))
            self.assertNotIn("secret", str(error.exception))


from tests.mt_fixtures import PoolPipelineFixture


class MagpiePipelineTests(PoolPipelineFixture):
    def setUp(self):
        super().setUp()
        registry = self.path / 'gateway'
        registry.mkdir(exist_ok=True)
        asr_key = self.config.get_key()
        self.config.settings.magpie_data_dir = str(registry)
        atomic_json(registry / 'providers.json', {'providers': [
            {'id': 'free', 'chat': 'https://openrouter.ai/api/v1', 'countFree': True},
            {'id': 'paid', 'chat': 'https://api.qnaigc.com/v1'}],
            'groups': [{'id': 'transcribe-free', 'members': ['free/provider/draft:free']},
                       {'id': 'transcribe', 'members': ['group/transcribe-free', 'paid/paid-model']},
                       {'id': 'transcribe-paid', 'members': ['paid/paid-model']},
                       {'id': 'transcribe-review', 'members': ['paid/review-model']},
                       {'id': 'transcribe-review-paid', 'members': ['paid/review-model']},
                       {'id': 'transcribe-judge', 'members': ['paid/judge-model']},
                       {'id': 'transcribe-judge-paid', 'members': ['paid/judge-model']}]})
        set_asr_account(self.config, asr_key)

    def assert_remote_route_ignores_local_groups(self, *, restored):
        remote = {'MT_BASE_URL': 'https://remote.example/v1', 'MT_MODEL': 'group/remote-draft',
                  'MT_FALLBACK_MODEL': 'group/remote-draft-paid', 'MT_REVIEW_MODEL': 'group/remote-review',
                  'MT_REVIEW_FALLBACK_MODEL': 'group/remote-review-paid', 'MT_JUDGE_MODEL': 'group/remote-judge',
                  'MT_JUDGE_FALLBACK_MODEL': 'group/remote-judge-paid'}
        with mock.patch.dict(os.environ, {'MAGPIE_API_KEY': 'synthetic-remote-key'}):
            with mock.patch.dict(os.environ, remote):
                self.install()
                if not restored:
                    self.assertEqual(self.run_pool(), 'completed')
            if restored:
                self.assertEqual(self.run_pool(), 'completed')
        self.assertTrue(self.calls)
        self.assertEqual({url for _, url, _ in self.calls}, {'https://remote.example/v1/chat/completions'})

    def test_environment_remote_route_is_not_validated_against_local_registry(self):
        self.assert_remote_route_ignores_local_groups(restored=False)

    def test_saved_remote_route_survives_return_to_local_defaults(self):
        self.assert_remote_route_ignores_local_groups(restored=True)

    def test_saved_local_route_still_checks_roles_when_current_default_is_remote(self):
        from pathlib import Path
        from subtitle_tool.engine import Engine
        from subtitle_tool.mt_pipeline import Pipeline
        self.install()
        path = Path(self.config.settings.magpie_data_dir) / 'providers.json'
        raw = json.loads(path.read_bytes())
        next(g for g in raw['groups'] if g['id'] == 'transcribe-review')['members'] = ['paid/paid-model']
        atomic_json(path, raw)
        self.config.settings.mt_base_url = 'https://remote.example/v1'
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope)
        self.addCleanup(pipeline.close)
        with self.assertRaises(ToolError) as caught:
            pipeline._validate_gateway(pipeline.router.entries[0])
        self.assertEqual(caught.exception.code, 'magpie_group')

    def test_all_stages_use_gateway_and_resume_keeps_saved_group(self):
        from subtitle_tool.main import summary
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual({url for _, url, _ in self.calls}, {'http://127.0.0.1:3425/v1/chat/completions'})
        self.assertEqual({data['_model'] for _, _, data in self.calls}, {'group/transcribe', 'group/transcribe-review'})
        before = self.store.job(self.job_id)['manifest']['mt_route_snapshot']
        self.config.settings.mt_model = 'group/new'
        self.assertEqual(saved_route(self.store.job(self.job_id)['manifest']), before)

        report = summary(self.store, self.job_id)
        self.assertEqual(report['models']['mt'], 'group/transcribe')
        self.assertEqual(report['gateway_session_prefix'], 'transcribe-' + self.job_id + '-')
        self.assertEqual(report['mt_policy_version'], 1)


    def test_cancel_saves_inflight_work_and_resume_uses_it(self):
        import threading
        from subtitle_tool.engine import Engine
        self.install()
        cancel = threading.Event()
        self.cancel_after = 'translate'
        self.assertEqual(self.run_pool(cancel=cancel), 'cancelled')
        self.calls.clear()
        self.cancel_after = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertNotIn('translate', [stage for stage, _, _ in self.calls])

    def test_gateway_requests_member_model_for_saved_provenance(self):
        from subtitle_tool.mt_pipeline import check_pool
        from subtitle_tool.mt_transport import HTTPReply
        import threading
        identity = {'region': 'beijing', 'workspace_id': self.config.settings.workspace_id,
                    'access_mode': 'workspace', 'account': key_fingerprint(self.config.get_key())}
        route = configured_route(self.config, identity)
        calls = []
        def request(method, url, **kwargs):
            calls.append(kwargs['headers'])
            self.assertGreater(kwargs['timeout'], 60)
            body = {'model': 'free/provider/draft:free', 'choices': [{'finish_reason': 'stop',
                    'message': {'content': '{"translations":{"0":"缓存保存先前计算的结果。"}}'}}]}
            return HTTPReply(200, json.dumps(body).encode())
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport') as transport:
            transport.return_value.request.side_effect = request
            report = check_pool(self.config, identity, route, threading.Event())
        self.assertEqual(calls[0].get('X-Magpie-Response-Model'), 'member')
        self.assertRegex(calls[0].get('X-Magpie-Session', ''), r'^transcribe-[0-9a-f]{32}-[0-9a-f]{32}$')
        self.assertEqual(report['gateway_session'], calls[0]['X-Magpie-Session'])
        self.assertEqual(report['actual_model'], 'free/provider/draft:free')
