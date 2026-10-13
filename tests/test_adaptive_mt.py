from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from unittest import mock

from subtitle_tool.api import Http, RequestPacer, mt_pacer
from subtitle_tool.common import Cancelled, ToolError
from subtitle_tool.config import Settings
from tests.fixtures import response


class AdaptiveSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.cancel = mock.Mock()
        self.cancel.is_set.return_value = False
        self.cancel.wait.side_effect = self.advance
        self.pacer = RequestPacer(clock=lambda: self.now)

    def advance(self, seconds):
        self.now += seconds
        return False

    def test_initial_requests_have_no_fixed_delay(self):
        for _ in range(8):
            self.pacer.wait(self.cancel)
        self.assertEqual(self.now, 0)
        self.cancel.wait.assert_not_called()

    def test_default_workers_are_four_with_existing_range(self):
        self.assertEqual(Settings().mt_workers, 4)
        for value in [1, 2, 3, 4]:
            Settings(mt_workers=value).validate()
        for value in [0, 5]:
            with self.assertRaises(ToolError):
                Settings(mt_workers=value).validate()

    def test_zero_retry_after_still_applies_new_spacing_to_last_start(self):
        self.pacer.wait(self.cancel)
        self.pacer.throttle(0)
        self.pacer.wait(self.cancel)
        self.assertAlmostEqual(self.now, 1.05)

    def test_two_http_clients_share_the_same_cooldown_and_spacing(self):
        clients = [Http(self.cancel, pacer=self.pacer), Http(self.cancel, pacer=self.pacer)]
        with mock.patch('requests.request', side_effect=[response({}, 429, {'Retry-After': '22'}), response({'ok': True}), response({'ok': True})]):
            clients[0].json('POST', 'https://example.test', name='mt')
            self.assertEqual(self.now, 22)
            clients[1].json('POST', 'https://example.test', name='mt')
            self.assertAlmostEqual(self.now, 23.05)

    def test_throttling_doubles_spacing_and_shares_cooldown(self):
        self.pacer.wait(self.cancel)
        for seconds, expected in [(15, 1.05), (30, 2.1), (60, 4.2), (60, 8), (60, 8)]:
            self.pacer.throttle(seconds)
            self.assertAlmostEqual(self.pacer.interval, expected)
        self.pacer.wait(self.cancel)
        self.assertEqual(self.now, 60)
        self.pacer.wait(self.cancel)
        self.assertEqual(self.now, 68)

    def test_cooldown_metrics_exclude_spacing_and_do_not_double_count_overlap(self):
        pacer = self.pacer
        for _ in range(4):
            pacer.throttle(0)
        pacer.wait(self.cancel)
        self.assertEqual(pacer.throttle(15), 15)
        self.assertEqual(pacer.throttle(15), 0)
        self.now = 3
        self.assertEqual(pacer.throttle(15), 3)
        self.now = 30
        self.assertEqual(pacer.throttle(15), 15)

    def test_twenty_successes_recover_gradually_and_exclude_older_requests(self):
        old = self.pacer.wait(self.cancel)
        self.pacer.throttle(15)
        for _ in range(30):
            self.pacer.success(old)
        self.assertEqual(self.pacer.interval, 1.05)
        for count in range(19):
            self.pacer.success(self.pacer.wait(self.cancel))
        self.assertEqual(self.pacer.interval, 1.05)
        self.pacer.success(self.pacer.wait(self.cancel))
        self.assertAlmostEqual(self.pacer.interval, 0.84)
        for _ in range(20 * 20):
            self.pacer.success(self.pacer.wait(self.cancel))
        self.assertEqual(self.pacer.interval, 0)

    def test_failure_breaks_consecutive_success_window(self):
        self.pacer.throttle(0)
        for _ in range(19):
            self.pacer.success(self.pacer.wait(self.cancel))
        self.pacer.failure(self.pacer.wait(self.cancel))
        self.pacer.success(self.pacer.wait(self.cancel))
        self.assertEqual(self.pacer.interval, 1.05)

    def test_scope_shares_key_region_model_across_clients(self):
        self.assertIs(mt_pacer('offline-adaptive', 'beijing'), mt_pacer('offline-adaptive', 'beijing'))
        self.assertIsNot(mt_pacer('offline-adaptive', 'beijing'), mt_pacer('offline-another', 'beijing'))
        self.assertIsNot(mt_pacer('offline-adaptive', 'beijing'), mt_pacer('offline-adaptive', 'singapore'))

    def test_valid_retry_after_seconds_and_dates_are_not_capped_at_sixty(self):
        for header in ['125', formatdate(1000 + 125, usegmt=True), '0']:
            with self.subTest(header=header):
                pacer = mock.Mock()
                http = Http(threading.Event(), pacer=pacer)
                with mock.patch('time.time', return_value=1000), mock.patch('requests.request', side_effect=[
                        response({}, 429, {'Retry-After': header}), response({'ok': True})]):
                    self.assertTrue(http.json('POST', 'https://example.test', name='mt')['ok'])
                pacer.throttle.assert_called_once_with(0.0 if header == '0' else 125.0)

    def test_invalid_retry_after_falls_back_and_final_429_still_throttles(self):
        for header in ['', 'NaN', 'inf', '-1', 'bad date']:
            with self.subTest(header=header):
                pacer, count = mock.Mock(), mock.Mock()
                http = Http(threading.Event(), count=count, pacer=pacer)
                with mock.patch('requests.request', side_effect=[response({}, 429, {'Retry-After': header}) for _ in range(4)]) as request:
                    with self.assertRaises(ToolError):
                        http.json('POST', 'https://example.test', name='mt')
                self.assertEqual(request.call_count, 4)
                self.assertEqual([c.args[0] for c in pacer.throttle.call_args_list], [15, 30, 60, 60])
                self.assertEqual([c.args[0] for c in count.call_args_list].count('mt_429'), 4)

    def test_cancel_interrupts_shared_cooldown(self):
        event = threading.Event()
        pacer = RequestPacer()
        pacer.throttle(60)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(pacer.wait, event)
            event.set()
            with self.assertRaises(Cancelled):
                future.result(timeout=2)

    def test_initial_http_requests_can_overlap_with_four_workers(self):
        barrier = threading.Barrier(4)
        pacer = RequestPacer()
        http = Http(threading.Event(), pacer=pacer)
        def request(*args, **kwargs):
            barrier.wait(timeout=2)
            return response({'ok': True})
        with mock.patch('requests.request', side_effect=request), ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(http.json, 'POST', 'https://example.test', name='mt') for _ in range(4)]
            self.assertTrue(all(f.result(timeout=3)['ok'] for f in futures))
