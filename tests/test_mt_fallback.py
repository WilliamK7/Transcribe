"""Group fallback regression coverage with real SQLite and offline HTTP responses."""
from __future__ import annotations

import json
import time
from pathlib import Path
import threading
from unittest import mock

from subtitle_tool.common import ToolError, atomic_bytes
from subtitle_tool.engine import Engine
from subtitle_tool.mt_backend import MTBackend, key_fingerprint
from subtitle_tool.mt_runtime import FreeQuota, RecoveryCoordinator
from subtitle_tool.mt_transport import HTTPReply
from tests.fixtures import SavedResultFixture, result
from tests.test_mt_runtime import Clock, business_failure, quota
from tests.test_translation_v3 import batch_input, batch_response


class FallbackTests(SavedResultFixture):
    def setUp(self):
        super().setUp()
        self.config.settings.mt_provider = 'openai_compatible'
        self.config.settings.mt_base_url = 'https://openrouter.ai/api/v1'
        self.config.settings.mt_model = 'openrouter/free'
        self.config.settings.mt_workers = 1
        atomic_bytes(self.config.data / 'mt-key.txt', b'offline-fallback-mt-key')
        self.clock = Clock()
        self.resources = {}
        def resources(backend):
            identity = (backend.provider, backend.base_url, backend.model)
            if identity not in self.resources:
                self.resources[identity] = (RecoveryCoordinator(clock=self.clock, jitter=lambda: 0, wait=self.clock.wait),
                    FreeQuota(clock=self.clock, utcnow=lambda: self.clock.day, wait=self.clock.wait))
            return self.resources[identity]
        patch = mock.patch('subtitle_tool.mt_runtime.runtime_resources', side_effect=resources)
        patch.start()
        self.addCleanup(patch.stop)
        # Runtime pacing uses the same virtual clock; the real subprocess tests cover wall-clock limits.
        from subtitle_tool.api import RequestPacer
        pacers = {}
        def pacer(backend, **kwargs):
            return pacers.setdefault((backend.provider, backend.model),
                RequestPacer(clock=self.clock, requests_per_window=0))
        patch = mock.patch('subtitle_tool.mt_runtime.mt_pacer', side_effect=pacer)
        patch.start()
        self.addCleanup(patch.stop)
        self.calls = []

    def install(self, raw=None, *, enabled=True):
        self.save_result(raw or result(('First sentence.', 'en'), ('Second sentence.', 'en')))
        manifest = self.store.job(self.job_id)['manifest']
        Engine(self.config, self.store).identity(self.job_id, manifest)
        manifest['mt_snapshot'] = MTBackend('openai_compatible', 'https://openrouter.ai/api/v1', 'openrouter/free',
                                          key_fingerprint('offline-fallback-mt-key'), 2).snapshot()
        self.store.update(self.job_id, manifest=manifest)
        if enabled:
            Engine(self.config, self.store).resume(self.job_id, mt_fallback='bailian')

    def run_backend(self, handler, *, cancel=None):
        transport = mock.Mock()
        def request(method, url, **kwargs):
            if method == 'GET':
                return handler('quota', None, kwargs)
            provider = 'primary' if url.startswith('https://openrouter.ai/') else 'fallback'
            self.calls.append((provider, kwargs['json']))
            return handler(provider, batch_input(kwargs['json']), kwargs)
        transport.request.side_effect = request
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', return_value=transport):
            return Engine(self.config, self.store, cancel).run(self.job_id)

    @staticmethod
    def success(items):
        return HTTPReply(200, batch_response(['完整译文'] * len(items)).content)

    def test_four_failures_fall_back_only_this_group_and_next_group_prefers_primary(self):
        from subtitle_tool.main import summary

        self.install()
        def service(provider, items, kwargs):
            if provider == 'quota':
                return quota()
            if provider == 'primary' and items[0]['text'].startswith('First'):
                return HTTPReply(503, b'{}')
            return self.success(items)
        self.assertEqual(self.run_backend(service), 'completed')
        self.assertEqual([p for p, _ in self.calls], ['primary'] * 4 + ['fallback', 'primary'])
        self.assertEqual([row['mt_origin'] for row in self.store.references(self.job_id)], ['fallback', 'primary'])
        self.assertEqual([row['mt_origin'] for row in self.store.cues(self.job_id)], ['fallback', 'primary'])
        backup_body = next(body for p, body in self.calls if p == 'fallback')
        self.assertEqual(set(backup_body), {'model', 'messages', 'stream', 'temperature', 'seed'})
        self.assertEqual((backup_body['model'], backup_body['temperature'], backup_body['seed']), ('qwen-mt-flash', 0, 0))
        self.assertFalse(backup_body['stream'])
        self.assertEqual(summary(self.store, self.job_id)['mt_fallback'],
                         {'enabled': True, 'provider': 'bailian', 'model': 'qwen-mt-flash'})

    def test_fourth_primary_attempt_success_does_not_invoke_backup(self):
        self.install()
        attempts = 0
        def service(provider, items, kwargs):
            nonlocal attempts
            if provider == 'quota':
                return quota()
            attempts += 1
            return HTTPReply(503, b'{}') if attempts < 4 else self.success(items)
        self.assertEqual(self.run_backend(service), 'completed')
        self.assertFalse(any(p == 'fallback' for p, _ in self.calls))

    def check_not_found_recovery(self, status):
        self.install()
        attempts = 0
        def service(provider, items, kwargs):
            nonlocal attempts
            if provider == 'quota':
                return quota()
            if provider == 'primary' and items[0]['text'].startswith('First'):
                attempts += 1
                if attempts == 1:
                    return business_failure(status=status, choice=status == 404)
            return self.success(items)
        self.assertEqual(self.run_backend(service), 'completed')
        self.assertEqual([p for p, _ in self.calls], ['primary'] * 3)
        self.assertEqual(self.store.counts(self.job_id).get('mt_fallback_switch', 0), 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_invalid_response', 0), 0)

    def test_http_200_not_found_recovers_without_fallback(self):
        self.check_not_found_recovery(200)

    def test_http_404_not_found_recovers_without_fallback(self):
        self.check_not_found_recovery(404)

    def test_not_found_exhaustion_saves_fallback_origin_and_cache_then_next_group_uses_primary(self):
        self.install()
        attempts = 0
        def service(provider, items, kwargs):
            nonlocal attempts
            if provider == 'quota':
                return quota()
            if provider == 'primary' and items[0]['text'].startswith('First'):
                attempts += 1
                return business_failure(choice=attempts % 2 == 0)
            return self.success(items)
        with mock.patch.object(self.store, 'cache_get', wraps=self.store.cache_get) as cache, \
                mock.patch.object(self.store, 'translation_batch_update', wraps=self.store.translation_batch_update) as save:
            self.assertEqual(self.run_backend(service), 'completed')
        self.assertEqual([p for p, _ in self.calls], ['primary'] * 4 + ['fallback', 'primary'])
        self.assertTrue(all(body == self.calls[0][1] for _, body in self.calls[:4]))
        self.assertEqual([r['mt_origin'] for r in self.store.references(self.job_id)], ['fallback', 'primary'])
        self.assertEqual([r['mt_origin'] for r in self.store.cues(self.job_id)], ['fallback', 'primary'])
        keys = {r[0] for r in self.store.db.execute('SELECT key FROM mt_cache')}
        fallback_keys = [c.kwargs['cache_key'] for c in save.call_args_list if c.kwargs['origin'] == 'fallback']
        self.assertEqual(len(fallback_keys), 1)
        self.assertIn(fallback_keys[0], keys)
        self.assertNotIn(cache.call_args_list[0].args[0], keys)
        counts = self.store.counts(self.job_id)
        self.assertEqual((counts['mt_primary_request'], counts['mt_fallback_request'], counts['mt_fallback_switch']), (5, 1, 1))
        self.assertEqual(counts.get('mt_invalid_response', 0) + counts.get('mt_split', 0), 0)
        path = self.config.root / '.build' / 'mt-diagnostics' / (self.job_id + '.jsonl')
        events = [json.loads(row) for row in path.read_text(encoding='utf-8').splitlines()]
        retries = [e for e in events if e['kind'] == 'runtime_request' and e.get('category') == 'not_found']
        self.assertEqual([e['attempt'] for e in retries], [1, 2, 3, 4])
        self.assertTrue(all(e['retryable'] for e in retries))
        switches = [e for e in events if e['kind'] == 'runtime_fallback']
        self.assertEqual([(e['reason'], e['attempt']) for e in switches], [('transient_exhausted', 4)])
        self.assertNotIn('private provider details', path.read_text(encoding='utf-8'))

    def test_cancel_during_not_found_retry_does_not_start_fallback(self):
        self.install()
        cancel = threading.Event()
        def service(provider, *args):
            if provider == 'quota':
                return quota()
            cancel.set()
            return business_failure()
        self.assertEqual(self.run_backend(service, cancel=cancel), 'cancelled')
        self.assertEqual([p for p, _ in self.calls], ['primary'])

    def test_disabled_fallback_stops_after_four_not_found_attempts(self):
        self.install()
        Engine(self.config, self.store).resume(self.job_id, mt_fallback='none')
        self.assertEqual(self.run_backend(lambda p, *args: quota() if p == 'quota' else business_failure()), 'failed')
        self.assertEqual([p for p, _ in self.calls], ['primary'] * 4)
        self.assertEqual(self.store.counts(self.job_id).get('mt_fallback_switch', 0), 0)

    def test_other_group_can_commit_while_not_found_group_retries(self):
        self.install()
        self.config.settings.mt_workers = 2
        committed = threading.Event()
        admitted = threading.Barrier(2)
        attempts = 0
        original_save = self.store.translation_batch_update
        def save(*args, **kwargs):
            original_save(*args, **kwargs)
            if kwargs.get('origin') == 'primary':
                committed.set()
        def service(provider, items, kwargs):
            nonlocal attempts
            if provider == 'quota':
                return quota()
            if provider == 'primary' and items[0]['text'].startswith('First'):
                attempts += 1
                if attempts == 1:
                    admitted.wait(3)
                if attempts == 2:
                    self.assertTrue(committed.wait(3), 'Other primary group did not commit during retry')
                return business_failure()
            if provider == 'primary':
                admitted.wait(3)
            return self.success(items)
        with mock.patch.object(self.store, 'translation_batch_update', side_effect=save):
            self.assertEqual(self.run_backend(service), 'completed')
        self.assertTrue(committed.is_set())
        self.assertEqual(attempts, 4)
        self.assertEqual(sum(p == 'fallback' for p, _ in self.calls), 1)
        self.assertEqual([r['mt_origin'] for r in self.store.references(self.job_id)], ['fallback', 'primary'])

    def test_resume_retains_saved_translation_and_only_retries_unfinished_group(self):
        self.install()
        def fail_second(provider, items, kwargs):
            if provider == 'quota':
                return quota()
            if items[0]['text'].startswith('Second'):
                return business_failure('permission_denied')
            return self.success(items)
        self.assertEqual(self.run_backend(fail_second), 'failed')
        saved = self.store.references(self.job_id)[0]
        self.assertTrue(saved['translated'])
        Engine(self.config, self.store).resume(self.job_id)
        self.resources.clear()
        self.calls.clear()
        self.assertEqual(self.run_backend(lambda p, items, kw: quota() if p == 'quota' else
            business_failure() if p == 'primary' else self.success(items)), 'completed')
        self.assertEqual(self.store.references(self.job_id)[0], saved)
        self.assertEqual([p for p, _ in self.calls], ['primary'] * 4 + ['fallback'])
        self.assertTrue(all(batch_input(body)[0]['text'].startswith('Second') for _, body in self.calls))

    def test_trusted_quota_exhaustion_routes_remaining_work_to_backup(self):
        self.install()
        def service(provider, items, kwargs):
            if provider == 'quota':
                return quota(1)
            return self.success(items)
        self.assertEqual(self.run_backend(service), 'completed')
        self.assertEqual([p for p, _ in self.calls], ['primary', 'fallback'])

    def test_first_quota_unknown_stops_without_fallback(self):
        self.install()
        self.assertEqual(self.run_backend(lambda *args: HTTPReply(200, b'{"data":{}}')), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_quota_unknown')
        self.assertEqual(self.calls, [])

    def test_disabled_fallback_content_filter_stops_without_repair_or_backup(self):
        self.install()
        Engine(self.config, self.store).resume(self.job_id, mt_fallback='none')
        denied = HTTPReply(200, b'{"choices":[{"finish_reason":"content_filter","message":{"content":null}}]}')
        self.assertEqual(self.run_backend(lambda p, *args: quota() if p == 'quota' else denied), 'failed')
        self.assertEqual([p for p, _ in self.calls], ['primary'])
        self.assertEqual(self.store.counts(self.job_id).get('mt_invalid_response', 0), 0)

    def test_message_refusal_falls_back_once_and_next_group_keeps_primary_for_v2(self):
        self.install()
        denied = HTTPReply(200, b'{"choices":[{"finish_reason":"stop","message":{"content":null,"refusal":"synthetic refusal"}}]}')
        def service(provider, items, kwargs):
            if provider == 'quota':
                return quota()
            if provider == 'primary' and items[0]['text'].startswith('First'):
                return denied
            return self.success(items)
        self.assertEqual(self.run_backend(service), 'completed')
        self.assertEqual([provider for provider, _ in self.calls], ['primary', 'fallback', 'primary'])
        self.assertEqual([row['mt_origin'] for row in self.store.references(self.job_id)], ['fallback', 'primary'])
        self.assertEqual([row['mt_origin'] for row in self.store.cues(self.job_id)], ['fallback', 'primary'])
        path = self.config.root / '.build' / 'mt-diagnostics' / (self.job_id + '.jsonl')
        raw = path.read_text(encoding='utf-8')
        events = [json.loads(line) for line in raw.splitlines()]
        switches = [event for event in events if event['kind'] == 'runtime_fallback']
        self.assertEqual([(event['reason'], event['attempt']) for event in switches], [('content_refused', 1)])
        self.assertEqual(self.store.counts(self.job_id).get('mt_invalid_response', 0), 0)
        self.assertNotIn('synthetic refusal', raw)

    def test_content_refusal_group_eligibility_covers_code_and_categories_only(self):
        from subtitle_tool.mt_control import MTControl
        from subtitle_tool.mt_fallback import GroupFallback
        from subtitle_tool.mt_runtime import ServiceError
        for error in [ToolError('safe refusal', 'mt_content_refused'),
                      ServiceError('content_policy_violation'), ServiceError('refusal', 403)]:
            with self.subTest(code=error.code, category=getattr(error, 'category', None)):
                router = GroupFallback(MTControl(threading.Event()))
                backup, switched = mock.Mock(return_value='completed'), mock.Mock()
                def denied():
                    raise error
                self.assertEqual(router.run(denied, backup, switched), 'completed')
                backup.assert_called_once_with()
                switched.assert_called_once_with(error)
                self.assertFalse(getattr(error, 'retryable', False))
                self.assertFalse(router.exhausted.is_set())
                self.assertIsNone(router.control.error)
        for category in ['http_permanent', 'authentication', 'permission_denied', 'payment_required',
                         'invalid_request', 'service_unknown']:
            with self.subTest(category=category):
                router = GroupFallback(MTControl(threading.Event()))
                error = ServiceError(category, 403)
                backup, switched = mock.Mock(), mock.Mock()
                def denied():
                    raise error
                with self.assertRaises(ToolError) as caught:
                    router.run(denied, backup, switched)
                self.assertIs(caught.exception, error)
                backup.assert_not_called()
                switched.assert_not_called()

    def test_unclassified_http_403_stops_without_backup(self):
        self.install()
        self.assertEqual(self.run_backend(lambda provider, *args:
            quota() if provider == 'quota' else HTTPReply(403, b'{}')), 'failed')
        self.assertEqual([provider for provider, _ in self.calls], ['primary'])
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_http_403')

    def test_format_failure_falls_back_with_the_whole_original_group(self):
        self.install()
        def service(provider, items, kwargs):
            if provider == 'quota':
                return quota()
            if provider == 'primary' and items[0]['text'].startswith('First'):
                return HTTPReply(200, batch_response([]).content)
            return self.success(items)
        self.assertEqual(self.run_backend(service), 'completed')
        self.assertEqual([p for p, _ in self.calls], ['primary', 'primary', 'fallback', 'primary'])

    def test_backup_failure_is_bounded_and_reports_both_safe_attempt_counts(self):
        self.install()
        def service(provider, items, kwargs):
            return quota() if provider == 'quota' else HTTPReply(503, b'{"error":{"code":"InternalError","message":"private"}}')
        self.assertEqual(self.run_backend(service), 'failed')
        self.assertEqual([p for p, _ in self.calls], ['primary'] * 4 + ['fallback'] * 4)
        error = self.store.job(self.job_id)['error']
        self.assertIn('OpenRouter', error)
        self.assertIn('百炼', error)
        self.assertIn('4/4', error)
        self.assertNotIn('private', error)

    def test_format_failure_reports_cumulative_attempts_without_a_per_body_denominator(self):
        from subtitle_tool.mt_control import MTControl
        from subtitle_tool.mt_fallback import GroupFallback
        router = GroupFallback(MTControl(threading.Event()))
        def fail(attempts):
            error = ToolError('格式恢复耗尽', 'mt_batch_invalid')
            error.attempts = attempts
            raise error
        with self.assertRaises(ToolError) as raised:
            router.run(lambda: fail(5), lambda: fail(6), lambda error: None)
        self.assertIn('累计 5 次 HTTP 尝试', str(raised.exception))
        self.assertIn('累计 6 次 HTTP 尝试', str(raised.exception))
        self.assertNotIn('/4', str(raised.exception))

    def test_cancel_after_primary_failure_does_not_start_backup(self):
        self.install()
        cancel = threading.Event()
        def service(provider, items, kwargs):
            if provider == 'quota':
                return quota()
            cancel.set()
            return HTTPReply(503, b'{}')
        self.assertEqual(self.run_backend(service, cancel=cancel), 'cancelled')
        self.assertEqual([p for p, _ in self.calls], ['primary'])

    def test_enable_upgrade_is_transactional_idempotent_and_marks_existing_success(self):
        self.install(enabled=False)
        self.assertEqual(self.run_backend(lambda p, items, kw: quota() if p == 'quota' else self.success(items)), 'completed')
        before = self.store.job(self.job_id)['manifest']['mt_snapshot']
        engine = Engine(self.config, self.store)
        engine.resume(self.job_id)
        policy = self.store.job(self.job_id)['manifest']['mt_fallback_policy']
        self.assertTrue(policy['enabled'])
        snapshot = self.config.root / policy['migration_snapshot']
        self.assertTrue(snapshot.is_file())
        self.assertNotIn('mt_origin', json.loads(snapshot.read_text(encoding='utf-8'))['references'][0])
        self.assertTrue(all(r['mt_origin'] == 'primary' for r in self.store.references(self.job_id)))
        engine.resume(self.job_id)
        self.assertEqual(self.store.job(self.job_id)['manifest']['mt_fallback_policy'], policy)
        self.assertEqual(self.store.job(self.job_id)['manifest']['mt_snapshot'], before)
        engine.resume(self.job_id, mt_fallback='none')
        self.assertFalse(self.store.job(self.job_id)['manifest']['mt_fallback_policy']['enabled'])

    def test_long_retry_after_switches_without_clearing_primary_cooldown(self):
        self.install()
        def service(provider, items, kwargs):
            if provider == 'quota':
                return quota()
            return HTTPReply(429, b'{}', '301') if provider == 'primary' else self.success(items)
        self.assertEqual(self.run_backend(service), 'completed')
        self.assertEqual([p for p, _ in self.calls], ['primary', 'fallback', 'fallback'])
        primary = self.resources[('openai_compatible', 'https://openrouter.ai/api/v1', 'openrouter/free')][0]
        self.assertGreater(primary.cooldown_until, self.clock.now)

    def test_permanent_primary_error_does_not_switch(self):
        self.install()
        denied = HTTPReply(401, b'{"error":{"metadata":{"error_type":"authentication"}}}')
        self.assertEqual(self.run_backend(lambda p, *args: quota() if p == 'quota' else denied), 'failed')
        self.assertEqual([p for p, _ in self.calls], ['primary'])

    def test_backup_authentication_or_arrearage_stops_after_one_attempt(self):
        self.install()
        def service(provider, items, kwargs):
            if provider == 'quota':
                return quota(0)
            return HTTPReply(503, b'{"error":{"code":"Arrearage","message":"private"}}')
        self.assertEqual(self.run_backend(service), 'failed')
        self.assertEqual([p for p, _ in self.calls], ['fallback'])
        self.assertIn('payment_required', self.store.job(self.job_id)['error'])

    def test_disabling_preserves_mixed_origins_and_republication(self):
        self.install()
        self.assertEqual(self.run_backend(lambda p, items, kw: quota(1) if p == 'quota' else self.success(items)), 'completed')
        before = self.store.references(self.job_id), self.store.cues(self.job_id)
        Engine(self.config, self.store).resume(self.job_id, mt_fallback='none')
        self.assertEqual((self.store.references(self.job_id), self.store.cues(self.job_id)), before)
        self.store.update(self.job_id, 'writing')
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', side_effect=AssertionError('Publication must stay local')):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'completed')

    def test_upgrade_rollback_does_not_leave_policy_or_partial_origins(self):
        self.install(enabled=False)
        self.assertEqual(self.run_backend(lambda p, items, kw: quota() if p == 'quota' else self.success(items)), 'completed')
        before = self.store.job(self.job_id), self.store.cues(self.job_id), self.store.references(self.job_id)
        self.store.db.execute("CREATE TRIGGER reject_fallback BEFORE UPDATE OF manifest ON jobs "
                              "WHEN json_extract(NEW.manifest,'$.mt_fallback_policy.enabled')=1 "
                              "BEGIN SELECT RAISE(ABORT,'test rejection'); END")
        self.store.db.commit()
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual((self.store.job(self.job_id), self.store.cues(self.job_id), self.store.references(self.job_id)), before)

    def test_result_and_origin_and_fallback_cache_rollback_together(self):
        self.install()
        self.store.db.execute("CREATE TRIGGER reject_fallback_success BEFORE INSERT ON counters "
                              "WHEN NEW.name='mt_fallback_success' BEGIN SELECT RAISE(ABORT,'test rejection'); END")
        self.store.db.commit()
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_backend(lambda p, items, kw: quota(0) if p == 'quota' else self.success(items))
        self.assertFalse(any(row['translated'] or row.get('mt_origin') for row in self.store.references(self.job_id)))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)

        self.store.db.execute('DROP TRIGGER reject_fallback_success')
        self.store.db.commit()
        self.resources.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_backend(lambda p, items, kw: quota(0) if p == 'quota' else self.success(items)), 'completed')

    def test_mismatched_key_rejects_enable_before_changing_output_or_records(self):
        self.install(enabled=False)
        before = self.store.job(self.job_id), self.store.cues(self.job_id)
        atomic_bytes(self.config.data / 'mt-key.txt', b'another-mt-key')
        with self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).resume(self.job_id, output_dir=self.path / 'other')
        self.assertEqual(error.exception.code, 'mt_account_changed')
        self.assertEqual((self.store.job(self.job_id), self.store.cues(self.job_id)), before)

    def test_origin_is_validated_against_saved_backends(self):
        self.install()
        self.assertEqual(self.run_backend(lambda p, items, kw: quota(0) if p == 'quota' else self.success(items)), 'completed')
        cues = self.store.cues(self.job_id)
        cues[0]['mt_origin'] = 'unknown'
        self.store.replace_cues(self.job_id, cues)
        self.store.update(self.job_id, 'writing')
        self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')

    def test_review_invalidates_translation_and_origin_together(self):
        from subtitle_tool.review import ReviewDecision
        self.install()
        self.assertEqual(self.run_backend(lambda p, items, kw: quota(0) if p == 'quota' else self.success(items)), 'completed')
        self.store.update(self.job_id, 'needs_review')
        cue = self.store.cues(self.job_id)[0]
        self.store.apply_review(self.job_id, cue['cue_id'], ReviewDecision('translate', 'English'))
        self.assertFalse(any(row['translated'] or row.get('mt_origin') for row in self.store.cues(self.job_id)))
        self.assertEqual(self.store.references(self.job_id), [])

    def test_http_200_does_not_count_as_validated_backend_success(self):
        self.install()
        self.assertEqual(self.run_backend(lambda p, *args: quota() if p == 'quota' else HTTPReply(200,
            b'{"error":{"metadata":{"error_type":"permission_denied"}}}')), 'failed')
        counts = self.store.counts(self.job_id)
        self.assertEqual(counts['mt_primary_http_2xx'], 1)
        self.assertEqual(counts.get('mt_primary_success', 0), 0)
        self.assertEqual(counts.get('mt_success', 0), 1)

    def test_disabled_fallback_still_counts_primary_http_responses(self):
        self.install()
        Engine(self.config, self.store).resume(self.job_id, mt_fallback='none')
        self.assertEqual(self.run_backend(lambda p, *args: quota() if p == 'quota' else HTTPReply(200,
            b'{"error":{"metadata":{"error_type":"permission_denied"}}}')), 'failed')
        counts = self.store.counts(self.job_id)
        self.assertEqual(counts.get('mt_primary_http_2xx', 0), 1)
        self.assertEqual(counts.get('mt_primary_success', 0), 0)

    def test_fallback_diagnostics_have_run_group_backend_without_private_content(self):
        self.install()
        self.assertEqual(self.run_backend(lambda p, items, kw: quota(0) if p == 'quota' else self.success(items)), 'completed')
        path = self.config.root / '.build' / 'mt-diagnostics' / (self.job_id + '.jsonl')
        raw = path.read_text(encoding='utf-8')
        events = [json.loads(line) for line in raw.splitlines()]
        switches = [e for e in events if e['kind'] == 'runtime_fallback']
        self.assertTrue(switches)
        self.assertTrue(all(len(e['run_id']) == 32 and len(e['group_key']) == 64 and e['backend'] == 'fallback' for e in switches))
        self.assertNotIn('First sentence', raw)
        self.assertNotIn('完整译文', raw)
        self.assertNotIn('offline-fallback-mt-key', raw)

    def test_new_split_ranges_clear_obsolete_origin(self):
        self.install()
        self.assertEqual(self.run_backend(lambda p, items, kw: quota(0) if p == 'quota' else self.success(items)), 'completed')
        revised = [{**cue, 'translated': ''} for cue in self.store.cues(self.job_id)]
        self.store.save_layout(self.job_id, revised, self.store.job(self.job_id)['manifest'])
        self.assertFalse(any(row.get('mt_origin') for row in self.store.cues(self.job_id)))

    def test_backup_cache_has_its_own_keys_and_is_not_read_until_fallback(self):
        self.install()
        self.assertEqual(self.run_backend(lambda p, items, kw: quota(0) if p == 'quota' else self.success(items)), 'completed')
        fallback_keys = {row[0] for row in self.store.db.execute('SELECT key FROM mt_cache')}
        self.assertTrue(fallback_keys)
        self.reset_for_legacy_cache_recovery()
        self.resources.clear()
        self.calls.clear()
        get = self.store.cache_get
        with mock.patch.object(self.store, 'cache_get', wraps=get) as lookup:
            self.assertEqual(self.run_backend(lambda p, items, kw: quota() if p == 'quota' else self.success(items)), 'completed')
        self.assertFalse(fallback_keys.intersection(call.args[0] for call in lookup.call_args_list))
        self.assertFalse(any(p == 'fallback' for p, _ in self.calls))

    def test_primary_failure_reuses_existing_backup_cache_without_http(self):
        self.install()
        self.assertEqual(self.run_backend(lambda p, items, kw: quota(0) if p == 'quota' else self.success(items)), 'completed')
        self.reset_for_legacy_cache_recovery()
        self.resources.clear()
        self.calls.clear()
        self.assertEqual(self.run_backend(lambda p, items, kw: quota(0) if p == 'quota' else self.success(items)), 'completed')
        self.assertEqual(self.calls, [])
        self.assertTrue(self.store.counts(self.job_id)['mt_fallback_cache_hit'])

    def reset_for_legacy_cache_recovery(self):
        # An old resume keeps its contract. Explicit reflow now upgrades to a distinct cache identity.
        manifest = self.store.job(self.job_id)['manifest']
        manifest['overwrite'] = True
        self.store.save_layout(self.job_id, [{**c, 'translated': ''} for c in self.store.cues(self.job_id)], manifest,
                               references=[{**r, 'translated': ''} for r in self.store.references(self.job_id)])
        self.store.update(self.job_id, 'queued')

    def test_disabling_fallback_restores_stop_on_primary_exhaustion(self):
        self.install()
        Engine(self.config, self.store).resume(self.job_id, mt_fallback='none')
        self.assertEqual(self.run_backend(lambda p, *args: quota() if p == 'quota' else HTTPReply(503, b'{}')), 'failed')
        self.assertEqual([p for p, _ in self.calls], ['primary'] * 4)

    def test_explicit_none_on_an_old_task_saves_the_disabled_policy(self):
        self.install(enabled=False)
        Engine(self.config, self.store).resume(self.job_id, mt_fallback='none')
        policy = self.store.job(self.job_id)['manifest'].get('mt_fallback_policy')
        self.assertIsNotNone(policy)
        self.assertFalse(policy['enabled'])

    def test_backup_runs_during_primary_cooldown(self):
        self.install()
        times = []
        def service(provider, items, kwargs):
            if provider == 'quota':
                return quota()
            if provider == 'primary' and items[0]['text'].startswith('First'):
                return HTTPReply(503, b'{}')
            if provider == 'fallback':
                primary = self.resources[('openai_compatible', 'https://openrouter.ai/api/v1', 'openrouter/free')][0]
                times.append((self.clock.now, primary.cooldown_until))
            return self.success(items)
        self.assertEqual(self.run_backend(service), 'completed')
        self.assertTrue(times)
        self.assertTrue(all(now < cooldown for now, cooldown in times))

    def test_shared_pool_concurrency_does_not_double_on_quota_cutover(self):
        self.install(result(*[(f'Sentence number {i}.', 'en') for i in range(8)]))
        self.config.settings.mt_workers = 4
        barrier = threading.Barrier(4)
        lock, active, maximum, count, queries = threading.Lock(), 0, 0, 0, 0
        def service(provider, items, kwargs):
            nonlocal active, maximum, count, queries
            if provider == 'quota':
                with lock:
                    queries += 1
                return quota(0)
            with lock:
                active += 1
                maximum = max(maximum, active)
                count += 1
                initial = count <= 4
            try:
                if initial:
                    barrier.wait(4)
                time.sleep(.02)
                return self.success(items)
            finally:
                with lock:
                    active -= 1
        self.assertEqual(self.run_backend(service), 'completed')
        self.assertEqual(maximum, 4)
        self.assertEqual(queries, 1)
        self.assertEqual([p for p, _ in self.calls], ['fallback'] * 8)

    def test_resume_clears_only_quota_observation_not_recovery_state(self):
        from subtitle_tool import mt_runtime
        backend = MTBackend('openai_compatible', 'https://openrouter.ai/api/v1', 'openrouter/free', 'a' * 64, 2)
        account = (backend.base_url, backend.key_fingerprint)
        identity = (backend.provider, backend.base_url, backend.key_fingerprint, backend.model)
        recovery, ledger = RecoveryCoordinator(), FreeQuota()
        recovery.cooldown_until = 99999
        mt_runtime._QUOTAS[account] = ledger
        mt_runtime._RECOVERY[identity] = recovery
        mt_runtime.reset_quota_for_resume(backend)
        self.assertNotIn(account, mt_runtime._QUOTAS)
        self.assertIs(mt_runtime._RECOVERY[identity], recovery)
        self.assertEqual(recovery.cooldown_until, 99999)

    def test_cli_new_task_defaults_to_enabled_and_none_is_saved(self):
        from subtitle_tool import main as cli
        import contextlib
        import io
        for choice in [None, 'none']:
            source = self.path / ('new-default.mkv' if choice is None else 'new-disabled.mkv')
            source.write_bytes(b'CLI preflight fixture')
            args = [str(source), '--non-interactive'] + (['--mt-fallback', choice] if choice else [])
            def run(engine, job_id):
                engine.store.update(job_id, 'failed', code='mocked_run')
                return 'failed'
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(cli, 'Config', return_value=self.config), mock.patch.object(Engine, 'run', new=run), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(cli.main(args), 2)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report['mt_fallback'],
                             {'enabled': choice is None, 'provider': 'bailian', 'model': 'qwen3.8-flash'})
            if choice is None:
                self.assertIn('百炼后备（qwen3.8-flash）', stderr.getvalue())
            else:
                self.assertNotIn('已启用按组百炼后备', stderr.getvalue())
            matches = [j for j in self.store.jobs() if Path(j['source']).name == source.name]
            self.assertTrue(matches)
            job = matches[0]
            self.assertEqual(job['manifest']['mt_fallback_policy']['enabled'], choice is None)

    def test_private_upgrade_snapshot_cannot_be_overwritten_by_a_report(self):
        from subtitle_tool.reports import ReportWriter
        target = self.path / '.build' / 'mt-fallback' / self.job_id / 'future.json'
        with self.assertRaises(ToolError) as error:
            ReportWriter(self.config, target).validate()
        self.assertEqual(error.exception.code, 'report_conflict')

    def test_bailian_processes_use_the_same_bounded_ctrl_c_drain(self):
        from tests.test_mt_runtime_engine import RuntimeEngineTests
        self.install(result(('Only this sentence.', 'en')))
        self.config.save()
        for mode in ['bailian_once', 'bailian_twice']:
            run = RuntimeEngineTests.run_cancel_cli(self, mode)
            self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
            report = json.loads(run.stdout)
            self.assertEqual(report['exit_code'], 130)
            self.assertEqual(report['backend_calls'], {'primary': 0, 'fallback': 1})
            self.assertEqual(report['active_children'], 0)
            self.assertTrue(report['lock_reacquired'])
            self.assertLess(report['drain_seconds'], 6 if mode.endswith('once') else 1.5)
            self.assertEqual(self.store.job(self.job_id)['state'], 'cancelled')
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)

    def test_native_errors_are_classified_without_recording_native_codes(self):
        from subtitle_tool.mt_runtime import bailian_failure
        for code, category, retryable in [('InvalidApiKey', 'authentication', False),
                ('AccessDenied', 'permission_denied', False), ('Arrearage', 'payment_required', False),
                ('DataInspectionFailed', 'content_policy_violation', False),
                ('InternalError.Algo.InvalidParameter', 'invalid_request', False),
                ('LimitRequests', 'rate_limit_exceeded', True), ('RequestTimeOut', 'timeout', True),
                ('InternalError.TranslationFailed', 'server', True), ('unknown-private', 'service_unknown', False)]:
            with self.subTest(code=code):
                error = bailian_failure(200, {'error': {'code': code, 'message': 'private input'}})
                self.assertEqual((error.category, error.retryable), (category, retryable))
                self.assertNotIn('private input', str(error))
                self.assertNotIn(code, error.code)

    def test_bailian_content_filter_does_not_enter_format_repair(self):
        self.install()
        self.assertEqual(self.run_backend(lambda p, *args: quota(0) if p == 'quota' else HTTPReply(200,
            b'{"choices":[{"finish_reason":"content_filter","message":{"content":null}}]}')), 'failed')
        self.assertEqual([p for p, _ in self.calls], ['fallback'])
        self.assertEqual(self.store.counts(self.job_id).get('mt_invalid_response', 0), 0)

    def test_partial_recursive_primary_result_is_discarded_and_backup_receives_original_group(self):
        from subtitle_tool.api import OpenAICompatible, BoundedBailianMT
        from subtitle_tool.mt_control import MTControl
        from subtitle_tool.mt_fallback import GroupFallback, saved_fallback
        from subtitle_tool.mt_backend import saved_mt
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        router = GroupFallback(MTControl(threading.Event()))
        backend = saved_mt(manifest)
        primary = OpenAICompatible('offline-fallback-mt-key', backend, router.primary_control.cancel,
                                    control=router.primary_control)
        primary.http.stop_on_failure = False
        backup = BoundedBailianMT(self.config.get_key(), saved_fallback(manifest), router.control)
        calls = []
        def request(method, url, **kwargs):
            if method == 'GET':
                return quota()
            items = batch_input(kwargs['json'])
            origin = 'primary' if url.startswith(backend.base_url) else 'fallback'
            calls.append((origin, [i['text'] for i in items]))
            if origin == 'primary':
                return self.success(items) if len(items) == 1 and items[0]['text'] == 'alpha' else HTTPReply(200, batch_response([]).content)
            return self.success(items)
        transport = mock.Mock()
        transport.request.side_effect = request
        primary.http.transport = backup.http.transport = transport
        translated = router.run(lambda: primary.translate_batch(['alpha', 'bravo']),
                                lambda: backup.translate_batch(['alpha', 'bravo']), lambda error: None)
        self.assertEqual(translated.translations, {'0': '完整译文', '1': '完整译文'})
        self.assertEqual([len(items) for p, items in calls if p == 'primary'], [2, 2, 1, 1, 1])
        self.assertEqual([items for p, items in calls if p == 'fallback'], [['alpha', 'bravo']])

    def test_complete_backup_result_during_cancel_is_committed_once(self):
        self.install()
        cancel = threading.Event()
        def service(provider, items, kwargs):
            if provider == 'quota':
                return quota(0)
            cancel.set()
            return self.success(items)
        with mock.patch.object(self.store, 'translation_batch_update', wraps=self.store.translation_batch_update) as save:
            self.assertEqual(self.run_backend(service, cancel=cancel), 'cancelled')
        self.assertEqual(save.call_count, 1)
        self.assertEqual(self.store.references(self.job_id)[0]['mt_origin'], 'fallback')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 1)
