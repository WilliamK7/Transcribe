"""Offline regressions for the translation pool scheduler."""
from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from subtitle_tool.common import Cancelled
from subtitle_tool.mt_backend import MTBackend, QINIU_BASE_URL, QINIU_FREE_MODEL
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_routing import CacheHit, MTRouter, RouteEntry, RouteExhausted, RouteFailure


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []
        self.utc = datetime(2026, 10, 25, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def wait(self, control, seconds):
        control.check_start()
        self.waits.append(seconds)
        self.now += seconds


def entry(name, tier='free', weight=1, *, model=None, key='key-a', url='https://a.example/v1',
          provider='openai_compatible', rpm=0):
    return RouteEntry(name, MTBackend(provider, url, model or name, key, 4), tier,
                      weight=weight, rpm=rpm)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.control = MTControl(threading.Event(), clock=self.clock)

    def router(self, entries, *, paid_enabled=True, **kwargs):
        return MTRouter(entries, paid_enabled=paid_enabled, control=self.control, clock=self.clock,
                        utcnow=lambda: self.clock.utc, wait=self.clock.wait, **kwargs)

    def test_weighted_selection_across_groups_and_preferred_other_backend(self):
        a, b = entry('a', weight=3), entry('b')
        router = self.router([a, b])
        selected = [router.run(lambda e, timeout, attempt: e.id).value for _ in range(1000)]
        self.assertGreaterEqual(selected.count('a'), 650)
        self.assertLessEqual(selected.count('a'), 850)
        self.assertEqual(router.run(lambda e, timeout, attempt: e.id, prefer_other_than=a).entry, b)

    def test_format_failures_are_scoped_to_stage_and_cache_remains_available(self):
        bad, good = entry('bad', weight=3), entry('good')
        router = self.router([bad, good])
        calls = []

        def review(candidate, timeout, attempt):
            calls.append(candidate.id)
            if candidate == bad:
                raise RouteFailure('format_exhausted')
            return 'valid review'

        for _ in range(3):
            self.assertEqual(router.run(review, stage='review', prefer_other_than=good).value, 'valid review')
        self.assertEqual(calls, ['bad', 'good', 'bad', 'good', 'good'])
        self.assertEqual(router.run(lambda candidate, *args: candidate.id, stage='draft',
                                    prefer_other_than=good).value, 'bad')
        cached = router.run(lambda *args: self.fail('cached review must not dispatch'), stage='review',
                            cache=lambda candidate: CacheHit('saved') if candidate == bad else None)
        self.assertEqual(cached.value, 'saved')
        self.assertTrue(cached.cached)

        self.clock.now += 301
        self.assertEqual(router.run(lambda candidate, *args: candidate.id, stage='review',
                                    prefer_other_than=good).value, 'bad')
        self.assertEqual(router.run(lambda candidate, *args: candidate.id, stage='review',
                                    prefer_other_than=good).value, 'bad')

    def test_suspect_free_candidate_is_still_tried_before_paid_fallback(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid])
        calls = []

        def invoke(candidate, timeout, attempt):
            calls.append(candidate.id)
            if candidate == free:
                raise RouteFailure('format_exhausted')
            return 'valid review'

        for _ in range(4):
            self.assertEqual(router.run(invoke, stage='review').value, 'valid review')
        self.assertEqual(calls, ['free', 'paid'] * 4)

    def test_format_recovery_probe_does_not_block_other_healthy_entries(self):
        bad, good = entry('bad', weight=3), entry('good')
        router = self.router([bad, good])

        def fail_bad(candidate, timeout, attempt):
            if candidate == bad:
                raise RouteFailure('format_exhausted')
            return 'good'

        for _ in range(2):
            router.run(fail_bad, stage='review', prefer_other_than=good)
        self.clock.now += 301
        entered, release = threading.Event(), threading.Event()

        def probe(candidate, timeout, attempt):
            self.assertEqual(candidate, bad)
            entered.set()
            if not release.wait(5):
                raise AssertionError('probe not released')
            return candidate.id

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(router.run, probe, stage='review', prefer_other_than=good)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(router.run(lambda candidate, *args: candidate.id, stage='review',
                                            prefer_other_than=good).value, 'good')
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5).value, 'bad')
        self.assertEqual(router.run(lambda candidate, *args: candidate.id, stage='review',
                                    prefer_other_than=good).value, 'bad')

    def test_single_free_format_probe_respects_existing_wait_budget(self):
        for paid_enabled in (True, False):
            for budget in (0, 3, 15):
                with self.subTest(paid_enabled=paid_enabled, budget=budget):
                    self.clock.now = 0
                    self.clock.waits.clear()
                    free, paid = entry('free'), entry('paid', 'paid')
                    router = self.router([free, paid], paid_enabled=paid_enabled, free_wait_seconds=budget)

                    def fail_free(candidate, timeout, attempt):
                        if candidate == free:
                            raise RouteFailure('format_exhausted')
                        return 'paid'

                    for _ in range(2):
                        if paid_enabled:
                            self.assertEqual(router.run(fail_free, stage='review').value, 'paid')
                        else:
                            with self.assertRaises(RouteExhausted):
                                router.run(fail_free, stage='review')
                    self.clock.now += 301
                    entered, release = threading.Event(), threading.Event()

                    def probe(candidate, timeout, attempt):
                        self.assertEqual(candidate, free)
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError('probe not released')
                        return 'free'

                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(router.run, probe, stage='review')
                        try:
                            self.assertTrue(entered.wait(5))
                            self.assertEqual(router.run(lambda candidate, *args: candidate.id,
                                                        stage='draft').value, 'free')
                            if paid_enabled:
                                result = router.run(lambda candidate, *args: candidate.id, stage='review')
                                self.assertEqual(result.value, 'paid')
                                self.assertAlmostEqual(result.free_waited, budget)
                            else:
                                with self.assertRaises(RouteExhausted) as error:
                                    router.run(lambda candidate, *args: candidate.id, stage='review')
                                self.assertAlmostEqual(error.exception.waited, budget)
                        finally:
                            release.set()
                        self.assertEqual(future.result(timeout=5).value, 'free')
                    self.assertEqual(router.run(lambda candidate, *args: candidate.id, stage='review').value,
                                     'free')

    def test_all_unhealthy_free_entries_use_only_one_probe_each(self):
        first, second, paid = entry('first', weight=3), entry('second'), entry('paid', 'paid')
        router = self.router([first, second, paid])

        def fail_free(candidate, timeout, attempt):
            if candidate.tier == 'free':
                raise RouteFailure('format_exhausted')
            return 'paid'

        for _ in range(2):
            self.assertEqual(router.run(fail_free, stage='review').value, 'paid')
        entered, both_entered, release = threading.Event(), threading.Event(), threading.Event()
        lock, probes = threading.Lock(), []

        def probe(candidate, timeout, attempt):
            with lock:
                probes.append(candidate.id)
                entered.set()
                if len(probes) == 2:
                    both_entered.set()
            if not release.wait(5):
                raise AssertionError('probes not released')
            return candidate.id

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(router.run, probe, stage='review')]
            try:
                self.assertTrue(entered.wait(5))
                pending.append(pool.submit(router.run, probe, stage='review'))
                self.assertTrue(both_entered.wait(5))
                self.assertCountEqual(probes, ['first', 'second'])
                result = router.run(lambda candidate, *args: candidate.id, stage='review')
                self.assertEqual(result.value, 'paid')
                self.assertAlmostEqual(result.free_waited, 15)
            finally:
                release.set()
            self.assertCountEqual([future.result(timeout=5).value for future in pending], ['first', 'second'])

    def test_waiter_uses_free_after_active_probe_finishes(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid])

        def fail_free(candidate, timeout, attempt):
            if candidate == free:
                raise RouteFailure('format_exhausted')
            return 'paid'

        for _ in range(2):
            self.assertEqual(router.run(fail_free, stage='review').value, 'paid')
        self.clock.now += 301
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def probe(candidate, timeout, attempt):
            self.assertEqual(candidate, free)
            entered.set()
            if not release.wait(5):
                raise AssertionError('probe not released')
            return 'free'

        def first_run():
            try:
                return router.run(probe, stage='review')
            finally:
                finished.set()

        def wait_for_probe(control, seconds):
            release.set()
            if not finished.wait(5):
                raise AssertionError('probe did not finish')
            self.clock.wait(control, seconds)

        router.wait = wait_for_probe
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first_run)
            try:
                self.assertTrue(entered.wait(5))
                calls = []
                result = router.run(lambda candidate, *args: calls.append(candidate.id) or candidate.id,
                                    stage='review')
                self.assertEqual((result.value, calls), ('free', ['free']))
                self.assertAlmostEqual(result.free_waited, 0.1)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5).value, 'free')

    def test_old_success_cannot_clear_new_format_quarantine(self):
        bad, good = entry('bad', weight=3), entry('good')
        router = self.router([bad, good])
        entered, release = threading.Event(), threading.Event()

        def old_request(candidate, timeout, attempt):
            self.assertEqual(candidate, bad)
            entered.set()
            if not release.wait(5):
                raise AssertionError('old request not released')
            return candidate.id

        def fail_bad(candidate, timeout, attempt):
            if candidate == bad:
                raise RouteFailure('format_exhausted')
            return 'good'

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(router.run, old_request, stage='review', prefer_other_than=good)
            try:
                self.assertTrue(entered.wait(5))
                for _ in range(2):
                    self.assertEqual(router.run(fail_bad, stage='review', prefer_other_than=good).value, 'good')
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5).value, 'bad')
        self.assertEqual(router.run(lambda candidate, *args: candidate.id, stage='review',
                                    prefer_other_than=good).value, 'good')

    def test_expiration_preference_stays_within_equally_capable_bailian_candidates(self):
        later = entry('later', provider='bailian', model='qwen-later')
        sooner = entry('sooner', provider='bailian', model='qwen-sooner')
        larger = entry('larger', provider='bailian', model='qwen-larger')
        priorities = {'later': ('same', 20), 'sooner': ('same', 10), 'larger': ('large', 1)}
        router = self.router([later, sooner, larger], priority=lambda candidate: priorities[candidate.id])
        self.assertEqual(router.run(lambda e, timeout, attempt: e.id).entry, sooner)
        self.assertEqual(router.run(lambda e, timeout, attempt: e.id, prefer_other_than=sooner).entry, later)

    def test_expiration_preference_preserves_other_provider_selection(self):
        qiniu = entry('qiniu', provider='qiniu')
        bailian = entry('bailian', provider='bailian')
        router = self.router([qiniu, bailian], priority=lambda candidate:
                             ('same', 1) if candidate == bailian else None)
        self.assertEqual(router.run(lambda e, timeout, attempt: e.id).entry, qiniu)

    def test_bailian_exhaustion_is_scoped_to_account_and_model(self):
        first = entry('first', model='qwen-a', provider='bailian')
        other = entry('other', model='qwen-b', provider='bailian')
        different_account = entry('account', model='qwen-a', key='key-b', provider='bailian')
        for unaffected in (other, different_account):
            with self.subTest(unaffected=unaffected.id):
                router = self.router([first, unaffected])
                calls = []

                def invoke(candidate, timeout, attempt):
                    calls.append(candidate.id)
                    if candidate == first:
                        raise RouteFailure('quota_exhausted', scope='model_quota', disable=True)
                    return candidate.id

                self.assertEqual(router.run(invoke).value, unaffected.id)
                self.assertEqual(router.run(invoke).value, unaffected.id)
                self.assertEqual(calls, ['first', unaffected.id, unaffected.id])

    def test_bailian_free_tier_only_error_has_exact_classification(self):
        from subtitle_tool.mt_runtime import bailian_failure
        error = bailian_failure(403, {'error': {'code': 'AllocationQuota.FreeTierOnly'}})
        self.assertEqual((error.category, error.retryable), ('quota_exhausted', False))
        self.assertEqual(bailian_failure(403, {'error': {'code': 'AccessDenied'}}).category,
                         'permission_denied')
        self.assertNotEqual(bailian_failure(403, {}).category, 'quota_exhausted')
        self.assertNotEqual(bailian_failure(429, {}).category, 'quota_exhausted')
        self.assertNotEqual(bailian_failure(429, {'error': {'code': 'AllocationQuota.FreeTierOnly'}}).category,
                            'quota_exhausted')

    def test_multiple_groups_dispatch_concurrently(self):
        a, b = entry('a', weight=3), entry('b')
        control = MTControl(threading.Event())
        router = MTRouter([a, b], paid_enabled=False, control=control)
        lock, all_entered, release = threading.Lock(), threading.Event(), threading.Event()
        seen = []

        def invoke(candidate, timeout, attempt):
            with lock:
                seen.append(candidate.id)
                if len(seen) == 4:
                    all_entered.set()
            if not release.wait(5):
                raise AssertionError('workers were not released')
            return candidate.id

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(router.run, invoke) for _ in range(4)]
            try:
                self.assertTrue(all_entered.wait(5))
            finally:
                release.set()
            self.assertEqual(len([f.result(timeout=5) for f in futures]), 4)

    def test_free_wait_budget_is_shared_across_candidates_and_retries(self):
        a, b, paid = entry('a', weight=3), entry('b'), entry('paid', 'paid')
        router = self.router([a, b, paid])
        calls = []

        def invoke(candidate, timeout, attempt):
            calls.append((candidate.id, self.clock.now, attempt))
            if candidate.id == 'a':
                raise RouteFailure('rate_limit_exceeded', retryable=True,
                                   retry_after=6 if attempt == 1 else 30)
            if candidate.id == 'b':
                raise RouteFailure('rate_limit_exceeded', retryable=True, retry_after=20)
            return 'paid result'

        result = router.run(invoke)
        self.assertEqual(calls, [('a', 0, 1), ('b', 0, 1), ('a', 6, 2), ('paid', 15, 1)])
        self.assertEqual(self.clock.waits, [6, 9])
        self.assertEqual(result.waited, 15)
        self.assertEqual(result.entry, paid)
        self.assertEqual(result.attempts, 4)

    def test_network_time_does_not_reset_or_consume_free_wait_budget(self):
        a, paid = entry('a'), entry('paid', 'paid')
        router = self.router([a, paid])

        def invoke(candidate, timeout, attempt):
            if candidate == a:
                self.clock.now += 60
                raise RouteFailure('rate_limit_exceeded', retryable=True, retry_after=100)
            return (self.clock.now, timeout)

        result = router.run(invoke)
        self.assertEqual(result.value, (75, 60))
        self.assertEqual(result.waited, 15)

    def test_preparation_counts_against_free_wait_and_limits_http_to_stage_remainder(self):
        a, b = entry('a', weight=3), entry('b')
        router = self.router([a, b], paid_enabled=False)
        prepares, calls = [], []

        @contextmanager
        def prepare(candidate, allowance):
            prepares.append((candidate.id, allowance))
            if len(prepares) == 3:
                self.clock.now += 15
            yield None

        def invoke(candidate, timeout, attempt, reservation):
            calls.append((candidate.id, timeout, attempt))
            self.clock.now += timeout
            raise RouteFailure('timeout', retryable=True, retry_after=0)

        with self.assertRaises(RouteExhausted) as error:
            router.run(invoke, prepare=prepare)
        self.assertEqual(error.exception.code, 'mt_route_deadline')
        self.assertEqual(calls, [('a', 60, 1), ('b', 60, 1), ('a', 45, 2)])
        self.assertEqual(prepares, [('a', 15), ('b', 15), ('a', 15)])
        self.assertEqual(error.exception.waited, 15)

    def test_preparation_failure_skips_free_without_charging_an_http_attempt(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid])
        calls = []

        @contextmanager
        def prepare(candidate, allowance):
            if candidate == free:
                self.clock.now += 4
                raise RouteFailure('quota_exhausted', scope='free_quota')
            yield None

        result = router.run(lambda candidate, timeout, attempt, reservation: calls.append((candidate.id, attempt)) or 'ok',
                            prepare=prepare)
        self.assertEqual(calls, [('paid', 1)])
        self.assertEqual((result.attempts, result.waited, result.free_waited), (1, 4, 4))

    def test_prepare_reservation_is_refunded_if_admission_invalidated_before_dispatch(self):
        free, paid = entry('free'), entry('paid', 'paid')
        available = {'free': True}
        router = self.router([free, paid], availability=lambda candidate: available.get(candidate.id, True))
        calls, state = [], {'reserved': 0, 'refunded': 0}

        class Reservation:
            def claim(self):
                state['reserved'] += 1
                available['free'] = False

            def mark_sent(self):
                self.sent = True

        @contextmanager
        def prepare(candidate, allowance):
            reservation = Reservation()
            reservation.sent = False
            try:
                yield reservation
            finally:
                if candidate == free and not reservation.sent:
                    state['refunded'] += 1

        def invoke(candidate, timeout, attempt, reservation):
            reservation.mark_sent()
            return calls.append(candidate.id) or 'ok'
        result = router.run(invoke, prepare=prepare)
        self.assertEqual(result.entry, paid)
        self.assertEqual(calls, ['paid'])
        self.assertEqual(state, {'reserved': 2, 'refunded': 1})

    def test_inflight_ticket_cannot_send_after_another_request_disables_same_model(self):
        candidate = entry('bailian', provider='bailian', model='qwen-model')
        first_ready, disabled = threading.Event(), threading.Event()
        sent, refunded = [], []
        router = self.router([candidate], paid_enabled=False,
                             on_event=lambda event: disabled.set() if event['kind'] == 'route_failure' else None)

        class Reservation:
            def __init__(self):
                self.sent = False
            def claim(self):
                pass
            def mark_sent(self):
                self.sent = True

        @contextmanager
        def prepare(candidate, allowance):
            reservation = Reservation()
            try:
                yield reservation
            finally:
                if not reservation.sent:
                    refunded.append(threading.current_thread().name)

        def invoke(candidate, timeout, attempt, reservation):
            thread = threading.current_thread().name
            if thread == 'first':
                first_ready.set()
                if not disabled.wait(5):
                    raise AssertionError('peer did not disable the model')
            else:
                if not first_ready.wait(5):
                    raise AssertionError('first request did not reach the dispatch gate')
            reservation.mark_sent()
            sent.append(thread)
            raise RouteFailure('quota_exhausted', scope='model_quota', disable=True)

        def run():
            try:
                router.run(invoke, prepare=prepare)
            except RouteExhausted:
                return
            raise AssertionError('both requests should exhaust their routes')

        first = threading.Thread(target=run, name='first')
        peer = threading.Thread(target=run, name='peer')
        first.start()
        self.assertTrue(first_ready.wait(5))
        peer.start()
        peer.join(5)
        first.join(5)
        self.assertFalse(peer.is_alive() or first.is_alive())
        self.assertEqual(sent, ['peer'])
        self.assertEqual(refunded, ['first'])

    def test_configured_free_wait_limit_and_validation(self):
        free, paid = entry('free'), entry('paid', 'paid')
        for limit in (0, 2.5):
            with self.subTest(limit=limit):
                self.setUp()
                router = self.router([free, paid], free_wait_seconds=limit)
                calls = []

                def invoke(candidate, timeout, attempt):
                    calls.append((candidate.id, self.clock.now))
                    if candidate == free:
                        raise RouteFailure('rate_limit_exceeded', retryable=True, retry_after=20)
                    return 'paid'

                self.assertEqual(router.run(invoke).value, 'paid')
                self.assertEqual(calls, [('free', 0), ('paid', limit)])
        for limit in (-1, 16, float('inf'), True, '15'):
            with self.subTest(invalid=limit):
                with self.assertRaises(ValueError):
                    self.router([free], free_wait_seconds=limit)

    def test_rpm_uses_the_free_wait_budget(self):
        free, paid = entry('free', rpm=3), entry('paid', 'paid')
        router = self.router([free, paid])
        for _ in range(3):
            self.assertEqual(router.run(lambda candidate, timeout, attempt: candidate.id).value, 'free')
            self.assertEqual(self.clock.now, 0)
        fourth = router.run(lambda candidate, timeout, attempt: candidate.id)
        self.assertEqual((fourth.value, fourth.free_waited, self.clock.now), ('paid', 15, 15))
        self.clock.now = 61
        self.assertEqual(router.run(lambda candidate, timeout, attempt: candidate.id).value, 'free')

    def test_rpm_window_uses_each_start_time_and_releases_oldest_at_61_seconds(self):
        free = entry('free', rpm=2)
        router = self.router([free], paid_enabled=False)
        invoke = lambda candidate, timeout, attempt: self.clock.now
        self.assertEqual(router.run(invoke).value, 0)
        self.clock.now = 20
        self.assertEqual(router.run(invoke).value, 20)
        self.clock.now = 60
        self.assertEqual(router.run(invoke).value, 61)
        self.clock.now = 70
        self.assertEqual(router.run(invoke).value, 81)

    def test_rpm_counts_pre_dispatch_reservations_across_threads(self):
        free, paid = entry('free', rpm=1), entry('paid', 'paid')
        reserved, release = threading.Event(), threading.Event()
        local = threading.local()

        def on_event(event):
            if local.name == 'first' and event['kind'] == 'route_attempt' and event['entry'] == 'free':
                reserved.set()
                if not release.wait(5):
                    raise AssertionError('reserved request was not released')

        router = self.router([free, paid], free_wait_seconds=0, on_event=on_event)

        def run(name):
            local.name = name
            return router.run(lambda candidate, timeout, attempt: candidate.id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, 'first')
            self.assertTrue(reserved.wait(5))
            try:
                self.assertEqual(pool.submit(run, 'second').result(timeout=5).entry, paid)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).entry, free)
        self.assertEqual(run('third').entry, paid)

    def test_all_caches_are_checked_before_any_network_and_paid_cache_can_be_used(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], paid_enabled=False)
        seen = []

        def cached(candidate):
            seen.append(candidate.id)
            return CacheHit(None) if candidate == paid else None

        result = router.run(lambda *args: self.fail('network called'), cache=cached)
        self.assertIn('paid', seen)
        self.assertTrue(result.cached)
        self.assertIsNone(result.value)
        self.assertEqual(result.entry, paid)

        seen.clear()
        def miss(candidate):
            seen.append(candidate.id)
        def invoke(candidate, timeout, attempt):
            self.assertEqual(set(seen), {'free', 'paid'})
            return candidate.id
        self.assertEqual(router.run(invoke, cache=miss).value, 'free')

    def test_review_prefers_a_different_backend_cache_even_in_paid_pool(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], paid_enabled=False)
        result = router.run(lambda *args: self.fail('network called'),
                            cache=lambda candidate: CacheHit(candidate.id), prefer_other_than=free)
        self.assertEqual(result.entry, paid)

    def test_paid_disabled_forbids_network_even_after_free_failure(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], paid_enabled=False)
        calls = []

        def invoke(candidate, timeout, attempt):
            calls.append(candidate.id)
            raise RouteFailure('not_found')

        with self.assertRaises(RouteExhausted):
            router.run(invoke)
        self.assertEqual(calls, ['free'])

    def test_free_quota_does_not_disable_same_account_paid(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid])
        calls = []

        def invoke(candidate, timeout, attempt):
            calls.append(candidate.id)
            if candidate == free:
                raise RouteFailure('quota_exhausted', scope='free_quota', disable=True)
            return candidate.id

        self.assertEqual(router.run(invoke).value, 'paid')
        self.assertEqual(router.run(invoke).value, 'paid')
        self.assertEqual(calls, ['free', 'paid', 'paid'])

    def test_authentication_disables_shared_credential_but_not_other_account(self):
        free = entry('free', key='shared')
        same = entry('same', 'paid', key='shared')
        other = entry('other', 'paid', key='other')
        router = self.router([free, same, other])
        calls = []

        def invoke(candidate, timeout, attempt):
            calls.append(candidate.id)
            if candidate == free:
                raise RouteFailure('authentication', scope='credential', disable=True)
            return candidate.id

        self.assertEqual(router.run(invoke).value, 'other')
        self.assertEqual(router.run(invoke).value, 'other')
        self.assertEqual(calls, ['free', 'other', 'other'])

    def test_model_and_paid_account_scopes_do_not_poison_unrelated_routes(self):
        first = entry('first', model='missing')
        same_model = entry('same_model', 'paid', model='missing', key='other')
        no_balance = entry('no_balance', 'paid', model='good')
        good = entry('good', 'paid', model='good', key='other')
        router = self.router([first, same_model, no_balance, good])
        calls = []

        def invoke(candidate, timeout, attempt):
            calls.append(candidate.id)
            if candidate == first:
                raise RouteFailure('not_found', scope='model', disable=True)
            if candidate == no_balance:
                raise RouteFailure('payment_required', scope='paid_account', disable=True)
            return candidate.id

        self.assertEqual(router.run(invoke).value, 'good')
        self.assertEqual(router.run(invoke).value, 'good')
        self.assertEqual(calls, ['first', 'no_balance', 'good', 'good'])

    def test_endpoint_cooldown_skips_all_same_endpoint_and_does_not_disable_it(self):
        free = entry('free')
        same_endpoint = entry('same', 'paid')
        elsewhere = entry('elsewhere', 'paid', url='https://b.example/v1')
        router = self.router([free, same_endpoint, elsewhere])
        calls = []

        def invoke(candidate, timeout, attempt):
            calls.append((candidate.id, self.clock.now))
            if candidate == free and len(calls) == 1:
                raise RouteFailure('network', scope='endpoint', retryable=True, retry_after=30)
            return candidate.id

        self.assertEqual(router.run(invoke).value, 'elsewhere')
        self.assertEqual(calls, [('free', 0), ('elsewhere', 15)])
        self.clock.now = 30
        self.assertEqual(router.run(invoke).value, 'free')

    def test_retry_after_is_honored_and_unknown_429_is_not_quota_exhaustion(self):
        free = entry('free')
        router = self.router([free], paid_enabled=False)
        calls = []

        def invoke(candidate, timeout, attempt):
            calls.append(self.clock.now)
            if len(calls) == 1:
                raise RouteFailure('rate_limit_exceeded', retryable=True, retry_after=3)
            return 'ok'

        self.assertEqual(router.run(invoke).value, 'ok')
        self.assertEqual(calls, [0, 3])
        self.assertEqual(router.run(invoke).value, 'ok')
        self.assertEqual(calls, [0, 3, 3])

    def test_retryable_failure_without_header_has_shared_bounded_backoff_and_recovers(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid])
        calls = []
        failed = 0

        def invoke(candidate, timeout, attempt):
            nonlocal failed
            calls.append((candidate.id, self.clock.now))
            if candidate == free and failed < 2:
                failed += 1
                raise RouteFailure('http_transient', retryable=True)
            return candidate.id

        self.assertEqual(router.run(invoke).value, 'paid')
        self.assertEqual(calls, [('free', 0), ('free', 15), ('paid', 15)])
        self.assertEqual(router.run(invoke).value, 'paid')
        self.assertEqual(calls[-1], ('paid', 30))
        self.clock.now = 45
        self.assertEqual(router.run(invoke).value, 'free')

        failed = 1
        self.assertEqual(router.run(invoke).value, 'free')
        self.assertEqual(calls[-2:], [('free', 45), ('free', 60)])

    def test_repeated_missing_header_backoff_is_capped_at_sixty_seconds(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)

        def invoke(candidate, timeout, attempt):
            if candidate == free:
                raise RouteFailure('server', retryable=True)
            return candidate.id

        for moment in (0, 15, 45, 105):
            self.clock.now = moment
            self.assertEqual(router.run(invoke).entry, paid)
        self.clock.now = 164
        self.assertEqual(router.run(lambda candidate, timeout, attempt: candidate.id).entry, paid)
        self.clock.now = 165
        self.assertEqual(router.run(lambda candidate, timeout, attempt: candidate.id).entry, free)

    def test_old_inflight_success_cannot_clear_new_shared_failure(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        entered, release = threading.Event(), threading.Event()
        local = threading.local()

        def invoke(candidate, timeout, attempt):
            if local.name == 'old' and candidate == free:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('old request was not released')
                return 'old result'
            if local.name == 'failure' and candidate == free:
                raise RouteFailure('server', retryable=True)
            return candidate.id

        def run(name):
            local.name = name
            return router.run(invoke)

        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(run, 'old')
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(pool.submit(run, 'failure').result(timeout=5).entry, paid)
            finally:
                release.set()
            self.assertEqual(old.result(timeout=5).value, 'old result')
        self.assertEqual(run('after_failure').entry, paid)
        self.clock.now = 15
        self.assertEqual(run('recovery').entry, free)

    def test_recovery_allows_only_one_probe_until_it_finishes(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        probe_entered, release = threading.Event(), threading.Event()
        self.assertEqual(router.run(lambda candidate, timeout, attempt: (
            (_ for _ in ()).throw(RouteFailure('network', retryable=True)) if candidate == free else 'paid')).entry, paid)
        self.clock.now = 15

        def probe(candidate, timeout, attempt):
            probe_entered.set()
            if not release.wait(5):
                raise AssertionError('probe was not released')
            return candidate.id

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(router.run, probe)
            self.assertTrue(probe_entered.wait(5))
            try:
                second = pool.submit(router.run, lambda candidate, timeout, attempt: candidate.id).result(timeout=5)
                self.assertEqual(second.entry, paid)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).entry, free)
        self.assertEqual(router.run(lambda candidate, timeout, attempt: candidate.id).entry, free)

    def test_late_retry_after_invalidates_an_already_started_probe(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        old_entered, release_old = threading.Event(), threading.Event()
        probe_entered, release_probe = threading.Event(), threading.Event()
        local = threading.local()

        def invoke(candidate, timeout, attempt):
            if candidate != free:
                return candidate.id
            if local.name == 'old':
                old_entered.set()
                if not release_old.wait(5):
                    raise AssertionError('old request was not released')
                raise RouteFailure('server', retryable=True, retry_after=10)
            if local.name == 'initial_failure':
                raise RouteFailure('server', retryable=True, retry_after=1)
            if local.name == 'probe':
                probe_entered.set()
                if not release_probe.wait(5):
                    raise AssertionError('probe was not released')
            return candidate.id

        def run(name):
            local.name = name
            return router.run(invoke)

        with ThreadPoolExecutor(max_workers=3) as pool:
            old = pool.submit(run, 'old')
            self.assertTrue(old_entered.wait(5))
            self.assertEqual(pool.submit(run, 'initial_failure').result(timeout=5).entry, paid)
            self.clock.now = 1
            probe = pool.submit(run, 'probe')
            self.assertTrue(probe_entered.wait(5))
            try:
                release_old.set()
                self.assertEqual(old.result(timeout=5).entry, paid)
            finally:
                release_old.set()
                release_probe.set()
            self.assertEqual(probe.result(timeout=5).entry, free)
        self.assertEqual(run('after_late_failure').entry, paid)
        self.clock.now = 11
        self.assertEqual(run('recovered').entry, free)

    def test_activity_boundaries_exclude_network_but_do_not_discard_cache(self):
        free = entry('free', model=QINIU_FREE_MODEL, url=QINIU_BASE_URL, provider='qiniu')
        paid = entry('paid', 'paid')
        for instant, expected in [
            (datetime(2026, 10, 23, 15, 59, 59, tzinfo=timezone.utc), 'paid'),
            (datetime(2026, 10, 23, 16, 0, tzinfo=timezone.utc), 'free'),
            (datetime(2026, 11, 30, 15, 59, 59, tzinfo=timezone.utc), 'free'),
            (datetime(2026, 11, 30, 16, 0, tzinfo=timezone.utc), 'paid'),
        ]:
            with self.subTest(instant=instant):
                self.clock.utc = instant
                result = self.router([free, paid]).run(lambda candidate, timeout, attempt: candidate.id)
                self.assertEqual(result.value, expected)
        result = self.router([free, paid]).run(lambda *args: self.fail('network called'),
                                               cache=lambda candidate: CacheHit('cached') if candidate == free else None)
        self.assertEqual((result.value, result.entry), ('cached', free))

    def test_max_two_attempts_per_backend_and_stage_deadline_includes_network(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid])
        calls = []

        def invoke(candidate, timeout, attempt):
            calls.append((candidate.id, timeout, attempt))
            self.clock.now += 60 if candidate == free else (40 if attempt == 1 else 20)
            raise RouteFailure('timeout', retryable=True, retry_after=0)

        with self.assertRaises(RouteExhausted) as error:
            router.run(invoke)
        self.assertEqual(calls, [('free', 60, 1), ('free', 60, 2), ('paid', 60, 1), ('paid', 20, 2)])
        self.assertEqual(error.exception.code, 'mt_route_deadline')
        self.assertEqual(self.clock.now, 180)

    def test_caller_can_pass_remaining_stage_deadline_without_extending_limit(self):
        free = entry('free')
        router = self.router([free], paid_enabled=False)
        calls = []

        def invoke(candidate, timeout, attempt):
            calls.append(timeout)
            self.clock.now += timeout
            raise RouteFailure('timeout', retryable=True, retry_after=0)

        with self.assertRaises(RouteExhausted) as error:
            router.run(invoke, deadline_seconds=2)
        self.assertEqual((error.exception.code, calls), ('mt_route_deadline', [2]))
        calls.clear()
        with self.assertRaises(RouteExhausted) as error:
            router.run(invoke, deadline_seconds=0)
        self.assertEqual((error.exception.code, calls), ('mt_route_deadline', []))
        for invalid in (-1, 181, float('inf'), True, '1'):
            with self.subTest(deadline=invalid), self.assertRaises(ValueError):
                router.run(invoke, deadline_seconds=invalid)

    def test_request_timeout_is_recomputed_after_slow_availability_check(self):
        free = entry('free')

        def available(candidate):
            self.clock.now = 175
            return True

        router = self.router([free], availability=available)
        self.assertEqual(router.run(lambda candidate, timeout, attempt: timeout).value, 5)

    def test_expired_deadline_during_availability_check_never_starts_network(self):
        free = entry('free')
        calls = []

        def available(candidate):
            self.clock.now = 180
            return True

        router = self.router([free], availability=available)
        with self.assertRaises(RouteExhausted) as error:
            router.run(lambda *args: calls.append(args))
        self.assertEqual(error.exception.code, 'mt_route_deadline')
        self.assertEqual(calls, [])

    def test_cancel_prevents_new_requests_and_allows_only_timely_inflight_result(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid])
        calls = []

        def fail_after_cancel(candidate, timeout, attempt):
            calls.append(candidate.id)
            self.control.cancel_user()
            raise RouteFailure('network', retryable=True)

        with self.assertRaises(Cancelled):
            router.run(fail_after_cancel)
        self.assertEqual(calls, ['free'])

        self.setUp()
        router = self.router([free, paid])
        def finish_in_drain(candidate, timeout, attempt):
            self.control.cancel_user()
            self.clock.now += 4.9
            return 'saved result'
        self.assertEqual(router.run(finish_in_drain).value, 'saved result')

        self.setUp()
        router = self.router([free, paid])
        def too_late(candidate, timeout, attempt):
            self.control.cancel_user()
            self.clock.now += 5
            return 'discarded'
        with self.assertRaises(Cancelled):
            router.run(too_late)

    def test_cancellation_immediately_before_dispatch_never_invokes_network(self):
        free = entry('free')
        calls = []

        def on_event(event):
            if event['kind'] == 'route_attempt':
                self.control.cancel_user()

        router = self.router([free], on_event=on_event)
        with self.assertRaises(Cancelled):
            router.run(lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_cancellation_during_free_wait_does_not_start_paid_fallback(self):
        free, paid = entry('free'), entry('paid', 'paid')
        calls = []

        def cancel_wait(control, seconds):
            control.cancel_user()
            control.check_start()

        router = MTRouter([free, paid], paid_enabled=True, control=self.control, clock=self.clock,
                          utcnow=lambda: self.clock.utc, wait=cancel_wait)

        def invoke(candidate, timeout, attempt):
            calls.append(candidate.id)
            raise RouteFailure('rate_limit_exceeded', retryable=True, retry_after=100)

        with self.assertRaises(Cancelled):
            router.run(invoke)
        self.assertEqual(calls, ['free'])

    def test_expired_activity_is_rechecked_immediately_before_dispatch(self):
        free = entry('free', model=QINIU_FREE_MODEL, url=QINIU_BASE_URL, provider='qiniu')
        paid = entry('paid', 'paid')
        self.clock.utc = datetime(2026, 11, 30, 15, 59, 59, tzinfo=timezone.utc)
        calls = []

        def on_event(event):
            if event['kind'] == 'route_attempt' and event['entry'] == 'free':
                self.clock.utc = datetime(2026, 11, 30, 16, 0, tzinfo=timezone.utc)

        router = self.router([free, paid], on_event=on_event)
        self.assertEqual(router.run(lambda candidate, timeout, attempt: calls.append(candidate.id) or 'ok').entry, paid)
        self.assertEqual(calls, ['paid'])

    def test_cooldown_from_peer_is_rechecked_before_network_start(self):
        free = entry('free')
        paid = entry('paid', 'paid', url='https://b.example/v1')
        reserved, release = threading.Event(), threading.Event()
        local = threading.local()
        calls = []
        calls_lock = threading.Lock()

        def on_event(event):
            if local.name == 'first' and event['kind'] == 'route_attempt' and event['entry'] == 'free':
                reserved.set()
                if not release.wait(5):
                    raise AssertionError('peer did not respond')
            if local.name == 'second' and event['kind'] == 'route_failure':
                release.set()

        router = self.router([free, paid], on_event=on_event)

        def invoke(candidate, timeout, attempt):
            with calls_lock:
                calls.append((local.name, candidate.id))
            if local.name == 'second' and candidate == free:
                raise RouteFailure('network', scope='endpoint', retry_after=30)
            return candidate.id

        def run(name):
            local.name = name
            return router.run(invoke)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, 'first')
            self.assertTrue(reserved.wait(5))
            second = pool.submit(run, 'second')
            try:
                self.assertEqual(first.result(timeout=5).entry, paid)
                self.assertEqual(second.result(timeout=5).entry, paid)
            finally:
                release.set()
        self.assertNotIn(('first', 'free'), calls)

    def test_event_and_failure_strings_never_expose_callback_text(self):
        free = entry('free')
        events = []
        router = self.router([free], paid_enabled=False, on_event=events.append)

        def invoke(candidate, timeout, attempt):
            raise RouteFailure('secret raw text', scope='entry')

        with self.assertRaises(RouteExhausted) as error:
            router.run(invoke)
        self.assertNotIn('secret raw text', repr(events))
        self.assertNotIn('secret raw text', str(error.exception))


if __name__ == '__main__':
    unittest.main()
