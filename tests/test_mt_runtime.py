"""Offline regression tests for OpenRouter's bounded request lifecycle."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from subtitle_tool.common import Cancelled
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_transport import HTTPReply, ProcessTransport, TransportError
from subtitle_tool.mt_runtime import service_failure

from tests.fixtures import TemporaryTest


def business_failure(kind='not_found', *, status=200, choice=False, finish_marker=False):
    error = {'message': 'private provider details', 'metadata': {'error_type': kind}}
    body = {'choices': [{'finish_reason': 'error', 'error': error}]} if choice else {'error': error}
    if finish_marker:
        body.setdefault('choices', []).append({'index': 0, 'finish_reason': 'error'})
    return HTTPReply(status, json.dumps(body).encode())


def generation_failure(*, status=200):
    return HTTPReply(status, b'{"choices":[{"finish_reason":"error","message":{"content":null}}]}')


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


class ServiceFailureTests(TemporaryTest):
    def test_generation_error_uses_only_allowed_http_statuses(self):
        body = json.loads(generation_failure().body)
        for free_router in [False, True]:
            for status in [200, 404, 408, 429, 503]:
                with self.subTest(status=status, free_router=free_router):
                    error = service_failure(status, body, free_router=free_router)
                    self.assertEqual(error.retryable, status != 404 or free_router)
                    self.assertEqual(error.category, 'generation_error' if error.retryable else 'http_permanent')
            for status in [301, 400, 401, 402, 403, 409, 413, 422]:
                with self.subTest(status=status, free_router=free_router):
                    self.assertFalse(service_failure(status, body, free_router=free_router).retryable)

    def test_generation_marker_preserves_explicit_error_classification(self):
        marker = {'finish_reason': 'error'}
        cases = [
            ({'error': {}, 'choices': [marker]}, 'service_unknown', False),
            ({'choices': [{**marker, 'error': None}]}, 'service_unknown', False),
            ({'choices': [marker, {'error': {'metadata': {'error_type': 'timeout'}}}]},
             'service_unknown', False),
            ({'error': {'metadata': {'error_type': 'timeout'}}, 'choices': [marker]}, 'timeout', True),
            ({'error': {'metadata': {'error_type': 'generation_error'}}, 'choices': [marker]},
             'service_unknown', False),
        ]
        for body, category, retryable in cases:
            with self.subTest(category=category, body=body):
                error = service_failure(200, body, free_router=True)
                self.assertEqual((error.category, error.retryable), (category, retryable))

    def test_typed_top_error_explains_detail_free_error_finish(self):
        for kind in ['timeout', 'provider_unavailable', 'provider_overloaded', 'server', 'rate_limit_exceeded', 'not_found']:
            for status in [200, 503]:
                with self.subTest(kind=kind, status=status):
                    body = json.loads(business_failure(kind, status=status, finish_marker=True).body)
                    body['choices'].append({'index': 1, 'finish_reason': 'error'})
                    error = service_failure(status, body, free_router=True)
                    self.assertEqual(error.category, kind)
                    self.assertTrue(error.retryable)

    def test_untyped_top_error_keeps_http_status_policy_with_or_without_finish_marker(self):
        for error in [None, {}, {'message': 'private'}, {'code': 503}]:
            for marker in [False, True]:
                with self.subTest(error=error, marker=marker):
                    body = {'error': error}
                    if marker:
                        body['choices'] = [{'finish_reason': 'error'}]
                    failure = service_failure(503, body, free_router=True)
                    self.assertEqual(failure.category, 'http_transient')
                    self.assertTrue(failure.retryable)

    def test_top_error_marker_keeps_permanent_refusal_and_fixed_404_precedence(self):
        for kind in ['authentication', 'permission_denied', 'payment_required', 'invalid_request',
                     'content_policy_violation', 'refusal']:
            for companion in [None, {}, {'metadata': {'error_type': None}}, {'metadata': {'error_type': 'private'}}]:
                with self.subTest(kind=kind, companion=companion):
                    body = json.loads(business_failure(kind, finish_marker=True).body)
                    body['choices'].append({'error': companion})
                    error = service_failure(503, body, free_router=True)
                    self.assertEqual(error.category, kind)
                    self.assertFalse(error.retryable)
        body = json.loads(business_failure('provider_unavailable', finish_marker=True).body)
        self.assertFalse(service_failure(404, body, free_router=False).retryable)
        for refusal in [{'finish_reason': 'content_filter'}, {'message': {'refusal': 'private refusal'}}]:
            body['choices'] = [{'error': None}, {'error': {'metadata': {'error_type': 'payment_required'}}}, refusal]
            error = service_failure(200, body, free_router=True)
            self.assertEqual(error.category, 'content_policy_violation')
            self.assertFalse(error.retryable)
            self.assertEqual(error.code, 'mt_content_refused')


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

    def test_mixed_typed_and_unknown_errors_stop_with_or_without_finish_marker(self):
        unknowns = [{'metadata': {'error_type': value}} for value in ['private-unknown', None, False, 42, [], {}]]
        unknowns.extend([{}, None, [], False, {'metadata': []}])
        for kind in ['not_found', 'provider_unavailable']:
            known = {'metadata': {'error_type': kind}}
            for status in [200, 404, 408, 429, 503]:
                for unknown in unknowns:
                    for reverse, marker in [(False, False), (True, False), (False, True), (True, True)]:
                        errors = [unknown, known] if reverse else [known, unknown]
                        choices = [{'finish_reason': 'error', 'error': errors[1]}]
                        if marker:
                            choices.append({'finish_reason': 'error'})
                        with self.subTest(kind=kind, status=status, unknown=unknown, reverse=reverse, marker=marker):
                            error = service_failure(status, {'error': errors[0], 'choices': choices}, free_router=True)
                            self.assertEqual(error.category, 'service_unknown')
                            self.assertFalse(error.retryable)

    def test_content_filter_and_refusal_override_router_not_found(self):
        for refusal in [{'finish_reason': 'content_filter'}, {'message': {'refusal': 'private refusal'}}]:
            body = json.loads(business_failure().body)
            body['choices'] = [refusal]
            error = service_failure(200, body, free_router=True)
            self.assertEqual(error.category, 'content_policy_violation')
            self.assertFalse(error.retryable)

    def test_untyped_business_unknown_is_fatal_and_raw_codes_are_ignored(self):
        for body in [{'error': {'code': 503, 'message': 'private'}},
                     {'choices': [{'error': {'metadata': {'error_type': 'private'}}}]}]:
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
