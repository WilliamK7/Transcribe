"""Real SQLite, local processes and safe fake service results; no cloud calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from unittest import mock

from subtitle_tool.common import atomic_bytes
from subtitle_tool.engine import Engine
from subtitle_tool.main import summary
from subtitle_tool.mt_backend import MTBackend, key_fingerprint
from subtitle_tool.mt_diagnostics import MTDiagnostics
from subtitle_tool.mt_transport import HTTPReply, ProcessTransport
from tests.fixtures import SavedResultFixture, result
from tests.test_mt_runtime import ChildProcesses, quota
from tests.test_translation_v3 import batch_input, batch_response


class RuntimeEngineTests(SavedResultFixture):
    def setUp(self):
        super().setUp()
        self.config.settings.mt_provider = 'openai_compatible'
        self.config.settings.mt_base_url = 'https://openrouter.ai/api/v1'
        self.config.settings.mt_model = 'openrouter/free'
        self.config.settings.mt_rpm = 20
        self.config.settings.mt_workers = 4
        atomic_bytes(self.config.data / 'mt-key.txt', b'offline-mt-key')
        self.config.save()

    def install(self, raw=None, *, request_version=2):
        self.save_result(raw)
        manifest = self.store.job(self.job_id)['manifest']
        Engine(self.config, self.store).identity(self.job_id, manifest)
        manifest['mt_snapshot'] = MTBackend('openai_compatible', 'https://openrouter.ai/api/v1',
            'openrouter/free', key_fingerprint('offline-mt-key'), request_version).snapshot()
        if request_version == 3:
            from subtitle_tool.policy import current_execution_snapshot
            manifest.update(translation_strategy_version=4, execution_snapshot=current_execution_snapshot(4))
        self.store.update(self.job_id, manifest=manifest)

    @staticmethod
    def translated(kwargs):
        items = batch_input(kwargs['json'])
        if len(kwargs['json']['messages']) == 2:
            from tests.test_translation_v4 import mapped_response
            return HTTPReply(200, mapped_response(['完成译文'] * len(items)).content)
        return HTTPReply(200, batch_response(['完成译文'] * len(items)).content)

    def test_cli_sigint_ends_within_six_seconds_and_next_process_gets_lock(self):
        self.install()
        run = self.run_cancel_cli('once')
        self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
        report = json.loads(run.stdout)
        self.assertEqual(report['exit_code'], 130)
        self.assertGreaterEqual(report['drain_seconds'], 4.9)
        self.assertLess(report['drain_seconds'], 6)
        self.assertEqual(report['active_children'], 0)
        self.assertTrue(report['lock_reacquired'])
        self.assertTrue(report['billing_hint'])
        if os.name == 'nt':
            self.assertEqual(report['signal_kind'], 'windows_console_ctrl_c')
            self.assertEqual(report['console_signals_delivered'], [True])
        self.assertEqual(self.store.job(self.job_id)['state'], 'cancelled')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
        self.assertFalse(any(row['translated'] for row in self.store.references(self.job_id)))
        # The runner releases the late response before it exits; no late write may appear.
        snapshot = self.store.job(self.job_id), self.store.references(self.job_id), self.store.counts(self.job_id)
        time.sleep(.15)
        self.assertEqual(snapshot, (self.store.job(self.job_id), self.store.references(self.job_id), self.store.counts(self.job_id)))

    def test_second_cli_sigint_skips_remaining_drain(self):
        self.install()
        run = self.run_cancel_cli('twice')
        self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
        report = json.loads(run.stdout)
        self.assertEqual(report['exit_code'], 130)
        self.assertLess(report['drain_seconds'], 1.5)
        self.assertEqual(report['active_children'], 0)
        self.assertTrue(report['lock_reacquired'])
        if os.name == 'nt':
            self.assertEqual(report['console_signals_delivered'], [True, True])

    def test_structured_primary_and_bailian_use_real_bounded_cli_cancellation(self):
        for mode, role in [('once', 'primary'), ('bailian_once', 'fallback')]:
            with self.subTest(role=role):
                self.install(request_version=3)
                run = self.run_cancel_cli(mode)
                self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
                report = json.loads(run.stdout)
                self.assertEqual(report['exit_code'], 130)
                self.assertGreaterEqual(report['drain_seconds'], 4.9)
                self.assertLess(report['drain_seconds'], 6)
                self.assertEqual(report['active_children'], 0)
                self.assertTrue(report['lock_reacquired'])
                self.assertEqual(report['backend_calls'][role], 1)
                self.assertEqual(self.store.job(self.job_id)['state'], 'cancelled')
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
    def run_cancel_cli(self, mode):
        options = {}
        if os.name == 'nt':
            # Deliver real console Ctrl+C events in a separate hidden console, never to the test runner.
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            options = {'creationflags': subprocess.CREATE_NEW_CONSOLE, 'startupinfo': startup}
            mode = 'console_' + mode
        return subprocess.run([sys.executable, '-m', 'tests.mt_cancel_runner', str(self.path), self.job_id, mode],
                              capture_output=True, timeout=15, close_fds=True, **options)

    def test_valid_drain_result_is_saved_once_and_runtime_does_not_count_as_invalid(self):
        self.install()
        cancel = threading.Event()
        writes = []
        original = self.store.translation_batch_update
        def save(*args, **kwargs):
            writes.append(1)
            return original(*args, **kwargs)
        transport = mock.Mock()
        def request(method, *args, **kwargs):
            if method == 'GET':
                return quota()
            cancel.set()
            return self.translated(kwargs)
        transport.request.side_effect = request
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', return_value=transport), \
                mock.patch.object(self.store, 'translation_batch_update', side_effect=save):
            self.assertEqual(Engine(self.config, self.store, cancel).run(self.job_id), 'cancelled')
        self.assertEqual(writes, [1])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 1)
        self.assertTrue(all(row['translated'] for row in self.store.references(self.job_id)))
        counts = self.store.counts(self.job_id)
        self.assertEqual(counts.get('mt_invalid_response', 0), 0)
        self.assertEqual(counts.get('mt_split', 0), 0)

    def test_quota_stop_is_failed_and_distinct_from_user_cancellation(self):
        self.install()
        for reply, expected in [(quota(0), 'mt_quota_exhausted'), (HTTPReply(200, b'{"data":{}}'), 'mt_quota_unknown')]:
            transport = mock.Mock()
            transport.request.return_value = reply
            with mock.patch.dict('subtitle_tool.mt_runtime._QUOTAS', clear=True), \
                    mock.patch('subtitle_tool.mt_runtime.ProcessTransport', return_value=transport):
                self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'failed')
            report = summary(self.store, self.job_id)
            self.assertEqual(report['error_code'], expected)
            self.assertEqual(report['state'], 'failed')
            self.assertEqual(transport.request.call_count, 1)
            self.assertEqual(report['counts'].get('mt', 0), 0)

    def test_cache_only_recovery_and_republication_never_construct_network_transport(self):
        self.install()
        # Populate the current contract; reflow from an old contract intentionally misses old caches.
        Engine(self.config, self.store).reflow(self.job_id)
        transport = mock.Mock()
        transport.request.side_effect = lambda method, *args, **kwargs: quota() if method == 'GET' else self.translated(kwargs)
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', return_value=transport):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'completed')
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', side_effect=AssertionError('Cache-only recovery called network')):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'completed')
            self.store.update(self.job_id, 'writing')
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'completed')

    def test_fatal_error_stops_other_real_network_children_with_bounded_drain(self):
        from tests.test_mt_runtime import LocalServer
        self.install(result(('First sentence.', 'en'), ('Second sentence.', 'en'), ('Third sentence.', 'en')))
        server = LocalServer()
        self.addCleanup(server.close)
        first_failed = threading.Event()
        class MixedTransport(ProcessTransport):
            def request(child, method, url, **kwargs):
                if method == 'GET':
                    return quota()
                text = batch_input(kwargs['json'])[0]['text']
                if text.startswith('First'):
                    self.assertTrue(server.entered.wait(5))
                    first_failed.set()
                    return HTTPReply(403, b'{"error":{"message":"private"}}')
                return super(MixedTransport, child).request(method, server.url + '/hang', **kwargs)
        started = time.monotonic()
        with ChildProcesses() as children, mock.patch('subtitle_tool.mt_runtime.ProcessTransport', MixedTransport):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'failed')
        self.assertTrue(first_failed.is_set())
        self.assertLess(time.monotonic() - started, 6.5)
        self.assertEqual(children.active_count, 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)

    def test_runtime_diagnostics_only_record_locally_allowlisted_fields(self):
        writer = MTDiagnostics(self.path, self.job_id)
        writer.write_runtime({'kind': 'runtime_request', 'category': 'private input', 'attempt': 2,
            'status': 503, 'duration_ms': 123, 'retryable': True, 'message': 'private input',
            'raw': 'private input', 'request_id': 'private input', 'deadline_terminated': False})
        writer.write_runtime({'kind': 'runtime_quota', 'category': 'quota_observed', 'used': 3,
            'limit': 20, 'remaining': 17, 'observed_at': '2026-10-22T00:00:00+00:00', 'account': 'private input'})
        raw = writer.path.read_text(encoding='utf-8')
        self.assertNotIn('private input', raw)
        records = [json.loads(line) for line in raw.splitlines()]
        self.assertNotIn('category', records[0])
        self.assertEqual(records[1]['remaining'], 17)
        self.assertEqual(records[1]['observed_at'], '2026-10-22T00:00:00+00:00')

    def _four_workers_at_free_balance(self, available):
        self.install(result(('First sentence.', 'en'), ('Second sentence.', 'en'),
                            ('Third sentence.', 'en'), ('Fourth sentence.', 'en')))
        transport, sent = mock.Mock(), []
        def request(method, *args, **kwargs):
            if method == 'GET':
                return quota(available)
            sent.append(1)
            time.sleep(.04)
            return self.translated(kwargs)
        transport.request.side_effect = request
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', return_value=transport):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_quota_exhausted')
        self.assertLessEqual(len(sent), available)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], len(sent))

    def test_four_workers_stop_at_one_free_request_without_caching_failures(self):
        self._four_workers_at_free_balance(1)

    def test_four_workers_stop_at_two_free_requests_without_caching_failures(self):
        self._four_workers_at_free_balance(2)

    def test_injected_late_valid_result_is_rejected_before_any_cache_write(self):
        self.install()
        engine = Engine(self.config, self.store)
        engine.mt_control.drain_seconds = .05
        transport = mock.Mock()
        def request(method, *args, **kwargs):
            if method == 'GET':
                return quota()
            engine.request_cancel()
            time.sleep(.1)
            return self.translated(kwargs)
        transport.request.side_effect = request
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', return_value=transport):
            self.assertEqual(engine.run(self.job_id), 'cancelled')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
        self.assertFalse(any(row['translated'] for row in self.store.references(self.job_id)))
        self.assertEqual(self.store.counts(self.job_id)['mt_drain_discarded'], 1)

    def test_local_failure_stops_workers_without_changing_it_to_user_cancellation(self):
        self.install()
        engine = Engine(self.config, self.store)
        transport = mock.Mock()
        def request(method, *args, **kwargs):
            if method == 'GET':
                return quota()
            raise OSError('private input')
        transport.request.side_effect = request
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', return_value=transport):
            self.assertEqual(engine.run(self.job_id), 'failed')
        self.assertFalse(engine.cancel.is_set())
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'local_io')
        self.assertNotIn('private input', self.store.job(self.job_id)['error'])
