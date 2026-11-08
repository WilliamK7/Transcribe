"""Policy identity, bounded format repair and request-level gateway evidence."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from subtitle_tool.common import Cancelled, ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.mt_pipeline import Pipeline
from subtitle_tool.mt_pool import configured_route, saved_route
from subtitle_tool.mt_quality import make_stage_payload
from subtitle_tool.mt_routing import RouteExhausted
from subtitle_tool.mt_transport import HTTPReply, TransportError
from tests.mt_fixtures import PoolPipelineFixture


def reply(value, *, model='synthetic/accepted', finish='stop'):
    return HTTPReply(200, json.dumps({'model': model,
        'usage': {'prompt_tokens': 10, 'completion_tokens': 4},
        'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(value)}}]}).encode())


class GatewayContractTests(PoolPipelineFixture):
    def pipeline(self, *, paid=False):
        self.config.settings.mt_routing = {'paid_enabled': paid}
        self.install()
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        return Pipeline(engine, self.job_id, manifest, scope)

    def data(self, stage='draft', text='Synthetic cache sentence.'):
        data = {'sentences': [{'id': '0', 'text': text}], 'context': [], 'glossary': []}
        if stage in {'align', 'review', 'correct'}:
            data.update(draft={'0': '缓存。'}, fragments=[{'id': '0', 'sentence': '0', 'text': text}])
        if stage in {'review', 'correct'}:
            data['translations'] = {'0': '缓存。'}
        if stage == 'correct':
            data['issues'] = []
        return data

    def stage(self, pipeline, stage='draft', data=None):
        data = data or self.data(stage)
        return pipeline.stage(stage, data, ids=['0'], originals=[data['sentences'][0]['text']])

    def test_policy_revision_reflow_changes_cache_and_unchanged_revision_reuses_it(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        self.config.settings.mt_policy_version = 2
        self.calls.clear()
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertIn('draft', [name for name, _, _ in self.calls])
        route = self.store.job(self.job_id)['manifest']['mt_route_snapshot']
        self.assertEqual({entry['backend']['policy_version'] for entry in route['entries']}, {2})
        self.calls.clear()
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(self.calls, [])

    def test_resume_uses_saved_policy_after_current_settings_change(self):
        self.install()
        self.cancel_after = 'draft'
        self.assertEqual(self.run_pool(cancel=threading.Event()), 'cancelled')
        self.config.settings.mt_policy_version = 2
        self.cancel_after = None
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertNotIn('draft', [name for name, _, _ in self.calls])
        route = self.store.job(self.job_id)['manifest']['mt_route_snapshot']
        self.assertEqual({entry['backend']['policy_version'] for entry in route['entries']}, {1})

    def test_policy_revision_rejects_invalid_settings_and_mixed_saved_versions(self):
        pipeline = self.pipeline()
        manifest = self.store.job(self.job_id)['manifest']
        for version in [True, 0, -1, 1.5, '2', None, 2**31]:
            self.config.settings.mt_policy_version = version
            with self.subTest(version=version), self.assertRaises(ToolError):
                configured_route(self.config, manifest)
        for version in [True, 0, '2', 2]:
            changed = copy.deepcopy(manifest)
            changed['mt_route_snapshot']['entries'][0]['backend']['policy_version'] = version
            with self.subTest(saved=version), self.assertRaises(ToolError):
                saved_route(changed)
        self.assertTrue(pipeline.snapshot)

    def test_old_contract_remains_reportable_and_reflows_explicitly(self):
        from subtitle_tool.main import summary
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        manifest = self.store.job(self.job_id)['manifest']
        manifest['execution_snapshot']['recovery_version'] = 4
        manifest['mt_route_snapshot']['format_version'] = 2
        for entry in manifest['mt_route_snapshot']['entries']:
            entry['backend']['format_version'] = 1
            entry['backend'].pop('policy_version')
        self.store.update(self.job_id, manifest=manifest)
        before = self.store.job(self.job_id)
        output = self.source.with_suffix('.srt').read_bytes()
        with self.assertRaises(ToolError) as caught:
            Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(caught.exception.code, 'cache_version')
        self.assertEqual(self.store.job(self.job_id), before)
        report = summary(self.store, self.job_id)
        self.assertEqual(report['mt_route_status'], 'reflow_required')
        self.assertEqual(self.source.with_suffix('.srt').read_bytes(), output)
        self.calls.clear()
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertNotIn('asr', [name for name, _, _ in self.calls])
        self.assertEqual(summary(self.store, self.job_id)['mt_policy_version'], 1)

    def test_format_repair_preserves_fee_group_and_original_evidence(self):
        for stage, paid in [('draft', False), ('align', True)]:
            with self.subTest(stage=stage, paid=paid):
                pipeline = self.pipeline(paid=paid)
                data = self.data(stage)
                original = copy.deepcopy(data)
                answers = iter([reply({'translations': {}}, model='synthetic/rejected'),
                                reply({'translations': {'0': '完整缓存译文。'}})])
                calls = []
                def send(*args, **kwargs):
                    calls.append(kwargs)
                    kwargs['on_dispatch']()
                    return next(answers)
                with mock.patch.object(pipeline.transport, 'request', side_effect=send):
                    value, _, _ = self.stage(pipeline, stage, data)
                self.assertEqual(value['translations'], {'0': '完整缓存译文。'})
                self.assertEqual(len(calls), 2)
                self.assertEqual({c['json']['model'] for c in calls},
                                 {'group/transcribe' if paid else 'group/transcribe-free'})
                self.assertEqual(calls[0]['json']['messages'][1], calls[1]['json']['messages'][1])
                self.assertIn('重新生成', calls[1]['json']['messages'][0]['content'])
                self.assertEqual(data, original)
                with mock.patch.object(pipeline.transport, 'request', side_effect=AssertionError('unexpected HTTP')):
                    self.assertEqual(self.stage(pipeline, stage, data)[0], value)

    def test_format_repair_is_bounded_and_never_saves_invalid_results(self):
        pipeline = self.pipeline()
        calls = []
        def send(*args, **kwargs):
            calls.append(kwargs)
            kwargs['on_dispatch']()
            return reply({'translations': {}})
        with mock.patch.object(pipeline.transport, 'request', side_effect=send), self.assertRaises(RouteExhausted):
            self.stage(pipeline)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.mt_stages(self.job_id), [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)

    def test_repair_budget_is_reserved_before_dispatch_and_preserves_frozen_evidence(self):
        pipeline = self.pipeline()
        data = self.data()
        data['context'] = [{'position': 'before', 'text': 'Synthetic background. ' * 20}]
        entry = pipeline.router.selected
        capabilities = pipeline.profiles[entry.id]['capabilities']
        initial = make_stage_payload('draft', data, entry.backend, capabilities=capabilities)
        budget = len(json.dumps(initial, ensure_ascii=False).encode('utf-8'))
        capabilities['input_bytes'] = budget
        calls = []
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            calls.append(kwargs['json'])
            return reply({'translations': {} if len(calls) == 1 else {'0': '缓存。'}})
        with mock.patch.object(pipeline.transport, 'request', side_effect=send):
            self.stage(pipeline, data=data)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) <= budget for payload in calls))
        self.assertTrue(all(json.loads(payload['messages'][1]['content'])['context'] == [] for payload in calls))
        self.assertTrue(data['context'])
        # A later stage with already frozen evidence must fail before sending,
        # rather than cut that evidence only when repairing an invalid response.
        with mock.patch.object(pipeline.transport, 'request', side_effect=AssertionError('unexpected HTTP')):
            with self.assertRaises(ToolError) as caught:
                pipeline.stage('draft', data, ids=['0'], originals=[data['sentences'][0]['text']], fixed_semantics=True)
        self.assertEqual(caught.exception.code, 'mt_input_budget')

    def test_format_repair_rechecks_fee_proof_before_second_request(self):
        pipeline = self.pipeline()
        registry = Path(self.config.settings.magpie_data_dir) / 'providers.json'
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            value = json.loads(registry.read_text(encoding='utf-8'))
            value['providers'][0]['chat'] = 'https://unknown.invalid/v1'
            registry.write_text(json.dumps(value), encoding='utf-8')
            return reply({'translations': {}})
        with mock.patch.object(pipeline.transport, 'request', side_effect=send) as request:
            with self.assertRaises(ToolError):
                self.stage(pipeline)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.store.counts(self.job_id).get('mt_format_repair_request', 0), 0)
        self.assertEqual(self.store.mt_stages(self.job_id), [])

    def test_transport_refusal_truncation_and_review_failures_do_not_repair(self):
        scenarios = [('draft', HTTPReply(429, b'{}')), ('draft', HTTPReply(503, b'{}')),
                     ('draft', TransportError('network')), ('draft', HTTPReply(200, b'{"choices":[]}')),
                     ('draft', reply({'translations': {}}, finish='length')),
                     ('draft', reply({'translations': {}}, finish=None)),
                     ('draft', HTTPReply(200, b'{"choices":[{"message":{"refusal":"blocked"}}]}')),
                     ('draft', HTTPReply(200, b'{"choices":[{"finish_reason":"stop",'
                                              b'"message":{"content":"{}","refusal":"blocked"}}]}')),
                     ('review', reply({'issues': 'invalid'})), ('correct', reply({'translations': {}}))]
        for stage, answer in scenarios:
            with self.subTest(stage=stage, answer=type(answer).__name__):
                pipeline = self.pipeline()
                def send(*args, **kwargs):
                    kwargs['on_dispatch']()
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
                with mock.patch.object(pipeline.transport, 'request', side_effect=send) as request, \
                        self.assertRaises(RouteExhausted):
                    self.stage(pipeline, stage)
                self.assertEqual(request.call_count, 1)

    def test_repair_shares_deadline_and_does_not_start_after_expiry_or_cancel(self):
        for action in ['consume', 'expire', 'cancel']:
            pipeline = self.pipeline()
            data = self.data(text=f'Synthetic deadline case {action}.')
            clock = [100.0]
            pipeline.router.clock = lambda: clock[0]
            calls = []
            def send(*args, **kwargs):
                kwargs['on_dispatch']()
                calls.append(kwargs)
                if len(calls) == 1:
                    clock[0] += 75 if action == 'consume' else 181 if action == 'expire' else 0
                    if action == 'cancel':
                        pipeline.engine.mt_control.cancel_user()
                    return reply({'translations': {}})
                return reply({'translations': {'0': '缓存。'}})
            with self.subTest(action=action), mock.patch('time.monotonic', side_effect=lambda: clock[0]), \
                    mock.patch.object(pipeline.transport, 'request', side_effect=send):
                if action == 'consume':
                    self.stage(pipeline, data=data)
                    self.assertEqual(len(calls), 2)
                    self.assertLessEqual(calls[1]['timeout'], 105)
                else:
                    with self.assertRaises(Cancelled if action == 'cancel' else RouteExhausted):
                        self.stage(pipeline, data=data)
                    self.assertEqual(len(calls), 1)

    def test_each_request_records_models_usage_outcome_and_unique_gateway_session(self):
        pipeline = self.pipeline()
        data = self.data(text='private-source-that-must-not-be-logged')
        answers = iter([reply({'translations': {}}, model='synthetic/rejected'),
                        reply({'translations': {'0': '缓存。'}}, model='synthetic/accepted')])
        calls = []
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            calls.append(kwargs)
            return next(answers)
        with mock.patch.object(pipeline.transport, 'request', side_effect=send):
            self.stage(pipeline, data=data)
        events = [json.loads(line) for line in pipeline.diagnostics.path.read_text(encoding='utf-8').splitlines()]
        ends = [event for event in events if event['kind'] == 'request_end']
        self.assertEqual([event['outcome'] for event in ends], ['invalid', 'valid'])
        self.assertEqual([event['attempt'] for event in ends], [1, 2])
        self.assertEqual([event['actual_model'] for event in ends], ['synthetic/rejected', 'synthetic/accepted'])
        self.assertEqual(len({event['request_id'] for event in ends}), 2)
        for event, request in zip(ends, calls):
            self.assertEqual(event['gateway_session'], request['headers']['X-Magpie-Session'])
            self.assertEqual(event['input_tokens'], 10)
            self.assertEqual(event['output_tokens'], 4)
            self.assertIn('duration_ms', event)
            self.assertEqual(event['policy_version'], 1)
        self.assertNotIn('private-source', json.dumps(events))
        counts = self.store.counts(self.job_id)
        self.assertEqual(counts['mt_format_repair_request'], 1)
        self.assertEqual(counts['mt_format_repair_success'], 1)
        self.assertEqual(counts['mt_response_input_tokens'], 20)
        self.assertEqual(counts['mt_input_tokens'], 10)

    def test_dispatched_failure_keeps_terminal_diagnostic_without_raw_exception(self):
        for failure, outcome in [(TransportError('network'), 'transport_error'),
                                 (Cancelled(), 'cancelled'), (RuntimeError('private exception'), 'local_error')]:
            pipeline = self.pipeline()
            def send(*args, **kwargs):
                kwargs['on_dispatch']()
                raise failure
            with self.subTest(outcome=outcome), mock.patch.object(pipeline.transport, 'request', side_effect=send):
                with self.assertRaises(RouteExhausted if isinstance(failure, TransportError) else type(failure)):
                    self.stage(pipeline)
            raw = pipeline.diagnostics.path.read_text(encoding='utf-8')
            events = [json.loads(line) for line in raw.splitlines()]
            starts = [event for event in events if event['kind'] == 'request_start']
            ends = [event for event in events if event['kind'] == 'request_end']
            self.assertEqual(len(starts), len(ends))
            self.assertEqual(ends[-1]['outcome'], outcome)
            self.assertEqual(starts[-1]['request_id'], ends[-1]['request_id'])
            self.assertNotIn('private exception', raw)

    def test_concurrent_stages_keep_request_associations_separate(self):
        pipeline = self.pipeline()
        gate = threading.Barrier(2)
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            gate.wait(timeout=5)
            return reply({'translations': {'0': '缓存。'}})
        with mock.patch.object(pipeline.transport, 'request', side_effect=send), ThreadPoolExecutor(2) as pool:
            values = list(pool.map(lambda i: self.stage(pipeline, data=self.data(text=f'Synthetic sentence {i}.')), range(2)))
        self.assertEqual(len(values), 2)
        events = [json.loads(line) for line in pipeline.diagnostics.path.read_text(encoding='utf-8').splitlines()]
        ends = [event for event in events if event['kind'] == 'request_end']
        self.assertEqual(len({event['gateway_session'] for event in ends}), 2)
        self.assertEqual(len({event['group_key'] for event in ends}), 2)
