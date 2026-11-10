"""Offline regression tests for the gateway's bounded request lifecycle."""
from __future__ import annotations
import base64

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
from subtitle_tool.mt_transport import ProcessTransport, TransportError
from subtitle_tool.mt_runtime import service_failure

from tests.fixtures import TemporaryTest


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
        self.headers = {}
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
                    for key, value in owner.headers.items():
                        self.send_header(key, value)
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

    def test_only_underlying_identity_response_header_crosses_process_boundary(self):
        self.server.headers = {'X-Vsr-Selected-Model': 'qwen/qwen3.8-27b',
                               'Set-Cookie': 'private-cookie', 'Authorization': 'private-key'}
        response = self.send('/ok', timeout=5)
        self.assertEqual(response.headers, {'x-vsr-selected-model': 'qwen/qwen3.8-27b'})

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

    def test_gateway_queue_uses_the_full_request_deadline(self):
        import io
        from types import SimpleNamespace
        from subtitle_tool import _mt_http_worker as worker
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status_code, response.headers = 200, {}
        response.iter_content.return_value = [b'{"ok":true}']
        def upstream(*args, **kwargs):
            # Simulate a gateway that sends no bytes during a long queue/fallback.
            if kwargs['timeout'][1] < 150:
                raise worker.requests.ReadTimeout()
            return response
        def communicate(payload=None, **kwargs):
            if payload is None:
                return b'', b''
            with mock.patch.object(worker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(payload))), \
                    mock.patch.object(worker.requests, 'request', side_effect=upstream):
                return json.dumps(worker.main()).encode(), b''
        child = mock.MagicMock(returncode=0)
        child.poll.return_value = 0
        child.communicate.side_effect = communicate
        with mock.patch('subtitle_tool.mt_transport.subprocess.Popen', return_value=child):
            self.assertEqual(self.send('/ok', timeout=180).body, b'{"ok":true}')

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

    def test_non_translation_requests_do_not_use_the_translation_drain_window(self):
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


class ServiceFailureTests(TemporaryTest):
    def test_gateway_errors_are_classified_without_exposing_messages_or_raw_codes(self):
        for kind in ['timeout', 'provider_unavailable', 'not_found', 'authentication', 'permission_denied']:
            with self.subTest(kind=kind):
                failure = service_failure(200, {'error': {'metadata': {'error_type': kind},
                                                       'message': 'private response', 'code': 'raw-code'}})
                self.assertEqual(failure, kind)
                self.assertNotIn('private', str(failure))
        for kind in [None, [], {}, 'unknown-value']:
            with self.subTest(kind=kind):
                failure = service_failure(200, {'error': {'metadata': {'error_type': kind}}})
                self.assertEqual(failure, 'service_unknown')

    def test_refusal_and_content_filter_take_precedence(self):
        for choice in [{'finish_reason': 'content_filter'}, {'message': {'refusal': 'private response'}}]:
            body = {'error': {'metadata': {'error_type': 'timeout'}}, 'choices': [choice]}
            self.assertEqual(service_failure(200, body), 'content_policy_violation')

    def test_statuses_and_generation_failures_have_safe_categories(self):
        for status, category in [(401, 'http_permanent'), (404, 'http_permanent'),
                                 (408, 'http_transient'), (429, 'http_transient'), (503, 'http_transient')]:
            with self.subTest(status=status):
                self.assertEqual(service_failure(status, {}), category)
        self.assertEqual(service_failure(200, {'choices': [{'finish_reason': 'error'}]}),
                         'generation_error')
        self.assertIsNone(service_failure(200, {'choices': [{'finish_reason': 'stop'}]}))

    def test_malformed_or_mixed_errors_are_not_treated_as_success(self):
        for error in [None, 'private response', {'metadata': {'error_type': []}}]:
            body = {'error': {'metadata': {'error_type': 'timeout'}}, 'choices': [{'error': error}]}
            self.assertEqual(service_failure(200, body), 'service_unknown')
