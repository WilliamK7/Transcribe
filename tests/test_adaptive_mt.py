from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from unittest import mock

import requests

from subtitle_tool.api import Http, RequestPacer
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

    def raise_spacing_to_max(self):
        for expected in [1.05, 2.1, 4.2, 8.0]:
            generation = self.pacer.wait(self.cancel)
            _, interval = self.pacer.throttle(0, generation)
            self.assertAlmostEqual(interval, expected)

    def test_short_job_keeps_fast_dispatch_without_per_request_spacing(self):
        for _ in range(50):
            self.pacer.success(self.pacer.wait(self.cancel))
        self.assertEqual(self.now, 0)
        self.cancel.wait.assert_not_called()

    def test_long_job_waits_for_rolling_quota_before_sending_more(self):
        starts = []
        for _ in range(121):
            self.pacer.success(self.pacer.wait(self.cancel))
            starts.append(self.now)
        self.assertEqual(starts[:60], [0] * 60)
        for start in starts[60:120]:
            self.assertAlmostEqual(start, 61)
        self.assertAlmostEqual(starts[120], 122)

    def test_window_releases_only_expired_requests_not_a_whole_batch(self):
        self.pacer.wait(self.cancel)
        self.now = 10
        for _ in range(59):
            self.pacer.wait(self.cancel)
        self.pacer.wait(self.cancel)
        self.assertAlmostEqual(self.now, 61)
        self.pacer.wait(self.cancel)
        self.assertAlmostEqual(self.now, 71)

    def test_quota_and_server_cooldown_use_later_deadline(self):
        for cooldown, expected in [(15, 61), (90, 90)]:
            with self.subTest(cooldown=cooldown):
                self.now = 0
                pacer = RequestPacer(clock=lambda: self.now)
                for _ in range(60):
                    generation = pacer.wait(self.cancel)
                pacer.throttle(cooldown, generation)
                pacer.wait(self.cancel)
                self.assertAlmostEqual(self.now, expected)

    def test_http_clients_share_quota_and_report_one_long_wait(self):
        events = []
        clients = [Http(self.cancel, pacer=self.pacer, progress=lambda *args: events.append(args))
                   for _ in range(2)]
        starts = []
        def request(*args, **kwargs):
            starts.append(self.now)
            return response({'ok': True})
        with mock.patch('requests.request', side_effect=request):
            for index in range(62):
                clients[index % 2].json('POST', 'https://example.test', name='mt')
        self.assertEqual(starts[:60], [0] * 60)
        self.assertAlmostEqual(starts[60], 61)
        self.assertAlmostEqual(starts[61], 61)
        self.assertEqual(events, [('mt_quota_wait', 61.0, 60)])

    def test_retry_consumes_quota_and_waits_for_both_window_and_cooldown(self):
        starts = []
        http = Http(self.cancel, pacer=self.pacer)
        def request(*args, **kwargs):
            starts.append(self.now)
            return response({}, 429) if len(starts) == 60 else response({'ok': True})
        with mock.patch('requests.request', side_effect=request):
            for _ in range(60):
                self.assertTrue(http.json('POST', 'https://example.test', name='mt')['ok'])
        self.assertEqual(len(starts), 61)
        self.assertEqual(starts[:60], [0] * 60)
        self.assertAlmostEqual(starts[-1], 61)

    def test_default_workers_are_four_with_existing_range(self):
        self.assertEqual(Settings().mt_workers, 4)
        for value in [1, 2, 3, 4]:
            Settings(mt_workers=value).validate()
        for value in [0, 5]:
            with self.assertRaises(ToolError):
                Settings(mt_workers=value).validate()

    def test_zero_retry_after_still_applies_new_spacing_to_last_start(self):
        generation = self.pacer.wait(self.cancel)
        self.assertEqual(self.pacer.throttle(0, generation), (0, 1.05))
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
        for seconds, expected in [(15, 1.05), (30, 2.1), (60, 4.2), (60, 8), (60, 8)]:
            generation = self.pacer.wait(self.cancel)
            added, interval = self.pacer.throttle(seconds, generation)
            self.assertEqual(added, seconds)
            self.assertAlmostEqual(interval, expected)
        deadline = self.now + 60
        self.pacer.wait(self.cancel)
        self.assertAlmostEqual(self.now, deadline)
        self.pacer.wait(self.cancel)
        self.assertAlmostEqual(self.now, deadline + 8)

    def test_same_generation_429_responses_merge_and_next_generation_can_throttle(self):
        generations = [self.pacer.wait(self.cancel) for _ in range(4)]
        for index, generation in enumerate(reversed(generations)):
            self.assertEqual(self.pacer.throttle(15, generation), (15 if index == 0 else 0, 1.05))
            self.assertEqual(self.pacer.generation, 1)
        generation = self.pacer.wait(self.cancel)
        self.assertAlmostEqual(self.now, 15)
        self.assertEqual(self.pacer.throttle(30, generation), (30, 2.1))
        self.assertEqual(self.pacer.generation, 2)

    def test_cooldown_metrics_exclude_spacing_and_do_not_double_count_overlap(self):
        pacer = self.pacer
        generation = pacer.wait(self.cancel)
        self.assertEqual(pacer.throttle(0, generation), (0, 1.05))
        self.assertEqual(pacer.throttle(15, generation), (15, 1.05))
        self.assertEqual(pacer.throttle(15, generation), (0, 1.05))
        self.now = 3
        self.assertEqual(pacer.throttle(15, generation), (3, 1.05))
        self.now = 30
        self.assertEqual(pacer.throttle(15, generation), (15, 1.05))

    def test_four_successes_reduce_spacing_by_thirty_percent_and_eventually_clear_it(self):
        self.raise_spacing_to_max()
        for expected in [5.6, 3.92, 2.744]:
            previous = self.pacer.interval
            for _ in range(3):
                self.assertIsNone(self.pacer.success(self.pacer.wait(self.cancel)))
                self.assertEqual(self.pacer.interval, previous)
            before, after = self.pacer.success(self.pacer.wait(self.cancel))
            self.assertEqual(before, previous)
            self.assertAlmostEqual(after, expected)
            self.assertAlmostEqual(self.pacer.interval, expected)
        recoveries = []
        for _ in range(40):
            change = self.pacer.success(self.pacer.wait(self.cancel))
            if change:
                recoveries.append(change)
        self.assertEqual(self.pacer.interval, 0)
        self.assertEqual(recoveries[-1][1], 0)
        now = self.now
        for _ in range(8):
            self.pacer.wait(self.cancel)
        self.assertEqual(self.now, now)

    def test_old_successes_and_failures_do_not_change_current_recovery_window(self):
        old = self.pacer.wait(self.cancel)
        self.pacer.throttle(0, old)
        for _ in range(3):
            self.pacer.success(self.pacer.wait(self.cancel))
            for _ in range(30):
                self.assertIsNone(self.pacer.success(old))
            self.pacer.failure(old)
        self.assertEqual(self.pacer.interval, 1.05)
        self.assertEqual(self.pacer.success(self.pacer.wait(self.cancel)), (1.05, 1.05 * 0.7))

    def test_out_of_order_successes_before_a_new_429_cannot_recover(self):
        self.pacer.throttle(0, self.pacer.wait(self.cancel))
        pending = [self.pacer.wait(self.cancel) for _ in range(4)]
        self.pacer.throttle(0, pending[-1])
        for generation in reversed(pending[:-1]):
            self.assertIsNone(self.pacer.success(generation))
        self.assertEqual(self.pacer.successes, 0)
        self.assertEqual(self.pacer.interval, 2.1)
        for _ in range(4):
            self.pacer.success(self.pacer.wait(self.cancel))
        self.assertAlmostEqual(self.pacer.interval, 1.47)

    def test_late_429_extends_cooldown_and_resets_successes_without_another_penalty(self):
        old = self.pacer.wait(self.cancel)
        self.pacer.throttle(15, old)
        for _ in range(3):
            self.pacer.success(self.pacer.wait(self.cancel))
        self.assertEqual(self.pacer.successes, 3)
        self.assertEqual(self.pacer.throttle(40, old), (40, 1.05))
        self.assertEqual(self.pacer.generation, 1)
        self.assertEqual(self.pacer.successes, 0)
        deadline = self.now + 40
        self.pacer.success(self.pacer.wait(self.cancel))
        self.assertAlmostEqual(self.now, deadline)
        self.assertEqual(self.pacer.interval, 1.05)

    def test_failure_breaks_consecutive_success_window(self):
        self.pacer.throttle(0, self.pacer.wait(self.cancel))
        for _ in range(3):
            self.pacer.success(self.pacer.wait(self.cancel))
        self.pacer.failure(self.pacer.wait(self.cancel))
        for _ in range(3):
            self.pacer.success(self.pacer.wait(self.cancel))
        self.assertEqual(self.pacer.interval, 1.05)
        self.pacer.success(self.pacer.wait(self.cancel))
        self.assertAlmostEqual(self.pacer.interval, 0.735)

    def test_recovery_updates_waiting_requests_without_shortening_cooldown(self):
        for cooldown in [0, 20]:
            with self.subTest(cooldown=cooldown):
                self.now = 0
                self.pacer = RequestPacer(clock=lambda: self.now)
                self.cancel.wait.side_effect = self.advance
                self.raise_spacing_to_max()
                pending = [self.pacer.wait(self.cancel) for _ in range(4)]
                last_started = self.now
                if cooldown:
                    self.pacer.throttle(cooldown, pending[0] - 1)

                def recover_during_wait(seconds):
                    self.advance(seconds)
                    if pending:
                        for generation in pending:
                            self.pacer.success(generation)
                        pending.clear()
                    return False

                self.cancel.wait.side_effect = recover_during_wait
                deadline = last_started + max(cooldown, 5.6)
                self.pacer.wait(self.cancel)
                self.assertAlmostEqual(self.now, deadline)
                self.assertAlmostEqual(self.pacer.interval, 5.6)
                self.pacer.wait(self.cancel)
                self.assertAlmostEqual(self.now, deadline + 5.6)

    def test_429_extending_cooldown_during_spacing_wait_updates_deadline(self):
        old = self.pacer.wait(self.cancel)
        self.pacer.throttle(0, old)
        deadline = None

        def throttle_during_wait(seconds):
            nonlocal deadline
            self.assertLessEqual(seconds, 0.1)
            self.advance(seconds)
            if deadline is None:
                self.pacer.throttle(30, old)
                deadline = self.now + 30
            return False

        self.cancel.wait.side_effect = throttle_during_wait
        self.pacer.wait(self.cancel)
        self.assertAlmostEqual(self.now, deadline)

    def test_valid_retry_after_seconds_and_dates_are_not_capped_at_sixty(self):
        for header, seconds in [('125', 125.0), (formatdate(1125, usegmt=True), 125.0),
                                ('0', 0.0), ('1.25', 1.25), (formatdate(900, usegmt=True), 0.0)]:
            with self.subTest(header=header):
                pacer = mock.Mock()
                pacer.wait.return_value = 7
                pacer.throttle.return_value = (seconds, 1.05)
                pacer.success.return_value = None
                http = Http(threading.Event(), pacer=pacer)
                with mock.patch('time.time', return_value=1000), mock.patch('requests.request', side_effect=[
                        response({}, 429, {'Retry-After': header}), response({'ok': True})]):
                    self.assertTrue(http.json('POST', 'https://example.test', name='mt')['ok'])
                pacer.throttle.assert_called_once_with(seconds, 7)

    def test_invalid_retry_after_falls_back_and_final_429_still_throttles(self):
        for header in ['', 'NaN', 'inf', '-1', 'bad date', 'Wed, 21 Oct 2015 07:28:00']:
            with self.subTest(header=header):
                self.now = 0
                pacer = RequestPacer(clock=lambda: self.now)
                counts, events = [], []
                http = Http(self.cancel, counts.append, pacer=pacer,
                            progress=lambda *args: events.append(args))
                with mock.patch('requests.request', side_effect=[response({}, 429, {'Retry-After': header}) for _ in range(4)]) as request:
                    with self.assertRaises(ToolError):
                        http.json('POST', 'https://example.test', name='mt')
                self.assertEqual(request.call_count, 4)
                self.assertEqual(counts.count('mt'), 4)
                self.assertEqual(counts.count('mt_429'), 4)
                self.assertEqual(counts.count('mt_success'), 0)
                cooldowns = [event for event in events if event[0] == 'mt_cooldown']
                self.assertEqual([event[1] for event in cooldowns], [15, 30, 60, 60])
                self.assertEqual(pacer.interval, 8)
                self.assertAlmostEqual(self.now, 105)
                self.assertAlmostEqual(pacer.cooldown_until, 165)

    def test_http_failures_break_current_success_window(self):
        for reply in [requests.ConnectionError(), response({}, 403), response({}, 503)]:
            with self.subTest(reply=type(reply).__name__, status=getattr(reply, 'status_code', None)):
                pacer = RequestPacer(clock=lambda: self.now)
                pacer.throttle(0, pacer.wait(self.cancel))
                for _ in range(3):
                    pacer.success(pacer.wait(self.cancel))
                http = Http(self.cancel, attempts=1, pacer=pacer)
                with mock.patch('requests.request', side_effect=[reply]), self.assertRaises(ToolError):
                    http.json('POST', 'https://example.test', name='mt')
                self.assertEqual(pacer.successes, 0)
                self.assertEqual(pacer.interval, 1.05)

    def test_http_reports_change_snapshots_instead_of_mutable_interval(self):
        pacer, progress = mock.Mock(interval=2.744), mock.Mock()
        pacer.wait.side_effect = [11, 12]
        pacer.throttle.return_value = (15, 1.05)
        pacer.success.return_value = (8, 5.6)
        http = Http(threading.Event(), pacer=pacer, progress=progress)
        with mock.patch('requests.request', side_effect=[response({}, 429), response({'ok': True})]):
            self.assertTrue(http.json('POST', 'https://example.test', name='mt')['ok'])
        pacer.throttle.assert_called_once_with(15, 11)
        pacer.success.assert_called_once_with(12)
        self.assertEqual(progress.call_args_list, [mock.call('mt_cooldown', 15, 1.05),
                                                  mock.call('retry', 1, 4), mock.call('mt_recovery', 8, 5.6)])

    def test_cancel_interrupts_quota_and_shared_cooldown_waits(self):
        for quota_full in [False, True]:
            with self.subTest(quota_full=quota_full):
                event = threading.Event()
                pacer = RequestPacer()
                if quota_full:
                    for _ in range(60):
                        pacer.wait(event)
                else:
                    pacer.throttle(60, pacer.wait(event))
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

    def test_four_concurrent_429_responses_increase_spacing_only_once(self):
        barrier = threading.Barrier(4)
        pacer = RequestPacer()
        counts, events = [], []
        http = Http(threading.Event(), counts.append, attempts=1, pacer=pacer,
                    progress=lambda *args: events.append(args))

        def request(*args, **kwargs):
            barrier.wait(timeout=2)
            return response({}, 429, {'Retry-After': '15'})

        with mock.patch('requests.request', side_effect=request), ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(http.json, 'POST', 'https://example.test', name='mt') for _ in range(4)]
            for future in futures:
                with self.assertRaises(ToolError):
                    future.result(timeout=3)
        self.assertAlmostEqual(pacer.interval, 1.05)
        self.assertEqual(pacer.generation, 1)
        self.assertEqual(counts.count('mt_429'), 4)
        self.assertEqual(len(events), 4)
        self.assertTrue(all(event[0] == 'mt_cooldown' and event[2] == 1.05 for event in events))

    def test_http_reports_recovery_on_every_four_successes(self):
        events = []

        def progress(*args):
            self.assertTrue(self.pacer.lock.acquire(blocking=False))
            self.pacer.lock.release()
            events.append(args)

        http = Http(self.cancel, pacer=self.pacer, progress=progress)
        replies = [response({}, 429, {'Retry-After': '0'})] + [response({'ok': True}) for _ in range(8)]
        with mock.patch('requests.request', side_effect=replies):
            for _ in range(8):
                self.assertTrue(http.json('POST', 'https://example.test', name='mt')['ok'])
        recoveries = [event for event in events if event[0] == 'mt_recovery']
        self.assertEqual(len(recoveries), 2)
        self.assertAlmostEqual(recoveries[0][1], 1.05)
        self.assertAlmostEqual(recoveries[0][2], 0.735)
        self.assertAlmostEqual(recoveries[1][1], 0.735)
        self.assertAlmostEqual(recoveries[1][2], 0.5145)
