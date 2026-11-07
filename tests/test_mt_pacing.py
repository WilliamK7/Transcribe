"""Offline admission and dispatch regressions after observed throttling."""
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

from subtitle_tool.common import Cancelled
from subtitle_tool.mt_routing import RouteExhausted, RouteFailure
from tests.mt_fixtures import RouterFixture, entry


class PacingTests(RouterFixture):
    def throttle(self, router, target):
        def invoke(candidate, *args):
            if candidate == target:
                raise RouteFailure('rate_limit_exceeded', retryable=True, retry_after=0)
            return candidate.id
        router.run(invoke)

    def test_successful_probe_keeps_spacing_instead_of_immediately_releasing_a_burst(self):
        free = entry('free')
        router = self.router([free], paid_enabled=False)
        starts = []
        def invoke(candidate, *args):
            starts.append(self.clock.now)
            if len(starts) == 1:
                raise RouteFailure('rate_limit_exceeded', retryable=True, retry_after=3)
            return candidate.id
        router.run(invoke)
        for _ in range(4):
            router.run(invoke)
        self.assertEqual(starts[:2], [0, 3])
        self.assertTrue(all(b - a >= 1 for a, b in zip(starts[1:], starts[2:])))

    def test_unknown_429_can_slow_dispatch_without_becoming_quota_exhaustion(self):
        free = entry('free')
        router = self.router([free], paid_enabled=False)
        calls = []
        def invoke(candidate, *args):
            calls.append(self.clock.now)
            if len(calls) == 1:
                raise RouteFailure('http_transient', retryable=True, retry_after=0, rate_limited=True)
            return candidate.id
        self.assertEqual(router.run(invoke).value, 'free')
        self.assertEqual(calls, [0, 1])
        failures = [event for event in self.events if event['kind'] == 'route_failure']
        self.assertEqual(failures[0]['category'], 'http_transient')

    def test_limit_reduces_after_throttling_and_other_backends_remain_available(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        self.throttle(router, free)
        self.clock.now = 1
        self.assertEqual(router.run(lambda candidate, *args: candidate.id).entry, free)
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        def blocked(index):
            def invoke(candidate, *args):
                self.assertEqual(candidate, free)
                entered[index].set()
                self.assertTrue(release.wait(5))
                return candidate.id
            return router.run(invoke)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.clock.now = 2
            first = pool.submit(blocked, 0)
            try:
                self.assertTrue(entered[0].wait(5))
                self.clock.now = 3
                second = pool.submit(blocked, 1)
                self.assertTrue(entered[1].wait(5))
                self.clock.now = 4
                self.assertEqual(router.run(lambda candidate, *args: candidate.id).entry, paid)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).entry, free)
            self.assertEqual(second.result(timeout=5).entry, free)

    def test_stable_valid_results_gradually_restore_the_dispatch_rate(self):
        free = entry('free')
        router = self.router([free], paid_enabled=False)
        attempts = 0
        def invoke(candidate, *args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RouteFailure('rate_limit_exceeded', retryable=True, retry_after=0)
            return candidate.id
        router.run(invoke)
        for _ in range(7):
            router.run(invoke)
        changes = [event for event in self.events if event['kind'] == 'route_pacing']
        self.assertEqual([(row['concurrency'], row['interval_ms']) for row in changes], [(2, 1000), (3, 500)])
        for _ in range(24):
            router.run(invoke)
        changes = [event for event in self.events if event['kind'] == 'route_pacing']
        self.assertEqual((changes[-1]['concurrency'], changes[-1]['interval_ms']), (4, 0))

    def test_failed_preparation_and_cache_hits_do_not_restore_rate_or_consume_spacing(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        self.throttle(router, free)
        self.clock.now = 1
        for _ in range(16):
            router.run(lambda *args: self.fail('unexpected network'), cache=lambda candidate: 'saved')
        @contextmanager
        def denied(candidate, allowance):
            if candidate == free:
                raise RouteFailure('quota_unknown')
            yield None
        for _ in range(8):
            self.assertEqual(router.run(lambda candidate, *args: candidate.id, prepare=denied).entry, paid)
        self.assertEqual(router.run(lambda candidate, *args: candidate.id).entry, free)
        self.assertEqual(self.clock.now, 1)
        changes = [event for event in self.events if event['kind'] == 'route_pacing']
        self.assertEqual(len(changes), 1)

    def test_spacing_counts_against_free_wait_budget_and_respects_paid_denial(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        self.throttle(router, free)
        router.free_wait_seconds = .25
        result = router.run(lambda candidate, *args: candidate.id)
        self.assertEqual(result.entry, paid)
        self.assertAlmostEqual(result.free_waited, .25)
        router.paid_enabled = False
        with self.assertRaises(RouteExhausted) as error:
            router.run(lambda *args: self.fail('not yet ready'))
        self.assertAlmostEqual(error.exception.waited, .25)

    def test_spacing_is_measured_from_actual_dispatch_after_preparation(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        self.throttle(router, free)
        router.paid_enabled = False
        router.free_wait_seconds = 15
        self.clock.now = 1
        @contextmanager
        def prepare(candidate, allowance):
            self.clock.now += 4
            yield SimpleNamespace(claim=lambda: None, mark_sent=lambda: None)
        sent = []
        def invoke(candidate, timeout, attempt, reservation):
            reservation.mark_sent()
            sent.append(self.clock.now)
            return candidate.id
        router.run(invoke, prepare=prepare)
        router.run(lambda candidate, *args: sent.append(self.clock.now) or candidate.id)
        self.assertEqual(sent, [5, 6])

    def test_non_throttling_errors_do_not_change_healthy_dispatch_rate(self):
        free = entry('free')
        router = self.router([free], paid_enabled=False)
        calls = []
        def invoke(candidate, *args):
            calls.append(self.clock.now)
            if len(calls) == 1:
                raise RouteFailure('server', retryable=True, retry_after=3)
            return candidate.id
        router.run(invoke)
        router.run(invoke)
        self.assertEqual(calls, [0, 3, 3])
        self.assertFalse(any(event['kind'] == 'route_pacing' for event in self.events))

    def test_cancellation_during_pacing_stops_before_dispatch(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        self.throttle(router, free)
        router.free_wait_seconds = 15
        def wait(control, seconds):
            control.cancel_user()
            control.check_start()
        router.wait = wait
        with self.assertRaises(Cancelled):
            router.run(lambda *args: self.fail('cancelled before dispatch'))

    def test_late_response_from_before_throttling_does_not_accelerate_recovery(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        entered, release = threading.Event(), threading.Event()
        def old(candidate, *args):
            self.assertEqual(candidate, free)
            entered.set()
            self.assertTrue(release.wait(5))
            return candidate.id
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(router.run, old)
            try:
                self.assertTrue(entered.wait(5))
                self.throttle(router, free)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).entry, free)
        router.paid_enabled, router.free_wait_seconds = False, 15
        for _ in range(7):
            router.run(lambda candidate, *args: candidate.id)
        self.assertEqual(sum(event['kind'] == 'route_pacing' for event in self.events), 1)
        router.run(lambda candidate, *args: candidate.id)
        self.assertEqual(sum(event['kind'] == 'route_pacing' for event in self.events), 2)

    def test_pending_dispatch_is_invalidated_and_refunded_when_peer_is_throttled(self):
        free, paid = entry('free'), entry('paid', 'paid')
        rejected = threading.Event()
        def event(value):
            self.events.append(value)
            if value['kind'] == 'route_failure' and value['category'] == 'rate_limit_exceeded':
                rejected.set()
        router = self.router([free, paid], free_wait_seconds=0, on_event=event)
        ready, sent, refunded = threading.Event(), [], []
        @contextmanager
        def prepare(candidate, allowance):
            state = {'sent': False}
            def mark():
                state['sent'] = True
            try:
                yield SimpleNamespace(claim=lambda: None, mark_sent=mark)
            finally:
                if not state['sent']:
                    refunded.append(candidate.id)
        def delayed(candidate, timeout, attempt, reservation):
            if candidate == free:
                ready.set()
                self.assertTrue(rejected.wait(5))
            reservation.mark_sent()
            sent.append(candidate.id)
            return candidate.id
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(router.run, delayed, prepare=prepare)
            try:
                self.assertTrue(ready.wait(5))
                self.throttle(router, free)
            finally:
                rejected.set()
            result = pending.result(timeout=5)
        self.assertEqual(result.entry, paid)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(sent, ['paid'])
        self.assertEqual(refunded, ['free'])

    def test_unpaced_dispatch_cannot_overtake_a_late_paced_preparation(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        self.throttle(router, free)
        self.clock.now = 1
        router.run(lambda candidate, *args: candidate.id)  # Complete the recovery probe.
        self.clock.now = 2
        ready, release = threading.Event(), threading.Event()
        @contextmanager
        def prepare(candidate, allowance):
            if candidate == free:
                ready.set()
                self.assertTrue(release.wait(5))
            # The peer advances the fake clock; no quota query waited here.
            yield SimpleNamespace(claim=lambda: None, mark_sent=lambda: None, preparation_waited=0)
        def invoke(candidate, timeout, attempt, reservation):
            reservation.mark_sent()
            return candidate.id
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(router.run, invoke, prepare=prepare)
            try:
                self.assertTrue(ready.wait(5))
                self.clock.now = 5
                self.assertEqual(router.run(lambda candidate, *args: candidate.id).entry, paid)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).entry, free)
        self.assertEqual(router.run(lambda candidate, *args: candidate.id).entry, paid)
        self.clock.now = 6
        self.assertEqual(router.run(lambda candidate, *args: candidate.id).entry, free)

    def test_repeated_throttling_is_bounded_and_new_run_starts_without_old_limits(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], free_wait_seconds=0)
        for _ in range(8):
            self.throttle(router, free)
            self.clock.now += 30
        changes = [event for event in self.events if event['kind'] == 'route_pacing']
        self.assertEqual((changes[-1]['concurrency'], changes[-1]['interval_ms']), (1, 15000))
        self.assertEqual(len(changes), 8)
        fresh = self.router([free], paid_enabled=False)
        moment = self.clock.now
        for _ in range(4):
            fresh.run(lambda candidate, *args: candidate.id)
        self.assertEqual(self.clock.now, moment)
