"""Offline regression tests for OpenRouter's bounded request lifecycle."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from subtitle_tool.common import Cancelled, ToolError
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_transport import HTTPReply, ProcessTransport, TransportError
from subtitle_tool.mt_runtime import FreeQuota, BoundedMTHttp, RecoveryCoordinator
from subtitle_tool.mt_runtime import runtime_resources, service_failure
from subtitle_tool.api import RequestPacer
from subtitle_tool.mt_backend import MTBackend, key_fingerprint
from tests.fixtures import TemporaryTest


def quota(remaining=20):
    return HTTPReply(200, json.dumps({'data': {'private': 'never record', 'free_model_daily_requests': {
        'used': 20 - remaining, 'limit': 20, 'remaining': remaining}}}).encode())


def business_failure(kind='not_found', *, status=200, choice=False):
    error = {'message': 'private provider details', 'metadata': {'error_type': kind}}
    body = {'choices': [{'finish_reason': 'error', 'error': error}]} if choice else {'error': error}
    return HTTPReply(status, json.dumps(body).encode())


def reserve(ledger, control, fetch):
    while True:
        ledger.ensure(control, fetch)
        if ledger.try_reserve(control):
            return


class ChildProcesses:
    """Observe actual child handles without adding bookkeeping to the transport."""
    def __enter__(self):
        self.processes = []
        self.start = subprocess.Popen
        self.patch = mock.patch('subtitle_tool.mt_transport.subprocess.Popen', side_effect=self.spawn)
        self.patch.start()
        return self

    def spawn(self, *args, **kwargs):
        process = self.start(*args, **kwargs)
        self.processes.append(process)
        return process

    def __exit__(self, *exc):
        self.patch.stop()

    @property
    def active_count(self):
        return sum(process.poll() is None for process in self.processes)

    @property
    def last_process(self):
        return self.processes[-1]


class Clock:
    def __init__(self):
        self.now = 100.0
        self.day = datetime(2026, 10, 22, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def wait(self, control, seconds):
        control.check_start()
        self.now += seconds


class LocalServer:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.disconnected = threading.Event()
        self.body = b'{"ok":true}'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                owner.entered.set()
                if self.path == '/hang':
                    owner.release.wait(10)
                try:
                    self.send_response(200)
                    self.end_headers()
                    if self.path == '/slow':
                        for _ in range(200):
                            self.wfile.write(b' ')
                            self.wfile.flush()
                            if owner.release.wait(.03):
                                break
                    elif self.path == '/large':
                        self.wfile.write(b'x' * (4 * 1024 * 1024 + 1))
                    else:
                        self.wfile.write(owner.body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    owner.disconnected.set()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class ProcessTransportTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.server = LocalServer()
        self.addCleanup(self.server.close)
        self.control = MTControl(threading.Event())
        self.transport = ProcessTransport()
        self.children = self.enterContext(ChildProcesses())

    def send(self, path, **kwargs):
        return self.transport.request('POST', self.server.url + path, headers={'Authorization': 'Bearer private'},
                                      json={'stream': False}, control=self.control, **kwargs)

    def test_silent_and_dribbling_responses_have_a_total_deadline_and_are_reaped(self):
        for path in ['/hang', '/slow']:
            with self.subTest(path=path):
                started = time.monotonic()
                with self.assertRaises(TransportError) as caught:
                    self.send(path, timeout=1.0)
                self.assertEqual(caught.exception.category, 'deadline')
                self.assertLess(time.monotonic() - started, 2.5)
                self.assertEqual(self.children.active_count, 0)
                self.assertIsNotNone(self.children.last_process.returncode)
                self.assertTrue(all(pipe.closed for pipe in [self.children.last_process.stdin,
                                                             self.children.last_process.stdout]))

    def test_response_cap_is_a_static_error(self):
        with self.assertRaises(TransportError) as caught:
            self.send('/large', timeout=5)
        self.assertEqual(caught.exception.category, 'response_too_large')
        self.assertEqual(self.children.active_count, 0)
        self.assertNotIn('xxxx', str(caught.exception))

    def test_private_pipes_hidden_process_and_no_key_in_arguments(self):
        with mock.patch('subtitle_tool.mt_transport.subprocess.Popen', wraps=subprocess.Popen) as start:
            self.assertEqual(self.send('/ok', timeout=5).body, b'{"ok":true}')
        args, kwargs = start.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertNotIn('private', str(args))
        self.assertIs(kwargs['shell'], False)
        self.assertIs(kwargs['close_fds'], True)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        if os.name == 'nt':
            self.assertTrue(kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)

    def test_cancel_drains_a_valid_inflight_result_but_force_terminates(self):
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.send, '/hang', timeout=10)
            self.assertTrue(self.server.entered.wait(5))
            self.control.cancel_user()
            self.server.release.set()
            self.assertEqual(future.result(3).status, 200)
        self.server.release.clear()
        self.server.entered.clear()
        self.control = MTControl(threading.Event())
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.send, '/hang', timeout=10)
            self.assertTrue(self.server.entered.wait(5))
            started = time.monotonic()
            self.control.cancel_user(force=True)
            with self.assertRaises(Cancelled):
                future.result(2)
            self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.children.active_count, 0)

    def test_child_crash_is_safe_and_reaped(self):
        with mock.patch('subtitle_tool.mt_transport.subprocess.Popen',
                        side_effect=lambda *args, **kwargs: self.children.spawn(
                            [sys.executable, '-c', 'raise SystemExit(7)'], **kwargs)):
            with self.assertRaises(TransportError) as caught:
                self.send('/ok', timeout=5)
        self.assertEqual(caught.exception.category, 'transport')
        self.assertEqual(self.children.active_count, 0)

    def test_all_inflight_requests_share_one_five_second_drain_window(self):
        with ThreadPoolExecutor(3) as pool:
            futures = [pool.submit(self.send, '/hang', timeout=30) for _ in range(3)]
            until = time.monotonic() + 5
            while self.children.active_count < 3 and time.monotonic() < until:
                time.sleep(.01)
            self.assertEqual(self.children.active_count, 3)
            started = time.monotonic()
            self.control.cancel_user()
            for future in futures:
                with self.assertRaises(Cancelled):
                    future.result(6)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 4.9)
            self.assertLess(elapsed, 6)
        self.assertEqual(self.children.active_count, 0)

    def test_quota_queries_do_not_use_the_translation_drain_window(self):
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.send, '/hang', timeout=10, drain=False)
            self.assertTrue(self.server.entered.wait(5))
            started = time.monotonic()
            self.control.cancel_user()
            with self.assertRaises(Cancelled):
                future.result(2)
            self.assertLess(time.monotonic() - started, 1)

    def test_network_child_does_not_inherit_the_task_lock(self):
        from subtitle_tool.instance import ProcessLock
        lock = ProcessLock(self.path)
        self.assertTrue(lock.acquire())
        try:
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(self.send, '/hang', timeout=10)
                try:
                    self.assertTrue(self.server.entered.wait(5))
                    lock.close()
                    probe = subprocess.run([sys.executable, '-c',
                        'import sys; from pathlib import Path; from subtitle_tool.instance import ProcessLock; '
                        'lock=ProcessLock(Path(sys.argv[1])); ok=lock.acquire(); lock.close(); sys.exit(0 if ok else 1)',
                        str(self.path)], capture_output=True, timeout=5, close_fds=True)
                    self.assertEqual(probe.returncode, 0)
                finally:
                    self.control.cancel_user(force=True)
                with self.assertRaises(Cancelled):
                    future.result(2)
        finally:
            lock.close()


class RecoveryTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.control = MTControl(threading.Event(), clock=self.clock)
        self.pacer = RequestPacer(clock=self.clock, requests_per_window=0)
        self.recovery = RecoveryCoordinator(clock=self.clock, jitter=lambda: 0, wait=self.clock.wait)

    def test_same_round_failures_merge_and_old_success_does_not_unlock_probe(self):
        old = self.recovery.acquire(self.control, self.pacer)
        peer = self.recovery.acquire(self.control, self.pacer)
        self.recovery.failure(old)
        deadline = self.recovery.cooldown_until
        self.recovery.failure(peer)
        self.assertEqual(self.recovery.cooldown_until, deadline)
        self.assertEqual(deadline, 115)
        self.recovery.success(peer)
        self.assertTrue(self.recovery.recovering)
        probe = self.recovery.acquire(self.control, self.pacer)
        self.assertTrue(probe.probe)
        self.recovery.failure(probe)
        self.assertEqual(self.recovery.cooldown_until, 145)
        probe = self.recovery.acquire(self.control, self.pacer)
        self.recovery.success(probe)
        self.assertFalse(self.recovery.recovering)

    def test_shared_wait_uses_latest_pacer_deadline(self):
        ticket = self.recovery.acquire(self.control, self.pacer)
        self.recovery.failure(ticket, retry_after=40)
        self.pacer.throttle(50, ticket.pacer_generation)
        self.recovery.acquire(self.control, self.pacer)
        self.assertAlmostEqual(self.clock.now, 150)

    def test_only_one_translation_probe_is_released_before_success(self):
        recovery = RecoveryCoordinator(jitter=lambda: 0)
        pacer = RequestPacer(requests_per_window=0)
        control = MTControl(threading.Event())
        ticket = recovery.acquire(control, pacer)
        recovery.failure(ticket)
        recovery.cooldown_until = time.monotonic()
        entered = []
        lock, probe_entered, release = threading.Lock(), threading.Event(), threading.Event()
        def acquire():
            item = recovery.acquire(control, pacer)
            with lock:
                entered.append(item)
            if item.probe:
                probe_entered.set()
                release.wait(3)
                recovery.success(item)
            return item
        with ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(acquire) for _ in range(4)]
            try:
                self.assertTrue(probe_entered.wait(2))
                time.sleep(.15)
                self.assertEqual(len(entered), 1)
            finally:
                release.set()
            tickets = [future.result(3) for future in futures]
        self.assertEqual(sum(item.probe for item in tickets), 1)

    def test_cancel_stops_shared_and_pacing_waits_without_a_request(self):
        old = self.recovery.acquire(self.control, self.pacer)
        self.recovery.failure(old)
        self.control.cancel_user()
        with self.assertRaises(Cancelled):
            self.recovery.acquire(self.control, self.pacer)

    def test_late_retry_after_cannot_be_cleared_by_an_already_started_probe(self):
        first = self.recovery.acquire(self.control, self.pacer)
        peer = self.recovery.acquire(self.control, self.pacer)
        self.recovery.failure(first)
        probe = self.recovery.acquire(self.control, self.pacer)
        self.recovery.failure(peer, retry_after=80)
        self.recovery.success(probe)
        self.assertTrue(self.recovery.recovering)
        later_probe = self.recovery.acquire(self.control, self.pacer)
        self.assertTrue(later_probe.probe)
        self.assertGreaterEqual(self.clock.now, 195)

    def test_long_retry_after_stops_other_clients_without_waiting_or_sending(self):
        first = self.recovery.acquire(self.control, self.pacer)
        self.recovery.failure(first, retry_after=301)
        with self.assertRaises(ToolError) as error:
            self.recovery.acquire(MTControl(threading.Event(), clock=self.clock), self.pacer)
        self.assertEqual(error.exception.code, 'mt_retry_after')
        self.assertEqual(self.clock.now, 100)

    def test_late_peer_retry_after_starts_a_new_probe_gate_after_recovery(self):
        first = self.recovery.acquire(self.control, self.pacer)
        late_peer = self.recovery.acquire(self.control, self.pacer)
        self.recovery.failure(first)
        probe = self.recovery.acquire(self.control, self.pacer)
        self.recovery.success(probe)
        inflight_after_recovery = self.recovery.acquire(self.control, self.pacer)
        self.recovery.failure(late_peer, retry_after=40)
        self.recovery.success(inflight_after_recovery)
        self.assertTrue(self.recovery.recovering)
        self.assertTrue(self.recovery.acquire(self.control, self.pacer).probe)


class QuotaTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.control = MTControl(threading.Event(), clock=self.clock)
        self.ledger = FreeQuota(clock=self.clock, utcnow=lambda: self.clock.day, wait=self.clock.wait)

    def test_four_workers_cannot_oversend_one_or_two_remaining_units(self):
        for available in [1, 2]:
            ledger = FreeQuota()
            control = MTControl(threading.Event())
            queried = []
            def fetch():
                queried.append(1)
                time.sleep(.06)
                return quota(available)
            def admit():
                try:
                    reserve(ledger, control, fetch)
                    return True
                except ToolError as error:
                    self.assertEqual(error.code, 'mt_quota_exhausted')
                    return False
            with ThreadPoolExecutor(4) as pool:
                self.assertEqual(sum(pool.map(lambda _: admit(), range(4))), available)
            self.assertEqual(len(queried), 1)

    def test_refresh_only_lowers_remaining_and_failed_refresh_keeps_trusted_balance(self):
        reserve(self.ledger, self.control, lambda: quota(10))
        self.ledger.complete()
        self.clock.now += 31
        reserve(self.ledger, self.control, lambda: quota(15))
        self.ledger.complete()
        self.assertEqual(self.ledger.remaining, 8)
        self.clock.now += 31
        def unavailable():
            raise TransportError('network')
        reserve(self.ledger, self.control, unavailable)
        self.assertEqual(self.ledger.remaining, 7)

    def test_unknown_fields_failed_first_query_and_cross_utc_day_stop(self):
        for reply in [HTTPReply(200, b'{"data":{}}'), quota(True), HTTPReply(503, b'private')]:
            ledger = FreeQuota(clock=self.clock, utcnow=lambda: self.clock.day)
            with self.assertRaises(ToolError) as error:
                reserve(ledger, self.control, lambda: reply)
            self.assertEqual(error.exception.code, 'mt_quota_unknown')
        reserve(self.ledger, self.control, lambda: quota(10))
        self.clock.day = datetime(2026, 10, 23, tzinfo=timezone.utc)
        self.clock.now += 31
        with self.assertRaises(ToolError) as error:
            reserve(self.ledger, self.control, lambda: HTTPReply(503, b'private'))
        self.assertEqual(error.exception.code, 'mt_quota_unknown')

    def test_delayed_refresh_subtracts_existing_inflight_and_new_sends(self):
        reserve(self.ledger, self.control, lambda: quota(10))
        self.clock.now += 31
        entered, release = threading.Event(), threading.Event()
        def query():
            entered.set()
            self.assertTrue(release.wait(3))
            return quota(6)
        with ThreadPoolExecutor(1) as pool:
            refresh = pool.submit(self.ledger.ensure, self.control, query)
            self.assertTrue(entered.wait(2))
            try:
                reserve(self.ledger, self.control, lambda: self.fail('Concurrent query was not merged'))
                reserve(self.ledger, self.control, lambda: self.fail('Concurrent query was not merged'))
            finally:
                release.set()
            refresh.result(3)
        self.assertEqual(self.ledger.remaining, 3)  # remote 6 minus one old and two newly sent

    def test_cross_day_within_refresh_interval_waits_and_requires_a_new_observation(self):
        reserve(self.ledger, self.control, lambda: quota(5))
        self.ledger.complete()
        self.clock.day = datetime(2026, 10, 23, tzinfo=timezone.utc)
        queries = []
        def query():
            queries.append(self.clock.now)
            return quota(12)
        reserve(self.ledger, self.control, query)
        self.assertEqual(len(queries), 1)
        self.assertGreaterEqual(queries[0], 130)
        self.assertEqual(self.ledger.remaining, 11)

    def test_concurrent_models_share_account_quota_and_coalesce_the_query(self):
        from dataclasses import replace
        backend = MTBackend('openai_compatible', 'https://openrouter.ai/api/v1', 'openrouter/free', 'a' * 64, 2)
        _, ledger = runtime_resources(backend)
        _, other = runtime_resources(replace(backend, model='provider/model:free'))
        self.assertIs(ledger, other)
        controls = [MTControl(threading.Event()) for _ in range(4)]
        queries = []
        def query():
            queries.append(1)
            time.sleep(.08)
            return quota(10)
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda control: reserve(ledger, control, query), controls))
        self.assertEqual(queries, [1])
        self.assertEqual(ledger.remaining, 6)

    def test_failed_initial_query_does_not_fall_back_to_an_earlier_runs_observation(self):
        reserve(self.ledger, self.control, lambda: quota(10))
        self.ledger.complete()
        self.clock.now += 1
        new_run = MTControl(threading.Event(), clock=self.clock)
        with self.assertRaises(ToolError) as error:
            reserve(self.ledger, new_run, lambda: HTTPReply(503, b'{}'))
        self.assertEqual(error.exception.code, 'mt_quota_unknown')

    def test_missing_refresh_fields_keep_a_same_day_trusted_local_balance(self):
        reserve(self.ledger, self.control, lambda: quota(6))
        self.ledger.complete()
        self.clock.now += 30
        reserve(self.ledger, self.control, lambda: HTTPReply(200, b'{"data":{}}'))
        self.assertEqual(self.ledger.remaining, 4)


class RetryTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.control = MTControl(threading.Event(), clock=self.clock)
        self.backend = MTBackend('openai_compatible', 'https://openrouter.ai/api/v1', 'openrouter/free',
                                 key_fingerprint('offline'), 2)
        self.transport = mock.Mock()
        self.ledger = FreeQuota(clock=self.clock, utcnow=lambda: self.clock.day)
        self.client = BoundedMTHttp(self.backend, self.control, transport=self.transport,
            pacer=RequestPacer(clock=self.clock, requests_per_window=0),
            recovery=RecoveryCoordinator(clock=self.clock, jitter=lambda: 0, wait=self.clock.wait), quota=self.ledger)

    def send(self):
        return self.client.json('POST', self.backend.base_url + '/chat/completions', name='mt',
                                headers={'Authorization': 'Bearer offline'}, json={'stream': False})

    def replies(self, *responses):
        pending = iter(responses)
        def request(method, *args, **kwargs):
            if method == 'GET':
                return quota()
            value = next(pending)
            if isinstance(value, BaseException):
                raise value
            return value
        self.transport.request.side_effect = request

    def mt_calls(self):
        return [call for call in self.transport.request.call_args_list if call.args[0] == 'POST']

    def test_free_router_not_found_is_retryable_in_top_level_and_choice_errors(self):
        for status in [200, 201, 404, 408, 429, 500, 503, 599]:
            for choice in [False, True]:
                with self.subTest(status=status, choice=choice):
                    reply = business_failure(status=status, choice=choice)
                    error = service_failure(status, json.loads(reply.body), free_router=True)
                    self.assertEqual(error.category, 'not_found')
                    self.assertTrue(error.retryable)
                    self.assertNotIn('private provider details', str(error))
        for kinds in [('not_found', 'timeout'), ('server', 'not_found')]:
            body = {'error': {'metadata': {'error_type': kinds[0]}},
                    'choices': [{'error': {'metadata': {'error_type': kinds[1]}}}]}
            error = service_failure(503, body, free_router=True)
            self.assertEqual(error.category, 'not_found')
            self.assertTrue(error.retryable)

    def test_free_router_not_found_then_success_uses_normal_retry_budget(self):
        for status in [200, 404]:
            for choice in [False, True]:
                with self.subTest(status=status, choice=choice):
                    self.control.begin()
                    self.replies(business_failure(status=status, choice=choice), HTTPReply(200, b'{"ok":true}'))
                    before = len(self.mt_calls())
                    self.assertEqual(self.send(), {'ok': True})
                    self.assertEqual(len(self.mt_calls()) - before, 2)

    def test_not_found_timeout_and_http_errors_share_four_total_attempts(self):
        events = []
        self.client.on_event = events.append
        self.replies(business_failure(), business_failure('timeout', choice=True),
                     HTTPReply(503, b'{}'), business_failure(status=404))
        with self.assertRaises(ToolError) as caught:
            self.send()
        self.assertEqual((caught.exception.category, caught.exception.attempts), ('not_found', 4))
        self.assertEqual(len(self.mt_calls()), 4)
        self.assertEqual(self.ledger.remaining, 16)
        self.assertEqual([e['category'] for e in events if e['kind'] == 'runtime_request'],
                         ['not_found', 'timeout', 'http_transient', 'not_found'])

    def test_not_found_exception_requires_official_v2_free_router_identity(self):
        for model, base, version in [('paid/fixed', self.backend.base_url, 2),
                ('vendor/fixed:free', self.backend.base_url, 2),
                ('openrouter/free', 'https://other.example/v1', 2),
                ('openrouter/free', self.backend.base_url, 1)]:
            with self.subTest(model=model, base=base, version=version):
                backend = MTBackend('openai_compatible', base, model, self.backend.key_fingerprint, version)
                client = BoundedMTHttp(backend, MTControl(threading.Event(), clock=self.clock), transport=self.transport,
                    pacer=RequestPacer(clock=self.clock, requests_per_window=0),
                    recovery=RecoveryCoordinator(clock=self.clock, jitter=lambda: 0, wait=self.clock.wait))
                client.quota = None
                self.transport.request.reset_mock(side_effect=True)
                self.transport.request.return_value = business_failure()
                with self.assertRaises(ToolError) as caught:
                    client.json('POST', base + '/chat/completions', json={})
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.category, 'not_found')
                self.assertEqual(self.transport.request.call_count, 1)

    def test_other_permanent_types_and_nonretryable_http_statuses_override_router_not_found(self):
        for kind in ['authentication', 'permission_denied', 'payment_required', 'invalid_request',
                     'content_policy_violation', 'refusal', 'context_length_exceeded']:
            for status in [200, 404, 408, 429, 503]:
                for reverse in [False, True]:
                    kinds = [kind, 'not_found'] if reverse else ['not_found', kind]
                    body = {'error': {'metadata': {'error_type': kinds[0]}},
                            'choices': [{'error': {'metadata': {'error_type': kinds[1]}}}]}
                    with self.subTest(kind=kind, status=status, reverse=reverse):
                        error = service_failure(status, body, free_router=True)
                        self.assertEqual(error.category, kind)
                        self.assertFalse(error.retryable)
        for status in [301, 400, 401, 402, 403, 405, 410, 413, 415, 422]:
            with self.subTest(status=status):
                error = service_failure(status, json.loads(business_failure().body), free_router=True)
                self.assertFalse(error.retryable)

    def test_router_not_found_mixed_with_unknown_malformed_or_untyped_errors_stays_unknown(self):
        unknowns = [{'metadata': {'error_type': value}} for value in ['private-unknown', None, False, 42, [], {}]]
        unknowns.extend([{}, None, {'metadata': []}])
        known = {'metadata': {'error_type': 'not_found'}}
        for status in [200, 404, 408, 429, 503]:
            for unknown in unknowns:
                for reverse in [False, True]:
                    errors = [unknown, known] if reverse else [known, unknown]
                    body = {'error': errors[0], 'choices': [{'finish_reason': 'error', 'error': errors[1]}]}
                    with self.subTest(status=status, unknown=unknown, reverse=reverse):
                        error = service_failure(status, body, free_router=True)
                        self.assertEqual(error.category, 'service_unknown')
                        self.assertFalse(error.retryable)

    def test_content_filter_and_refusal_override_router_not_found(self):
        for refusal in [{'finish_reason': 'content_filter'}, {'message': {'refusal': 'private refusal'}}]:
            body = json.loads(business_failure().body)
            body['choices'] = [refusal]
            error = service_failure(200, body, free_router=True)
            self.assertEqual(error.category, 'content_policy_violation')
            self.assertFalse(error.retryable)

    def test_http_and_business_errors_share_four_attempts(self):
        busy = HTTPReply(200, b'{"choices":[{"error":{"metadata":{"error_type":"timeout"}}}]}')
        self.replies(HTTPReply(404, b'{}'), busy, HTTPReply(503, b'{}'), HTTPReply(200, b'{"ok":true}'))
        self.assertEqual(self.send(), {'ok': True})
        self.assertEqual(len(self.mt_calls()), 4)
        self.assertEqual(self.ledger.remaining, 16)

    def test_404_exhaustion_is_bounded_and_fixed_model_does_not_retry(self):
        self.replies(*[HTTPReply(404, b'{}')] * 4)
        with self.assertRaises(ToolError) as error:
            self.send()
        self.assertEqual(error.exception.code, 'mt_http_404')
        self.assertEqual(len(self.mt_calls()), 4)
        self.backend = MTBackend('openai_compatible', self.backend.base_url, 'paid/fixed', self.backend.key_fingerprint, 2)
        client = BoundedMTHttp(self.backend, MTControl(threading.Event()), transport=self.transport)
        self.transport.request.reset_mock(side_effect=True)
        self.transport.request.return_value = HTTPReply(404, b'{}')
        with self.assertRaises(ToolError):
            client.json('POST', 'offline', headers={}, json={})
        self.assertEqual(self.transport.request.call_count, 1)

    def test_permanent_typed_error_overrides_transient_http_status_without_format_repair(self):
        self.transport.request.side_effect = [quota(), HTTPReply(503, json.dumps({'error': {
            'message': 'private input', 'metadata': {'error_type': 'permission_denied'}}}).encode())]
        with self.assertRaises(ToolError) as error:
            self.send()
        self.assertEqual(self.transport.request.call_count, 2)
        self.assertNotIn('private', str(error.exception))
        self.assertFalse(error.exception.retryable)

    def test_long_retry_after_ends_round_without_an_early_retry(self):
        self.transport.request.side_effect = [quota(), HTTPReply(429, b'{}', '301')]
        with self.assertRaises(ToolError) as error:
            self.send()
        self.assertEqual(error.exception.code, 'mt_retry_after')
        self.assertEqual(self.transport.request.call_count, 2)
        self.assertEqual(self.clock.now, 100)

    def test_deadline_exhaustion_uses_dedicated_code(self):
        self.replies(*[TransportError('deadline')] * 4)
        with self.assertRaises(ToolError) as error:
            self.send()
        self.assertEqual(error.exception.code, 'mt_request_deadline')
        self.assertEqual(len(self.mt_calls()), 4)

    def test_404_then_success_and_2xx_top_level_business_error_then_success(self):
        for failure in [HTTPReply(404, b'{}'), HTTPReply(200, b'{"error":{"metadata":{"error_type":"server"}}}')]:
            self.control.begin()
            self.replies(failure, HTTPReply(200, b'{"ok":true}'))
            before = len(self.mt_calls())
            self.assertEqual(self.send(), {'ok': True})
            self.assertEqual(len(self.mt_calls()) - before, 2)

    def test_retry_counts_against_quota_and_does_not_send_after_it_is_exhausted(self):
        sent = []
        def request(method, *args, **kwargs):
            if method == 'GET':
                return quota(1)
            sent.append(1)
            return HTTPReply(503, b'{}')
        self.transport.request.side_effect = request
        with self.assertRaises(ToolError) as error:
            self.send()
        self.assertEqual(error.exception.code, 'mt_quota_exhausted')
        self.assertEqual(sent, [1])

    def test_429_uses_adaptive_pacing_and_prioritizes_due_quota_refresh(self):
        self.replies(HTTPReply(429, b'{}', '40'), HTTPReply(200, b'{"ok":true}'))
        self.assertEqual(self.send(), {'ok': True})
        self.assertGreaterEqual(self.clock.now, 140)
        self.assertGreater(self.client.pacer.interval, 0)
        self.assertEqual([call.args[0] for call in self.transport.request.call_args_list], ['GET', 'POST', 'GET', 'POST'])
        self.assertEqual(self.ledger.remaining, 18)
        self.assertEqual(self.transport.request.call_args_list[0].kwargs['timeout'], 15)
        self.assertEqual(self.mt_calls()[0].kwargs['timeout'], 120)

    def test_untyped_business_unknown_is_fatal_and_raw_codes_are_ignored(self):
        for body in [{'error': {'code': 503, 'message': 'private'}},
                     {'choices': [{'error': {'metadata': {'error_type': 'private'}}}]},
                     {'choices': [{'finish_reason': 'error'}]}]:
            error = service_failure(200, body, free_router=True)
            self.assertFalse(error.retryable)
            self.assertEqual(error.category, 'service_unknown')
            self.assertNotIn('private', str(error))
        for status in [400, 401, 402, 403, 413, 422]:
            self.assertFalse(service_failure(status, {'error': {'metadata': {'error_type': 'timeout'}}}).retryable)

    def test_fixed_model_404_is_permanent_even_with_a_transient_business_type(self):
        body = {'error': {'metadata': {'error_type': 'provider_unavailable'}}}
        self.assertFalse(service_failure(404, body, free_router=False).retryable)

    def test_malformed_typed_error_does_not_fall_back_to_a_transient_status(self):
        for kind in [False, 503, [], {}, None]:
            with self.subTest(kind=kind):
                error = service_failure(503, {'error': {'metadata': {'error_type': kind}}})
                self.assertFalse(error.retryable)
                self.assertEqual(error.category, 'service_unknown')

    def test_cancellation_after_registration_does_not_send_or_query_again(self):
        self.replies(HTTPReply(200, b'{}'))
        self.client.count = lambda name: self.control.cancel_user() if name == 'mt' else None
        with self.assertRaises(Cancelled):
            self.send()
        self.assertEqual(self.mt_calls(), [])
        self.assertEqual(self.transport.request.call_count, 1)

    def test_slow_quota_refresh_rechecks_recovery_and_records_actual_admission_time(self):
        reserve(self.ledger, self.control, lambda: quota())
        self.ledger.complete()
        self.client.pacer.requests_per_window = 1
        self.client.pacer.window_seconds = 60
        old_inflight = self.client.recovery.acquire(self.control, self.client.pacer)
        sent_at = []
        def request(method, *args, **kwargs):
            if method == 'GET':
                self.clock.now += 10
                self.client.recovery.failure(old_inflight)
                return quota()
            sent_at.append(self.clock.now)
            return HTTPReply(200, b'{"ok":true}')
        self.transport.request.side_effect = request
        self.assertEqual(self.send(), {'ok': True})
        self.assertGreaterEqual(sent_at[0], 185)  # RPM until 160, query until 170, new cooldown until 185
        self.assertAlmostEqual(self.client.pacer.starts[-1], sent_at[0])
