"""Synthetic OpenRouter free-quota admission regressions for the v5 router."""
from __future__ import annotations

import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock

from subtitle_tool.common import Cancelled
from subtitle_tool.mt_backend import MTBackend, OPENROUTER_BASE_URL
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_quota import FreeQuota, OpenRouterQuota, reset_quota_for_resume
from subtitle_tool.mt_routing import MTRouter, RouteEntry, RouteFailure
from subtitle_tool.mt_transport import HTTPReply, TransportError


class Clock:
    def __init__(self):
        self.now = 100.0
        self.day = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def wait(self, control, seconds):
        control.check_start()
        self.now += seconds


def candidate(identity='openrouter', model='openrouter/free', tier='free', *, key='a' * 64):
    backend = MTBackend('openai_compatible', OPENROUTER_BASE_URL, model, key, 5)
    return RouteEntry(identity, backend, tier)


def quota_reply(remaining):
    return HTTPReply(200, json.dumps({'data': {'free_model_daily_requests': {
        'used': 10 - remaining, 'limit': 10, 'remaining': remaining}, 'private': 'DO-NOT-LOG'}}).encode())


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.control = MTControl(threading.Event(), clock=self.clock)
        self.ledger = FreeQuota(clock=self.clock, utcnow=lambda: self.clock.day)
        self.reply = quota_reply(4)
        self.requests = []
        self.events = []
        self.transport = mock.Mock()
        self.transport.request.side_effect = self.fetch
        self.quota = OpenRouterQuota(self.control, lambda entry: 'synthetic-key', clock=self.clock,
                             transport=self.transport, ledger_for=lambda backend: self.ledger,
                             on_event=self.events.append)
        self.free = candidate()

    def fetch(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs['timeout'], kwargs['drain']))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def test_first_query_then_atomic_claim_no_query_on_cache_or_paid_or_qiniu(self):
        paid = candidate('paid', model='openrouter/paid', tier='paid')
        qiniu = RouteEntry('qiniu', MTBackend('qiniu', 'https://api.qnaigc.com/v1',
                                            'qiniu/conatus-free', 'b' * 64, 5), 'free')
        for route in (paid, qiniu):
            with self.quota.reserve(route, 15) as reservation:
                reservation.claim()
                reservation.mark_sent()
        router = MTRouter([self.free, paid], paid_enabled=True, control=self.control, clock=self.clock,
                          wait=self.clock.wait)
        result = router.run(lambda *args: self.fail('unexpected network'),
                            cache=lambda route: 'cached' if route == paid else None,
                            prepare=self.quota.reserve)
        self.assertEqual(result.value, 'cached')
        self.assertEqual(self.requests, [])
        with self.quota.reserve(self.free, 15) as reservation:
            self.assertEqual(self.ledger.remaining, 4)
            reservation.claim()
            self.assertEqual((self.ledger.remaining, self.ledger.inflight), (3, 1))
            reservation.mark_sent()
        self.assertEqual((self.ledger.remaining, self.ledger.inflight), (3, 0))
        self.assertEqual(self.requests, [('GET', OPENROUTER_BASE_URL + '/key', 15, False)])
        self.assertNotIn('synthetic-key', repr(self.events))
        self.assertNotIn('DO-NOT-LOG', repr(self.events))

    def test_unsent_reservation_refunds_and_sent_failure_is_conservatively_counted(self):
        with self.quota.reserve(self.free, 15) as reservation:
            reservation.claim()
            self.assertEqual(self.ledger.remaining, 3)
        self.assertEqual((self.ledger.remaining, self.ledger.inflight), (4, 0))
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with self.quota.reserve(self.free, 15) as reservation:
                reservation.claim()
                reservation.mark_sent()
                raise RuntimeError('synthetic')
        self.assertEqual((self.ledger.remaining, self.ledger.inflight), (3, 0))
        self.assertEqual(len(self.requests), 1)

    def test_same_day_refresh_failure_keeps_only_previous_trusted_balance(self):
        with self.quota.reserve(self.free, 15) as reservation:
            reservation.claim()
            reservation.mark_sent()
        self.clock.now += 31
        self.reply = HTTPReply(503, b'private response')
        with self.quota.reserve(self.free, 15) as reservation:
            reservation.claim()
            reservation.mark_sent()
        self.assertEqual(self.ledger.remaining, 2)
        self.assertIn('quota_refresh_failed', [event.get('category') for event in self.events])
        self.assertNotIn('private response', repr(self.events))

    def test_new_run_requires_its_own_observation_after_refresh_failure(self):
        with self.quota.reserve(self.free, 15) as reservation:
            reservation.claim()
            reservation.mark_sent()
        self.clock.now += 31
        control = MTControl(threading.Event(), clock=self.clock)
        newer = OpenRouterQuota(control, lambda entry: 'synthetic-key', clock=self.clock,
                                transport=self.transport, ledger_for=lambda backend: self.ledger)
        self.reply = HTTPReply(503, b'private response')
        with self.assertRaises(RouteFailure) as unknown:
            with newer.reserve(self.free, 15):
                self.fail('a new run cannot trust an earlier run after a failed refresh')
        self.assertEqual(unknown.exception.category, 'quota_unknown')
        # The original run can still use its conservative same-day balance.
        with self.quota.reserve(self.free, 15) as reservation:
            reservation.claim()
            reservation.mark_sent()
        self.assertEqual(self.ledger.remaining, 2)
        self.clock.now += 31
        self.reply = quota_reply(4)
        with newer.reserve(self.free, 15) as reservation:
            reservation.claim()
            reservation.mark_sent()
        self.assertEqual(self.ledger.remaining, 1)
        self.assertEqual(len(self.requests), 3)

    def test_refresh_subtracts_existing_and_new_inflight_requests(self):
        from contextlib import ExitStack
        active = ExitStack()
        self.addCleanup(active.close)
        first = active.enter_context(self.quota.reserve(self.free, 15))
        first.claim()
        first.mark_sent()
        self.clock.now += 31

        def fetch_during_refresh(*args, **kwargs):
            second = active.enter_context(self.quota.reserve(self.free, 15))
            second.claim()
            second.mark_sent()
            return quota_reply(4)

        self.transport.request.side_effect = fetch_during_refresh
        with self.quota.reserve(self.free, 15) as reservation:
            self.assertEqual((self.ledger.remaining, self.ledger.inflight), (2, 2))
            reservation.claim()
            reservation.mark_sent()
        self.assertEqual((self.ledger.remaining, self.ledger.inflight), (1, 2))
        active.close()
        self.assertEqual(self.ledger.inflight, 0)
        self.assertEqual(self.events[-1], {'kind': 'runtime_quota', 'category': 'quota_observed'})

    def test_no_dispatch_after_claim_refunds_before_paid_fallback(self):
        paid = candidate('paid', model='openrouter/paid', tier='paid')
        router = MTRouter([self.free, paid], paid_enabled=True, control=self.control,
                          clock=self.clock, wait=self.clock.wait)
        calls = []

        def invoke(entry, timeout, attempt, reservation):
            calls.append(entry.id)
            if entry == self.free:
                raise RouteFailure('invalid_request')
            reservation.mark_sent()
            return 'paid'

        self.assertEqual(router.run(invoke, prepare=self.quota.reserve).value, 'paid')
        self.assertEqual(calls, ['openrouter', 'paid'])
        self.assertEqual((self.ledger.remaining, self.ledger.inflight), (4, 0))

    def test_zero_wait_budget_allows_immediate_nonquota_and_trusted_quota(self):
        control = MTControl(threading.Event())
        ledger = FreeQuota()
        transport = mock.Mock()
        transport.request.return_value = quota_reply(3)
        quota = OpenRouterQuota(control, lambda entry: 'synthetic-key', transport=transport,
                        ledger_for=lambda backend: ledger)
        with quota.reserve(self.free, 15) as reservation:
            reservation.claim()
            reservation.mark_sent()
        paid = candidate('paid', model='openrouter/paid', tier='paid')
        qiniu = RouteEntry('qiniu', MTBackend('qiniu', 'https://api.qnaigc.com/v1',
                                            'qiniu/conatus-free', 'b' * 64, 5), 'free')
        for free in (self.free, qiniu):
            with self.subTest(free=free.id), mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=True):
                router = MTRouter([free, paid], paid_enabled=True, control=control, free_wait_seconds=0)
                def invoke(entry, timeout, attempt, reservation):
                    reservation.mark_sent()
                    return entry.id
                self.assertEqual(router.run(invoke, prepare=quota.reserve).entry, free)

    def test_zero_wait_budget_does_not_charge_nonblocking_cpu_time(self):
        class TickingClock:
            def __init__(self):
                self.now = 0.0
            def __call__(self):
                self.now += .001
                return self.now

        clock = TickingClock()
        control = MTControl(threading.Event(), clock=clock)
        quota = OpenRouterQuota(control, lambda entry: self.fail('no key needed'), clock=clock)
        qiniu = RouteEntry('qiniu', MTBackend('qiniu', 'https://api.qnaigc.com/v1',
                                            'qiniu/conatus-free', 'b' * 64, 5), 'free')
        paid = candidate('paid', model='openrouter/paid', tier='paid')
        with mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=True):
            router = MTRouter([qiniu, paid], paid_enabled=True, control=control, clock=clock, free_wait_seconds=0)
            def invoke(entry, timeout, attempt, reservation):
                reservation.mark_sent()
                return entry.id
            self.assertEqual(router.run(invoke, prepare=quota.reserve).entry, qiniu)

    def test_only_actual_quota_query_wait_is_charged(self):
        def fetch(*args, **kwargs):
            self.clock.now += 4
            return quota_reply(4)
        self.transport.request.side_effect = fetch
        router = MTRouter([self.free], paid_enabled=False, control=self.control, clock=self.clock,
                          wait=self.clock.wait)
        def invoke(entry, timeout, attempt, reservation):
            reservation.mark_sent()
            return 'ok'
        result = router.run(invoke, prepare=self.quota.reserve)
        self.assertEqual((result.waited, result.free_waited), (4, 4))

    def test_query_and_cooldown_share_one_fifteen_second_free_wait_budget(self):
        paid = candidate('paid', model='openrouter/paid', tier='paid')
        calls = []

        def fetch(*args, **kwargs):
            self.clock.now += 10
            return quota_reply(4)

        def invoke(entry, timeout, attempt, reservation):
            calls.append((entry.id, self.clock.now))
            reservation.mark_sent()
            if entry == self.free:
                raise RouteFailure('rate_limit_exceeded', retryable=True, retry_after=20)
            return 'paid'

        self.transport.request.side_effect = fetch
        router = MTRouter([self.free, paid], paid_enabled=True, control=self.control,
                          clock=self.clock, wait=self.clock.wait)
        result = router.run(invoke, prepare=self.quota.reserve)
        self.assertEqual(calls, [('openrouter', 110), ('paid', 115)])
        self.assertEqual((result.waited, result.free_waited), (15, 15))

    def test_cancel_before_dispatch_refunds_reservation_and_starts_no_fallback(self):
        paid = candidate('paid', model='openrouter/paid', tier='paid')
        calls = []
        router = MTRouter([self.free, paid], paid_enabled=True, control=self.control,
                          clock=self.clock, wait=self.clock.wait)

        def invoke(entry, timeout, attempt, reservation):
            calls.append(entry.id)
            self.control.cancel_user()
            self.control.check_start()

        with self.assertRaises(Cancelled):
            router.run(invoke, prepare=self.quota.reserve)
        self.assertEqual(calls, ['openrouter'])
        self.assertEqual((self.ledger.remaining, self.ledger.inflight), (4, 0))

    def test_changed_observation_does_not_invent_a_refund(self):
        with self.quota.reserve(self.free, 15) as reservation:
            reservation.claim()
            self.clock.now += 31
            self.reply = quota_reply(1)
            with self.assertRaises(RouteFailure) as refreshed:
                with self.quota.reserve(self.free, 15):
                    self.fail('the refreshed balance must account for the outstanding claim')
            self.assertEqual(refreshed.exception.category, 'quota_exhausted')
        with self.assertRaises(RouteFailure) as after_refund:
            with self.quota.reserve(self.free, 15):
                self.fail('closing the old claim must not create new quota')
        self.assertEqual(after_refund.exception.category, 'quota_exhausted')
        self.assertEqual(len(self.requests), 2)

    def test_unknown_and_exhausted_skip_free_but_do_not_disable_paid(self):
        paid = candidate('paid', model='openrouter/paid', tier='paid')
        for reply in (HTTPReply(200, b'{"data":{}}'), HTTPReply(503, b'private'), quota_reply(0)):
            with self.subTest(status=reply.status):
                self.setUp()
                self.reply = reply
                router = MTRouter([self.free, paid], paid_enabled=True, control=self.control,
                                  clock=self.clock, wait=self.clock.wait)
                calls = []
                def invoke(entry, timeout, attempt, reservation):
                    reservation.mark_sent()
                    return calls.append(entry.id) or 'ok'
                result = router.run(invoke, prepare=self.quota.reserve)
                self.assertEqual(result.entry, paid)
                self.assertEqual(calls, ['paid'])
                self.assertEqual(len(self.requests), 1)

    def test_query_timeout_is_bounded_and_failure_is_redacted(self):
        self.reply = TransportError('network')
        with self.assertRaises(RouteFailure) as error:
            with self.quota.reserve(self.free, 3.5):
                self.fail('unexpected reservation')
        self.assertEqual(error.exception.category, 'quota_unknown')
        self.assertEqual(self.requests[0][2], 3.5)
        self.assertNotIn('synthetic-key', str(error.exception))

    def test_utc_day_rollover_allows_fresh_query(self):
        self.reply = quota_reply(0)
        with self.assertRaises(RouteFailure) as error:
            with self.quota.reserve(self.free, 15):
                pass
        self.assertEqual(error.exception.category, 'quota_exhausted')
        self.clock.day += timedelta(days=1)
        self.clock.now += 31
        self.reply = quota_reply(2)
        with self.quota.reserve(self.free, 15) as reservation:
            reservation.claim()
            reservation.mark_sent()
        self.assertEqual(len(self.requests), 2)

    def test_explicit_resume_obtains_a_fresh_quota_observation(self):
        control = MTControl(threading.Event())
        transport = mock.Mock()
        transport.request.side_effect = [quota_reply(0), quota_reply(2)]
        quota = OpenRouterQuota(control, lambda entry: 'synthetic-key', transport=transport)
        with mock.patch.dict('subtitle_tool.mt_quota._QUOTAS', clear=True):
            with self.assertRaises(RouteFailure) as exhausted:
                with quota.reserve(self.free, 15):
                    self.fail('the empty observation must block requests')
            self.assertEqual(exhausted.exception.category, 'quota_exhausted')
            reset_quota_for_resume(self.free.backend)
            with quota.reserve(self.free, 15) as reservation:
                reservation.claim()
                reservation.mark_sent()
        self.assertEqual(transport.request.call_count, 2)

    def test_query_crossing_utc_midnight_is_not_trusted(self):
        def crossing(*args, **kwargs):
            self.clock.day += timedelta(days=1)
            self.clock.now += 1
            return quota_reply(3)
        self.transport.request.side_effect = crossing
        with self.assertRaises(RouteFailure) as error:
            with self.quota.reserve(self.free, 15):
                self.fail('cross-day observation cannot be used')
        self.assertEqual(error.exception.category, 'quota_unknown')
        self.assertIsNone(self.ledger.day)
        self.assertEqual(self.ledger.inflight, 0)

    def test_concurrent_models_share_single_query_and_cannot_oversend(self):
        control = MTControl(threading.Event())
        ledger = FreeQuota()
        requests = []
        lock = threading.Lock()

        class Transport:
            def request(self, method, url, **kwargs):
                with lock:
                    requests.append(1)
                time.sleep(.03)
                return quota_reply(2)

        quota = OpenRouterQuota(control, lambda entry: 'synthetic-key', transport=Transport(),
                        ledger_for=lambda backend: ledger)
        routes = [candidate('a'), candidate('b', model='vendor/model:free')]

        def send(index):
            try:
                with quota.reserve(routes[index % 2], 2) as reservation:
                    reservation.claim()
                    reservation.mark_sent()
                    return True
            except RouteFailure as error:
                self.assertEqual(error.category, 'quota_exhausted')
                return False

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(send, range(4))), 2)
        self.assertEqual(len(requests), 1)
        self.assertEqual((ledger.remaining, ledger.inflight), (0, 0))

    def test_cancellation_during_query_does_not_dispatch(self):
        def fetch(*args, **kwargs):
            self.control.cancel_user()
            raise Cancelled()
        self.transport.request.side_effect = fetch
        with self.assertRaises(Cancelled):
            with self.quota.reserve(self.free, 15):
                self.fail('cancelled query must not reserve')
        self.assertFalse(self.ledger.refreshing)
        self.assertEqual(self.ledger.inflight, 0)


if __name__ == '__main__':
    unittest.main()
