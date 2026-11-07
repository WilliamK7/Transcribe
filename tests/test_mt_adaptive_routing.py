"""Deterministic clocks and synthetic stages; no network or real credentials."""
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

from subtitle_tool.common import Cancelled
from subtitle_tool.mt_routing import RouteExhausted, RouteFailure
from tests.mt_fixtures import RouterFixture, entry


class AdaptiveRoutingTests(RouterFixture):
    def timed(self, durations):
        def invoke(candidate, *args):
            self.clock.now += durations[candidate.id]
            return candidate.id
        return invoke

    def train(self, router, first, second, durations, **kwargs):
        for candidate, other in ((first, second), (second, first)):
            for _ in range(3):
                self.assertEqual(router.run(self.timed(durations), prefer_other_than=other.id,
                                            stage='draft', **kwargs).entry, candidate)

    def test_learns_valid_completion_latency_instead_of_static_weight_alone(self):
        slow, fast = entry('slow', weight=3), entry('fast')
        router = self.router([slow, fast])
        durations = {'slow': 20, 'fast': 2}
        self.train(router, slow, fast, durations)
        started = self.clock.now
        selected = [router.run(self.timed(durations), stage='draft').value for _ in range(32)]
        self.assertGreaterEqual(selected.count('fast'), 28)
        self.assertLess(self.clock.now - started, 150)

    def test_intermittent_invalid_responses_lower_ranking_without_consecutive_failures(self):
        flaky, good = entry('flaky'), entry('good')
        router = self.router([flaky, good])
        for failed in [True, False] * 4:
            def invoke(candidate, *args):
                self.clock.now += 1
                if candidate == flaky and failed:
                    raise RouteFailure('format_exhausted')
                return candidate.id
            router.run(invoke, stage='review', prefer_other_than=good.id)
        selected = [router.run(self.timed({'flaky': 1, 'good': 1}), stage='review').value
                    for _ in range(16)]
        self.assertGreaterEqual(selected.count('good'), 14)

    def test_learning_is_scoped_to_stage_and_output_size(self):
        slow, fast = entry('slow', weight=3), entry('fast')
        router = self.router([slow, fast])
        self.train(router, slow, fast, {'slow': 10, 'fast': 1}, output_size=3000)
        invoke = self.timed({'slow': 1, 'fast': 1})
        self.assertEqual(router.run(invoke, stage='draft', output_size=3000).entry, fast)
        self.assertEqual(router.run(invoke, stage='review', output_size=3000).entry, slow)
        self.assertEqual(router.run(invoke, stage='draft', output_size=100).entry, slow)
        self.assertEqual(router.run(invoke, stage='draft', output_size=3000,
                                    prefer_other_than=fast.id).entry, slow)

    def test_learning_keeps_free_first_paid_permission_and_exact_cache_priority(self):
        free, paid = entry('free'), entry('paid', 'paid')
        router = self.router([free, paid], paid_enabled=False)
        invoke = self.timed({'free': 30, 'paid': 1})
        for _ in range(4):
            self.assertEqual(router.run(invoke, stage='review').entry, free)
        self.assertEqual(router.run(lambda *args: self.fail('unexpected network'), stage='review',
                                    cache=lambda candidate: 'saved' if candidate == paid else None).value, 'saved')
        router.paid_enabled = True
        self.assertEqual(router.run(invoke, stage='review').entry, free)

    def test_slow_candidate_is_probed_again_without_extra_requests(self):
        slow, fast = entry('slow', weight=3), entry('fast')
        router = self.router([slow, fast])
        self.train(router, slow, fast, {'slow': 10, 'fast': 1})
        self.assertEqual(router.run(self.timed({'slow': 10, 'fast': 1}), stage='draft').entry, fast)
        self.clock.now += 61
        self.assertEqual(router.run(self.timed({'slow': 1, 'fast': 1}), stage='draft').entry, slow)
        attempts = [event for event in self.events if event['kind'] == 'route_attempt']
        self.assertEqual(len(attempts), 8)
        self.assertEqual(attempts[-1]['selection'], 'probe')

    def test_prediction_can_use_a_busy_fast_backend_before_an_idle_slow_one(self):
        slow, fast = entry('slow'), entry('fast')
        router = self.router([slow, fast])
        self.train(router, slow, fast, {'slow': 8, 'fast': 1})
        entered, release = threading.Event(), threading.Event()
        def blocked(candidate, *args):
            self.assertEqual(candidate, fast)
            entered.set()
            self.assertTrue(release.wait(5))
            return candidate.id
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(router.run, blocked, stage='draft')
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(router.run(self.timed({'slow': 8, 'fast': 1}), stage='draft').entry, fast)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).entry, fast)

    def test_cached_responses_do_not_teach_network_latency_and_new_runs_start_fresh(self):
        first, second = entry('first', weight=3), entry('second')
        router = self.router([first, second])
        router.run(lambda *args: self.fail('unexpected network'), stage='draft',
                   cache=lambda candidate: 'saved' if candidate == second else None)
        self.assertEqual(router.run(self.timed({'first': 10, 'second': 1}), stage='draft').entry, first)
        self.assertEqual(self.events[-1]['selection'], 'weighted')
        self.train(router, first, second, {'first': 10, 'second': 1})
        self.assertEqual(router.run(self.timed({'first': 10, 'second': 1}), stage='draft').entry, second)
        fresh = self.router([first, second])
        self.assertEqual(fresh.run(self.timed({'first': 10, 'second': 1}), stage='draft').entry, first)

    def test_truncation_does_not_poison_smaller_requests_or_other_stages(self):
        limited, good = entry('limited', weight=3), entry('good')
        router = self.router([limited, good])
        calls = []
        def truncate(candidate, *args):
            calls.append(candidate.id)
            if candidate == limited:
                raise RouteFailure('output_truncated')
            return candidate.id
        self.assertEqual(router.run(truncate, stage='review', output_size=2000).entry, good)
        invoke = lambda candidate, *args: candidate.id
        for size in [2000, 6000]:
            self.assertEqual(router.run(invoke, stage='review', output_size=size,
                                        prefer_other_than=good.id).entry, good)
        self.assertEqual(router.run(invoke, stage='review', output_size=100,
                                    prefer_other_than=good.id).entry, limited)
        self.assertEqual(router.run(invoke, stage='draft', output_size=6000,
                                    prefer_other_than=good.id).entry, limited)
        self.assertEqual(router.run(invoke, stage='review', output_size=2000,
                                    prefer_other_than=good.id).entry, good)
        self.assertEqual(calls, ['limited', 'good'])
        self.assertTrue(router.run(lambda *args: self.fail('unexpected network'), stage='review', output_size=2000,
                                   cache=lambda candidate: 'saved' if candidate == limited else None).cached)

    def test_truncated_free_route_remains_a_fallback_and_recovery_probe_is_exclusive(self):
        limited, good, paid = entry('limited', weight=3), entry('good'), entry('paid', 'paid')
        router = self.router([limited, good, paid])
        def truncate(candidate, *args):
            if candidate == limited:
                raise RouteFailure('output_truncated')
            return candidate.id
        router.run(truncate, stage='draft', output_size=2000)
        self.clock.now += 301
        entered, release = threading.Event(), threading.Event()
        def probe(candidate, *args):
            self.assertEqual(candidate, limited)
            entered.set()
            self.assertTrue(release.wait(5))
            return candidate.id
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(router.run, probe, stage='draft', output_size=2000,
                                  prefer_other_than=good.id)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(router.run(lambda candidate, *args: candidate.id, stage='draft', output_size=6000,
                                            prefer_other_than=good.id).entry, good)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).entry, limited)
        self.assertEqual(router.run(lambda candidate, *args: candidate.id, stage='draft', output_size=2000,
                                    prefer_other_than=good.id).entry, limited)

        single = self.router([limited, paid], paid_enabled=False)
        for _ in range(3):
            with self.assertRaises(RouteExhausted):
                single.run(truncate, stage='draft', output_size=2000)

    def test_quota_preparation_and_cache_time_are_not_network_latency_samples(self):
        first, second = entry('first'), entry('second')
        router = self.router([first, second])
        @contextmanager
        def prepare(candidate, allowance):
            self.clock.now += 10
            yield SimpleNamespace(claim=lambda: None, mark_sent=lambda: None)
        def invoke(candidate, timeout, attempt, reservation):
            reservation.mark_sent()
            self.clock.now += 1
            return candidate.id
        router.run(invoke, stage='draft', prepare=prepare)
        router.run(self.timed({'first': 1, 'second': 2}), stage='draft', prefer_other_than=first.id)
        self.assertEqual(router.run(self.timed({'first': 1, 'second': 2}), stage='draft').entry, first)

    def test_late_success_cannot_clear_a_new_truncation_observation(self):
        limited, good = entry('limited', weight=3), entry('good')
        router = self.router([limited, good])
        entered, release = threading.Event(), threading.Event()
        def old_reply(candidate, *args):
            entered.set()
            self.assertTrue(release.wait(5))
            return candidate.id
        def truncate(candidate, *args):
            if candidate == limited:
                raise RouteFailure('output_truncated')
            return candidate.id
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(router.run, old_reply, stage='review', output_size=6000,
                                  prefer_other_than=good.id)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(router.run(truncate, stage='review', output_size=2000,
                                            prefer_other_than=good.id).entry, good)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).entry, limited)
        self.assertEqual(router.run(lambda candidate, *args: candidate.id, stage='review', output_size=6000,
                                    prefer_other_than=good.id).entry, good)

    def test_invalid_workload_hint_is_rejected_before_dispatch(self):
        router = self.router([entry('free')])
        for value in [-1, True, 1.5, '100', None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                router.run(lambda *args: self.fail('unexpected network'), stage='draft', output_size=value)

    def test_cancelled_or_unsent_requests_are_not_valid_completion_samples(self):
        first, second = entry('first'), entry('second')
        router = self.router([first, second])
        @contextmanager
        def prepare(candidate, allowance):
            raise RouteFailure('quota_unknown')
            yield
        with self.assertRaises(RouteExhausted):
            router.run(lambda *args: self.fail('not admitted'), stage='draft', prepare=prepare)
        self.assertFalse(any(event.get('selection') == 'performance' for event in self.events))
        def cancel(candidate, *args):
            self.control.cancel_user()
            raise Cancelled()
        with self.assertRaises(Cancelled):
            router.run(cancel, stage='review')
        self.control.cancel.clear()
        self.control.begin()
        self.events.clear()
        router.run(self.timed({'first': 1, 'second': 1}), stage='review')
        self.assertEqual(self.events[-1]['selection'], 'weighted')
