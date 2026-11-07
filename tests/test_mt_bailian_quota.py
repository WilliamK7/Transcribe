"""Offline regressions for guarded Bailian free-tier admission."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from subtitle_tool.common import Cancelled
from subtitle_tool.mt_backend import MTBackend
from subtitle_tool.mt_bailian_quota import (BailianQuota, CLIQuotaSource, QuotaSourceError,
                                            VerifiedIdentity, estimate_tokens, parse_observation)
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_diagnostics import MTDiagnostics
from subtitle_tool.mt_routing import MTRouter, RouteEntry, RouteFailure


class Clock:
    def __init__(self):
        self.now = 100.0
        self.today = date(2026, 11, 2)

    def __call__(self):
        return self.now


def entry(model='qwen-test', *, name='first', fingerprint='a' * 64, workspace='ws-test', tier='free'):
    backend = MTBackend('bailian', f'https://{workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
                        model, fingerprint, 5)
    return RouteEntry(name, backend, tier)


def row(model='qwen-test', remaining=300, **changes):
    result = {'model': model, 'type': 'Text', 'remaining': remaining, 'total': 300,
              'remainingPercent': 100.0, 'expires': '2026-11-04', 'autoStop': True}
    result.update(changes)
    return result


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.registry = mock.patch.dict('subtitle_tool.mt_bailian_quota._ACCOUNTS', {}, clear=True)
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.clock = Clock()
        self.control = MTControl(threading.Event(), clock=self.clock)
        self.source = mock.Mock()
        self.source.bound_to.return_value = True
        self.rows = [row()]
        self.source.query.side_effect = lambda **kwargs: json.dumps(self.rows).encode()
        self.proof = VerifiedIdentity('account-a', 'ws-test', 'a' * 64, 'cn-beijing', 'c' * 64)
        self.quota = BailianQuota(self.control, identity=lambda route: self.proof,
                                  protection=lambda route, observation: True, source=self.source,
                                  clock=self.clock, today=lambda: self.clock.today)
        self.route = entry()

    def send(self, route=None, estimate=100, usage=None):
        with self.quota.reserve(route or self.route, 5, estimate=estimate) as reservation:
            reservation.claim()
            reservation.mark_sent()
            if usage is not None:
                reservation.settle(usage)

    def failure(self, quota=None, route=None, estimate=100):
        with self.assertRaises(RouteFailure) as error:
            with (quota or self.quota).reserve(route or self.route, 5, estimate=estimate) as reservation:
                reservation.claim()
        return error.exception.category

    def test_identity_absent_or_mismatched_fails_closed_without_query(self):
        unknown = BailianQuota(self.control, source=self.source, clock=self.clock, today=lambda: self.clock.today)
        self.assertEqual(self.failure(unknown), 'quota_unknown')
        for proof in (VerifiedIdentity('account-a', 'another-workspace', 'a' * 64, 'cn-beijing', 'c' * 64),
                      VerifiedIdentity('account-a', 'ws-test', 'b' * 64, 'cn-beijing', 'c' * 64),
                      VerifiedIdentity('account-a', 'ws-test', 'a' * 64, 'cn-shanghai', 'c' * 64),
                      VerifiedIdentity('account-a', 'ws-test', 'a' * 64, 'cn-beijing', '')):
            self.proof = proof
            self.assertEqual(self.failure(), 'quota_unknown')
        self.source.query.assert_not_called()

    def test_only_exact_text_model_with_valid_positive_quota_and_active_protection(self):
        invalid = [
            row(autoStop=False), row(autoStop=None), row(autoStop='unsupported'),
            row(type='Audio'), row(type=None), row(remaining=True), row(remaining=-1),
            row(remaining=301), row(total=0), row(expires=None), row(expires='2026-11-02'),
            row(expires='2026-11-01'), row(expires='2026-11-32'), row(expires='2026-11-04T00:00:00'),
            row(remainingPercent=None), row(model='other-model'),
        ]
        for item in invalid:
            with self.subTest(item=item):
                with mock.patch.dict('subtitle_tool.mt_bailian_quota._ACCOUNTS', {}, clear=True):
                    self.rows = [item]
                    self.assertEqual(self.failure(), 'quota_unknown')
        with mock.patch.dict('subtitle_tool.mt_bailian_quota._ACCOUNTS', {}, clear=True):
            self.rows = [row(), row()]
            self.assertEqual(self.failure(), 'quota_unknown')
        with mock.patch.dict('subtitle_tool.mt_bailian_quota._ACCOUNTS', {}, clear=True):
            self.rows = [row(remaining=0)]
            self.assertEqual(self.failure(), 'quota_exhausted')

    def test_reported_auto_stop_is_not_proof_that_protection_has_taken_effect(self):
        unknown = BailianQuota(self.control, identity=lambda route: self.proof, source=self.source,
                                clock=self.clock, today=lambda: self.clock.today)
        self.assertEqual(self.failure(unknown), 'quota_unknown')
        self.assertIsNone(unknown.priority(self.route))
        unknown.protection = lambda route, observation: False
        self.assertEqual(self.failure(unknown), 'quota_unknown')
        unknown.protection = lambda route, observation: 1  # bool, not truthy guesses
        self.assertEqual(self.failure(unknown), 'quota_unknown')

    def test_quota_query_must_bind_the_same_verified_console_context(self):
        self.source.bound_to.return_value = False
        self.assertEqual(self.failure(), 'quota_unknown')
        self.source.query.assert_not_called()
        self.assertFalse(CLIQuotaSource().bound_to(self.proof))
        self.source.bound_to.return_value = True
        self.send()
        self.assertEqual(self.source.query.call_args.kwargs['proof'], self.proof)

    def test_potential_is_nonblocking_and_never_treats_default_cli_as_verified(self):
        self.assertTrue(self.quota.potential(self.route))
        self.source.query.assert_not_called()
        self.source.bound_to.return_value = False
        self.assertFalse(self.quota.potential(self.route))
        self.assertFalse(BailianQuota(self.control, identity=lambda route: self.proof,
                                      protection=lambda route, row: True).potential(self.route))
        self.assertFalse(BailianQuota(self.control).potential(self.route))
        self.assertTrue(self.quota.potential(entry(tier='paid')))

    def test_token_claim_refund_settlement_unknown_and_overrun(self):
        with self.quota.reserve(self.route, 5, estimate=100) as reservation:
            reservation.claim()  # cancelled before HTTP: return all
        self.send(usage={'input_tokens': 30, 'output_tokens': 20})  # charge 50
        self.send()  # unknown server consumption: retain 100
        self.send(estimate=100, usage={'input_tokens': 90, 'output_tokens': 30})  # charge 120
        self.assertEqual(self.failure(estimate=31), 'quota_exhausted')
        self.send(estimate=30)
        self.assertEqual(self.failure(estimate=1), 'quota_exhausted')
        self.assertEqual(self.source.query.call_count, 1)

    def test_stale_refresh_never_increases_budget_and_no_midnight_reset(self):
        self.send(estimate=100)
        self.clock.today += timedelta(days=1)
        self.clock.now += 21
        self.send(estimate=100)
        self.assertEqual(self.failure(estimate=101), 'quota_exhausted')
        self.assertEqual(self.source.query.call_count, 2)
        self.clock.today += timedelta(days=1)  # exact expiry day is conservatively unusable
        self.assertEqual(self.failure(estimate=1), 'quota_unknown')

    def test_new_run_rechecks_protection_even_within_observation_ttl(self):
        self.send()
        self.clock.now += .1
        self.rows = [row(autoStop=False)]
        resumed = BailianQuota(self.control, identity=lambda route: self.proof,
                               protection=lambda route, observation: True, source=self.source,
                               clock=self.clock, today=lambda: self.clock.today)
        self.assertIsNone(resumed.priority(self.route))
        self.assertEqual(self.failure(resumed), 'quota_unknown')
        self.assertEqual(self.source.query.call_count, 2)

    def test_new_run_requires_fresh_observation_even_at_identical_clock_value(self):
        self.send()
        self.rows = [row(autoStop=False)]
        resumed = BailianQuota(self.control, identity=lambda route: self.proof,
                               protection=lambda route, observation: True, source=self.source,
                               clock=self.clock, today=lambda: self.clock.today)
        self.assertEqual(self.failure(resumed), 'quota_unknown')
        self.assertEqual(self.source.query.call_count, 2)

    def test_same_context_shares_query_and_changed_context_keeps_account_budget(self):
        control = MTControl(threading.Event())
        source = mock.Mock()
        source.bound_to.return_value = True
        calls = []
        def query(**kwargs):
            calls.append(1)
            time.sleep(.05)
            return json.dumps([row(remaining=200)]).encode()
        source.query.side_effect = query
        def identity(route):
            fingerprint = route.backend.key_fingerprint
            workspace = 'ws-test' if fingerprint.startswith('a') else 'ws-other'
            return VerifiedIdentity('same-account', workspace, fingerprint, 'cn-beijing', 'c' * 64)
        quota = BailianQuota(control, identity=identity, protection=lambda route, observation: True,
                             source=source, today=lambda: date(2026, 11, 2))
        routes = [entry(name='a'), entry(name='b', fingerprint='b' * 64, workspace='ws-other')]
        def send(_):
            try:
                with quota.reserve(routes[0], 2, estimate=100) as reservation:
                    reservation.claim()
                    reservation.mark_sent()
                return True
            except RouteFailure as error:
                self.assertEqual(error.category, 'quota_exhausted')
                return False
        with ThreadPoolExecutor(max_workers=6) as executor:
            self.assertEqual(sum(executor.map(send, range(6))), 2)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(RouteFailure) as error:
            with quota.reserve(routes[1], 2, estimate=100):
                pass
        self.assertEqual(error.exception.category, 'quota_exhausted')
        self.assertEqual(len(calls), 2)

    def test_each_model_has_a_separate_budget_and_safe_expiry_priority(self):
        self.rows = [row(remaining=100), row('other-model', remaining=200, expires='2026-11-03')]
        other = entry('other-model', name='other')
        self.assertIsNone(self.quota.priority(other))
        self.send(estimate=100)
        self.assertEqual(self.failure(estimate=1), 'quota_exhausted')
        self.assertEqual(self.quota.priority(other), date(2026, 11, 3))
        self.send(other, 200)
        self.assertIsNone(self.quota.priority(other))
        self.assertEqual(self.source.query.call_count, 1)
        self.clock.now += 21
        self.assertIsNone(self.quota.priority(other))

    def test_refresh_during_inflight_does_not_invent_refund(self):
        with self.quota.reserve(self.route, 5, estimate=100) as old:
            old.claim()
            self.clock.now += 21
            with self.quota.reserve(self.route, 5, estimate=100) as new:
                new.claim()
                new.mark_sent()
        self.send(estimate=100)
        self.assertEqual(self.failure(estimate=1), 'quota_exhausted')

    def test_expired_or_failed_observation_is_never_reused_and_errors_are_redacted(self):
        self.send()
        self.clock.now += 21
        self.source.query.side_effect = OSError('private key and private CLI details')
        with self.assertRaises(RouteFailure) as error:
            with self.quota.reserve(self.route, 3.5, estimate=100):
                self.fail('stale data must fail closed')
        self.assertEqual(error.exception.category, 'quota_unknown')
        self.assertNotIn('private', str(error.exception))
        self.assertLessEqual(self.source.query.call_args.kwargs['timeout'], 3.5)

    def test_failed_slow_refresh_is_suppressed_from_completion_and_recovers(self):
        self.rows = [row(), row('other-model')]
        self.send()
        self.clock.now += 21

        def slow_failure(**kwargs):
            self.clock.now += 2.3
            raise OSError('private CLI response')

        self.source.query.side_effect = slow_failure
        self.assertEqual(self.failure(), 'quota_unknown')
        self.assertEqual(self.source.query.call_count, 2)
        other = entry('other-model', name='other')
        self.assertEqual(self.failure(route=other), 'quota_unknown')
        self.assertEqual(self.source.query.call_count, 2)
        self.clock.now += 9.9
        self.assertEqual(self.failure(route=other), 'quota_unknown')
        self.assertEqual(self.source.query.call_count, 2)

        self.clock.now += .2
        self.source.query.side_effect = lambda **kwargs: json.dumps(self.rows).encode()
        self.send(other)
        self.assertEqual(self.source.query.call_count, 3)

    def test_new_console_context_can_retry_during_previous_failure_cooldown(self):
        self.source.query.side_effect = OSError('private CLI response')
        self.assertEqual(self.failure(), 'quota_unknown')
        self.assertEqual(self.source.query.call_count, 1)
        self.proof = VerifiedIdentity('account-a', 'ws-other', 'b' * 64, 'cn-beijing', 'c' * 64)
        other = entry(fingerprint='b' * 64, workspace='ws-other')
        self.source.query.side_effect = lambda **kwargs: json.dumps(self.rows).encode()
        self.send(other)
        self.assertEqual(self.source.query.call_count, 2)

    def test_authentication_failure_emits_only_fixed_safe_diagnostic(self):
        self.source.query.side_effect = QuotaSourceError('quota_console_session_invalid')
        with tempfile.TemporaryDirectory() as temp:
            writer = MTDiagnostics(Path(temp), 'a' * 32)
            self.quota.on_event = writer.write_quota
            self.assertEqual(self.failure(), 'quota_unknown')
            records = [json.loads(line) for line in writer.path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(records[-1]['category'], 'quota_console_session_invalid')
        self.assertNotIn('private', repr(records))

    def test_invalid_session_is_queried_once_for_both_models_throughout_run(self):
        self.source.query.side_effect = QuotaSourceError('quota_console_session_invalid')
        events = []
        self.quota.on_event = events.append
        other = entry('other-model', name='other')
        for _ in range(340):
            self.assertEqual(self.failure(), 'quota_unknown')
            self.assertEqual(self.failure(route=other), 'quota_unknown')
            self.clock.now += 11  # Exceed the ordinary failure cooldown every round.
        self.assertEqual(self.source.query.call_count, 1)
        self.assertEqual(sum(event['kind'] == 'quota_query' for event in events), 1)
        self.assertEqual(sum(event.get('category') == 'quota_console_session_invalid' for event in events), 1)
        self.assertFalse(self.quota.potential(self.route))
        self.assertFalse(self.quota.potential(other))
        self.assertIsNone(self.quota.priority(self.route))

    def test_changed_console_proof_can_retry_but_cannot_revive_invalid_proof(self):
        self.source.query.side_effect = QuotaSourceError('quota_console_session_invalid')
        self.assertEqual(self.failure(), 'quota_unknown')
        invalid = self.proof
        self.proof = VerifiedIdentity('account-a', 'ws-test', 'a' * 64, 'cn-beijing', 'd' * 64)
        self.source.query.side_effect = lambda **kwargs: json.dumps(self.rows).encode()
        self.send()
        self.assertEqual(self.source.query.call_count, 2)
        self.proof = invalid
        self.clock.now += 11
        self.assertEqual(self.failure(), 'quota_unknown')
        self.assertEqual(self.source.query.call_count, 2)

    def test_new_run_after_invalid_session_rechecks_binding_protection_and_expiry(self):
        self.source.query.side_effect = QuotaSourceError('quota_console_session_invalid')
        self.assertEqual(self.failure(), 'quota_unknown')
        self.clock.now += 11
        self.source.query.side_effect = lambda **kwargs: json.dumps(self.rows).encode()
        self.assertEqual(self.failure(), 'quota_unknown')
        resumed = BailianQuota(self.control, identity=lambda route: self.proof,
                               protection=lambda route, observation: False, source=self.source,
                               clock=self.clock, today=lambda: self.clock.today)
        self.assertEqual(self.failure(resumed), 'quota_unknown')
        resumed.protection = lambda route, observation: True
        self.source.bound_to.return_value = False
        self.assertEqual(self.failure(resumed), 'quota_unknown')
        self.assertEqual(self.source.query.call_count, 1)
        self.source.bound_to.return_value = True
        self.rows = [row(autoStop=False)]
        self.assertEqual(self.failure(resumed), 'quota_unknown')
        self.clock.now += 21
        self.rows = [row(expires=self.clock.today.isoformat())]
        self.assertEqual(self.failure(resumed), 'quota_unknown')
        self.clock.now += 21
        self.rows = [row()]
        with resumed.reserve(self.route, 5, estimate=100) as reservation:
            reservation.claim()
            reservation.mark_sent()
        self.assertEqual(self.source.query.call_count, 4)

    def test_unclassified_source_failure_remains_retryable_after_cooldown(self):
        self.source.query.side_effect = QuotaSourceError()
        self.assertEqual(self.failure(), 'quota_unknown')
        self.assertTrue(self.quota.potential(self.route))
        self.clock.now += 11
        self.source.query.side_effect = lambda **kwargs: json.dumps(self.rows).encode()
        self.send()
        self.assertEqual(self.source.query.call_count, 2)

    def test_cancel_and_cache_hit_make_no_network_request(self):
        router = MTRouter([self.route], paid_enabled=False, control=self.control, clock=self.clock)
        result = router.run(lambda *args: self.fail('no request'), cache=lambda route: 'cached',
                            prepare=lambda route, wait: self.quota.reserve(route, wait, estimate=100))
        self.assertEqual(result.value, 'cached')
        self.source.query.assert_not_called()
        self.control.cancel_user()
        with self.assertRaises(Cancelled):
            with self.quota.reserve(self.route, 5, estimate=100):
                pass
        self.source.query.assert_not_called()

    def test_full_payload_estimate_includes_output_and_utf8(self):
        payload = {'messages': [{'role': 'user', 'content': '课程'}], 'max_tokens': 500}
        self.assertGreaterEqual(estimate_tokens(payload), len(json.dumps(payload, ensure_ascii=False).encode()) + 500)
        for value in (0, -1, True, None, '100'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                estimate_tokens({'max_tokens': value})

    def test_cli_json_parser_discards_unknown_fields_and_rejects_ambiguous_json(self):
        parsed = parse_observation(json.dumps([row(private='DO-NOT-REPORT')]).encode())
        self.assertTrue(parsed['qwen-test'].usable(self.clock.today))
        self.assertNotIn('DO-NOT-REPORT', repr(parsed))
        for raw in (b'{}', b'not-json', b'[{"model":"qwen-test","model":"other"}]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_observation(raw)


class CLISourceTests(unittest.TestCase):
    def run_child(self, script, *, timeout=2, cancel_after=None):
        source = CLIQuotaSource()
        control = MTControl(threading.Event())
        started = time.monotonic()
        process = []
        popen = subprocess.Popen
        def capture(*args, **kwargs):
            process.append(popen(*args, **kwargs))
            return process[-1]
        timer = None
        if cancel_after is not None:
            timer = threading.Timer(cancel_after, control.cancel_user)
            timer.start()
        try:
            with mock.patch.object(source, '_command', return_value=[sys.executable, '-c', script]), \
                    mock.patch('subtitle_tool.mt_bailian_quota.subprocess.Popen', side_effect=capture):
                try:
                    result = source.query(control=control, timeout=timeout)
                except (QuotaSourceError, Cancelled) as error:
                    result = error
        finally:
            if timer:
                timer.cancel()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(process), 1)
        self.assertIsNotNone(process[0].poll(), 'CLI child must be reaped before return')
        return result

    def test_deadline_and_cancellation_terminate_and_reap_real_child(self):
        script = 'import time; time.sleep(30)'
        self.assertIsInstance(self.run_child(script, timeout=.2), QuotaSourceError)
        self.assertIsInstance(self.run_child(script, cancel_after=.2), Cancelled)

    def test_real_child_output_and_error_are_bounded_and_sanitized(self):
        self.assertEqual(self.run_child('print("[]")'), b'[]\r\n' if os.name == 'nt' else b'[]\n')
        error = self.run_child('import sys; print("private CLI failure", file=sys.stderr); sys.exit(1)')
        self.assertIsInstance(error, QuotaSourceError)
        self.assertNotIn('private', str(error))
        error = self.run_child('import sys; sys.stdout.write("x" * (5 * 1024 * 1024)); sys.stdout.flush()')
        self.assertIsInstance(error, QuotaSourceError)

    def test_session_error_classification_uses_only_fixed_cli_error_shape(self):
        script = ('import json,sys; print(json.dumps({"error":{"code":3,"message":'
                  '"Console session is not logged in or has expired.","rawResponse":"private"}}),'
                  'file=sys.stderr); sys.exit(3)')
        error = self.run_child(script)
        self.assertEqual(error.category, 'quota_console_session_invalid')
        self.assertNotIn('private', str(error))

        for script in (
            'import sys; print("private CLI failure", file=sys.stderr); sys.exit(3)',
            'import sys; print("private CLI failure", file=sys.stderr); sys.exit(1)',
            'import sys; sys.stderr.write("x" * (5 * 1024 * 1024)); sys.stderr.flush(); sys.exit(3)',
            'import sys; print(\'{"error":{"code":3,"message":"Console session is not logged in or has expired.","message":"private"}}\', file=sys.stderr); sys.exit(3)',
        ):
            with self.subTest(script=script[:40]):
                error = self.run_child(script)
                self.assertIsInstance(error, QuotaSourceError)
                self.assertEqual(error.category, 'quota_refresh_failed')
                self.assertNotIn('private', str(error))

    def test_query_child_does_not_inherit_credentials_from_the_environment(self):
        synthetic = {'ALIYUN_ACCESS_KEY_SECRET': 'synthetic-only', 'UNEXPECTED_PRIVATE_KEY': 'synthetic-only',
                     'DASHSCOPE_API_KEY': 'synthetic-only'}
        with mock.patch.dict(os.environ, synthetic):
            script = ('import os; print(all(name not in os.environ for name in '
                      '("ALIYUN_ACCESS_KEY_SECRET", "UNEXPECTED_PRIVATE_KEY", "DASHSCOPE_API_KEY")))')
            self.assertEqual(self.run_child(script).strip(), b'True')


if __name__ == '__main__':
    unittest.main()
