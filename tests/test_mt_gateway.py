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
    def pipeline(self):
        self.install()
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        return Pipeline(engine, self.job_id, manifest, scope)

    def data(self, stage='translate', text='Synthetic cache sentence.'):
        data = {'sentences': [{'id': '0', 'text': text}], 'context': [], 'glossary': [],
                'fragments': [{'id': '0', 'sentence': '0', 'text': text}]}
        if stage in {'review', 'judge'}:
            data['translations'] = {'0': '缓存。'}
        if stage == 'judge':
            data['issues'] = []
        return data

    def stage(self, pipeline, stage='translate', data=None):
        data = data or self.data(stage)
        return pipeline.stage(stage, data, ids=['0'], originals=[data['sentences'][0]['text']])

    def test_reasoning_request_applies_to_every_stage_and_repair(self):
        pipeline = self.pipeline()
        entry = pipeline.router.entries[0]
        for stage in ('glossary', 'translate', 'review', 'judge'):
            for repair in ((False, True) if stage == 'translate' else (False,)):
                with self.subTest(stage=stage, repair=repair):
                    payload = pipeline._request_payload(stage, self.data(stage), entry, repair=repair)
                    self.assertEqual(payload.get('reasoning_effort'), 'low' if stage == 'judge' else 'none')
                    self.assertEqual(payload['max_tokens'], 4096)
                    self.assertEqual(payload['model'], entry.backend.model)

    def test_truncation_retains_cause_request_and_reasoning_usage_without_saving_text(self):
        pipeline = self.pipeline()
        response = {'model': 'synthetic/reasoning', 'usage': {'prompt_tokens': 1414,
                    'completion_tokens': 4096, 'completion_tokens_details': {'reasoning_tokens': 3931}},
                    'choices': [{'finish_reason': 'length', 'message': {
                        'content': '{"translations":{"0":"private-incomplete',
                        'reasoning': 'private reasoning'}}]}
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            return HTTPReply(200, json.dumps(response).encode())
        with mock.patch.object(pipeline.transport, 'request', side_effect=send) as request:
            with self.assertRaises(RouteExhausted) as caught:
                self.stage(pipeline)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(caught.exception.code, 'mt_output_truncated')
        self.assertIn('截断', str(caught.exception))
        self.assertEqual(self.store.mt_stages(self.job_id), [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
        raw = pipeline.diagnostics.path.read_text(encoding='utf-8')
        ends = [json.loads(line) for line in raw.splitlines() if json.loads(line)['kind'] == 'request_end']
        self.assertEqual(caught.exception.request_id, ends[-1]['request_id'])
        self.assertEqual(ends[-1]['reasoning_tokens'], 3931)
        self.assertEqual(self.store.counts(self.job_id)['mt_response_reasoning_tokens'], 7862)
        self.assertNotIn('private', raw)

    def test_truncated_response_automatically_uses_paid_fallback_and_reuses_it(self):
        pipeline = self.pipeline()
        calls = []
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            calls.append(kwargs)
            return (reply({}, finish='length') if len(calls) == 1 else
                    reply({'translations': {'0': '完整后备译文。'}}, model='paid/accepted'))
        with mock.patch.object(pipeline.transport, 'request', side_effect=send):
            value, origin, proof = self.stage(pipeline)
            self.assertEqual(self.stage(pipeline)[0], value)
        self.assertEqual([call['json']['model'] for call in calls],
                         ['group/transcribe', 'group/transcribe-paid'])
        self.assertEqual(origin, 'magpie_fallback')
        self.assertEqual(proof['actual_model'], 'paid/accepted')
        self.assertEqual(calls[0]['json']['messages'], calls[1]['json']['messages'])
        self.assertEqual(self.store.counts(self.job_id).get('mt_format_repair_request', 0), 0)
        self.assertEqual(self.store.counts(self.job_id)['mt_paid_fallback_request'], 1)
        self.assertEqual(self.store.counts(self.job_id)['mt_paid_fallback_success'], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 1)

    def test_exhausted_format_repair_moves_to_paid_fallback_once(self):
        pipeline = self.pipeline()
        calls = []
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            calls.append(kwargs)
            return reply({'translations': {} if len(calls) < 4 else {'0': '完整后备译文。'}})
        with mock.patch.object(pipeline.transport, 'request', side_effect=send):
            self.stage(pipeline)
        self.assertEqual([call['json']['model'] for call in calls],
                         ['group/transcribe'] * 2 + ['group/transcribe-paid'] * 2)
        self.assertEqual(calls[0]['json']['messages'], calls[2]['json']['messages'])
        self.assertEqual(calls[1]['json']['messages'], calls[3]['json']['messages'])
        events = [json.loads(line) for line in pipeline.diagnostics.path.read_text(encoding='utf-8').splitlines()]
        completed = [event for event in events if event['kind'] == 'route_complete']
        self.assertEqual(completed[-1].get('attempt'), 4)

    def test_paid_fallback_exhaustion_is_bounded_and_keeps_last_request_id(self):
        for truncated, expected in [(True, 2), (False, 4)]:
            with self.subTest(truncated=truncated):
                pipeline = self.pipeline()
                def send(*args, **kwargs):
                    kwargs['on_dispatch']()
                    return reply({'translations': {}}, finish='length' if truncated else 'stop')
                with mock.patch.object(pipeline.transport, 'request', side_effect=send) as request:
                    with self.assertRaises(RouteExhausted) as caught:
                        self.stage(pipeline)
                self.assertEqual(request.call_count, expected)
                events = [json.loads(line) for line in pipeline.diagnostics.path.read_text(encoding='utf-8').splitlines()]
                ends = [event for event in events if event['kind'] == 'request_end']
                self.assertEqual(caught.exception.request_id, ends[-1]['request_id'])
                self.assertEqual(self.store.mt_stages(self.job_id), [])

    def test_paid_fallback_does_not_retry_unknown_transport_or_refusal(self):
        for answer in (TransportError('network'), reply({}, finish='content_filter'),
                       HTTPReply(200, b'{"choices":[{"message":{"refusal":"blocked"}}]}'),
                       HTTPReply(200, b'{}'), reply({}, finish='tool_calls')):
            pipeline = self.pipeline()
            def send(*args, **kwargs):
                kwargs['on_dispatch']()
                if isinstance(answer, Exception):
                    raise answer
                return answer
            with self.subTest(answer=type(answer).__name__), \
                    mock.patch.object(pipeline.transport, 'request', side_effect=send) as request:
                with self.assertRaises(RouteExhausted):
                    self.stage(pipeline)
            self.assertEqual(request.call_count, 1)

    def test_primary_timeout_reserves_time_for_paid_fallback(self):
        pipeline = self.pipeline()
        clock, calls = [100.0], []
        pipeline.router.clock = lambda: clock[0]
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            calls.append(kwargs)
            if len(calls) == 1:
                clock[0] += kwargs['timeout']
                raise TransportError('deadline')
            return reply({'translations': {'0': '超时后自动恢复。'}})
        with mock.patch('time.monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(pipeline.transport, 'request', side_effect=send):
            self.assertEqual(self.stage(pipeline)[1], 'magpie_fallback')
            self.assertEqual(len(calls), 2)
            self.assertAlmostEqual(calls[0]['timeout'], 120)
            self.assertAlmostEqual(calls[1]['timeout'], 60)

    def test_primary_format_repair_cannot_consume_fallback_time(self):
        pipeline = self.pipeline()
        clock, calls = [100.0], []
        pipeline.router.clock = lambda: clock[0]
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            calls.append(kwargs)
            if len(calls) == 1:
                clock[0] += 115
                return reply({'translations': {}})
            if len(calls) == 2:
                clock[0] += kwargs['timeout']
                raise TransportError('deadline')
            return reply({'translations': {'0': '超时后自动恢复。'}})
        with mock.patch('time.monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(pipeline.transport, 'request', side_effect=send):
            self.assertEqual(self.stage(pipeline)[1], 'magpie_fallback')
        self.assertEqual([round(call['timeout']) for call in calls], [120, 5, 60])
        self.assertEqual([call['json']['model'] for call in calls],
                         ['group/transcribe', 'group/transcribe', 'group/transcribe-paid'])

    def test_job_completes_with_paid_fallback_and_preserves_provenance(self):
        from subtitle_tool.mt_quality import PROMPTS
        from subtitle_tool.mt_pipeline import validate_provenance
        self.install(split=True)
        calls = []
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            payload = kwargs['json']
            stage = next(name for name, prompt in PROMPTS.items()
                         if payload['messages'][0]['content'].startswith(prompt))
            data = json.loads(payload['messages'][1]['content'])
            calls.append((stage, payload['model']))
            if stage == 'translate' and payload['model'] == 'group/transcribe':
                return reply({}, finish='length')
            value = ({'terms': []} if stage == 'glossary' else {'issues': []} if stage == 'review' else
                     {'translations': {item['id']: '完整后备译文。' for item in
                                       data['fragments']}})
            return reply(value, model='paid/accepted')
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport') as transport:
            transport.return_value.request.side_effect = send
            engine = Engine(self.config, self.store)
            self.assertEqual(engine.run(self.job_id), 'completed')
        validate_provenance(engine, self.job_id, self.store.job(self.job_id)['manifest'])
        self.assertTrue(all(cue['mt_origin'] == 'magpie_fallback' for cue in self.store.cues(self.job_id)))
        self.assertIn(('translate', 'group/transcribe-paid'), calls)
        self.assertTrue(self.source.with_suffix('.srt').is_file())

    def test_fallback_group_rejects_free_members_before_dispatch(self):
        from subtitle_tool.common import atomic_json
        pipeline = self.pipeline()
        registry = Path(self.config.settings.magpie_data_dir) / 'providers.json'
        raw = json.loads(registry.read_bytes())
        next(group for group in raw['groups'] if group['id'] == 'transcribe-paid')['members'] = ['group/transcribe-free']
        atomic_json(registry, raw)
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            return reply({}, finish='length')
        with mock.patch.object(pipeline.transport, 'request', side_effect=send) as request:
            with self.assertRaises(ToolError) as caught:
                self.stage(pipeline)
        self.assertEqual(caught.exception.code, 'magpie_group')
        self.assertEqual(request.call_count, 1)

    def test_fallback_cache_is_reusable_when_paid_network_is_denied(self):
        pipeline = self.pipeline()
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            return (reply({}, finish='length') if kwargs['json']['model'] == 'group/transcribe' else
                    reply({'translations': {'0': '完整后备译文。'}}))
        with mock.patch.object(pipeline.transport, 'request', side_effect=send):
            value = self.stage(pipeline)[0]
        Engine(self.config, self.store).reflow(self.job_id)
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope)
        with mock.patch.object(pipeline.transport, 'request', side_effect=AssertionError('unexpected HTTP')):
            self.assertEqual(self.stage(pipeline)[0], value)

    def test_truncation_is_reported_by_job_and_resume_reuses_completed_stages(self):
        from subtitle_tool.main import summary
        self.install()
        def send(*args, **kwargs):
            kwargs['on_dispatch']()
            prompt = kwargs['json']['messages'][0]['content']
            return (reply({'terms': []}) if prompt.startswith('从课程原文') else
                    reply({'translations': {}}, finish='length'))
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport') as transport:
            transport.return_value.request.side_effect = send
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'failed')
        report = summary(self.store, self.job_id)
        self.assertEqual(report['error_code'], 'mt_output_truncated')
        self.assertRegex(report['request_id'], r'^[0-9a-f]{32}$')
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertNotIn('glossary', [stage for stage, _, _ in self.calls])
        self.assertTrue(self.source.with_suffix('.srt').is_file())

    def test_truncated_advisory_stages_keep_complete_translation(self):
        from subtitle_tool.mt_quality import PROMPTS
        for version, failed in enumerate(('glossary', 'review', 'judge'), 1):
            with self.subTest(stage=failed):
                self.config.settings.mt_policy_version = version
                self.install()
                def send(*args, **kwargs):
                    kwargs['on_dispatch']()
                    payload = kwargs['json']
                    stage = next(name for name, prompt in PROMPTS.items()
                                 if payload['messages'][0]['content'].startswith(prompt))
                    data = json.loads(payload['messages'][1]['content'])
                    if stage == failed:
                        return reply({}, finish='length')
                    if stage == 'glossary':
                        value = {'terms': []}
                    elif stage == 'review':
                        value = {'issues': [{'id': '0', 'category': 'number', 'evidence': '合成疑点'}]}
                    else:
                        targets = data['sentences'] if stage == 'translate' else data['fragments']
                        value = {'translations': {item['id']: '完整译文。' for item in targets}}
                    return reply(value)
                with mock.patch('subtitle_tool.mt_transport.ProcessTransport') as transport:
                    transport.return_value.request.side_effect = send
                    self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'completed')
                self.assertGreater(self.store.counts(self.job_id)['mt_output_truncated'], 0)
                self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))
                # Reflow in the next case needs its own output; retain this result.
                self.source.with_suffix('.srt').rename(self.path / (failed + '.srt'))

    def test_previous_generation_contract_requires_reflow_and_does_not_reuse_cache(self):
        from subtitle_tool.main import lifecycle_status, summary
        from subtitle_tool import mt_quality, mt_pipeline
        previous = mt_quality.QUALITY_PROMPT_VERSION - 1
        with mock.patch.object(mt_quality, 'QUALITY_PROMPT_VERSION', previous), \
                mock.patch.object(mt_pipeline, 'QUALITY_PROMPT_VERSION', previous), \
                mock.patch('subtitle_tool.policy.QUALITY_PROMPT_VERSION', previous):
            self.install()
            self.assertEqual(self.run_pool(), 'completed')
        before = self.store.job(self.job_id)
        with self.assertRaises(ToolError) as caught:
            Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(caught.exception.code, 'cache_version')
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertIn('reflow', str(lifecycle_status({**before, 'state': 'failed'})))
        self.assertEqual(summary(self.store, self.job_id)['mt_route_status'], 'reflow_required')
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.calls.clear()
        self.assertEqual(self.run_pool(), 'completed')
        self.assertIn('translate', [stage for stage, _, _ in self.calls])
        self.assertNotIn('asr', [stage for stage, _, _ in self.calls])

    def test_reasoning_usage_rejects_invalid_optional_details(self):
        pipeline = self.pipeline()
        for index, reasoning in enumerate((None, True, -1, 5, '3', [], {})):
            with self.subTest(reasoning=reasoning):
                response = json.loads(reply({'translations': {'0': '完整译文。'}}).body)
                response['usage']['completion_tokens_details'] = {'reasoning_tokens': reasoning}
                def send(*args, **kwargs):
                    kwargs['on_dispatch']()
                    return HTTPReply(200, json.dumps(response).encode())
                with mock.patch.object(pipeline.transport, 'request', side_effect=send):
                    self.stage(pipeline, data=self.data(text=f'Synthetic usage case {index}.'))
        self.assertNotIn('mt_response_reasoning_tokens', self.store.counts(self.job_id))
        events = [json.loads(line) for line in pipeline.diagnostics.path.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(all('reasoning_tokens' not in event for event in events))

    def test_policy_revision_reflow_changes_cache_and_unchanged_revision_reuses_it(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        self.config.settings.mt_policy_version = 2
        self.calls.clear()
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertIn('translate', [name for name, _, _ in self.calls])
        route = self.store.job(self.job_id)['manifest']['mt_route_snapshot']
        self.assertEqual({entry['backend']['policy_version'] for entry in route['entries']}, {2})
        self.calls.clear()
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(self.calls, [])

    def test_resume_uses_saved_policy_after_current_settings_change(self):
        self.install()
        self.cancel_after = 'translate'
        self.assertEqual(self.run_pool(cancel=threading.Event()), 'cancelled')
        self.config.settings.mt_policy_version = 2
        self.cancel_after = None
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertNotIn('translate', [name for name, _, _ in self.calls])
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

    def test_unsupported_route_remains_reportable_and_reflows_explicitly(self):
        from subtitle_tool.main import summary
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        manifest = self.store.job(self.job_id)['manifest']
        manifest['mt_route_snapshot']['format_version'] -= 1
        self.store.update(self.job_id, manifest=manifest)
        before = self.store.job(self.job_id)
        output = self.source.with_suffix('.srt').read_bytes()
        with self.assertRaises(ToolError) as caught:
            Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(caught.exception.code, 'mt_config')
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
        pipeline = self.pipeline()
        data = self.data()
        original = copy.deepcopy(data)
        answers = iter([reply({'translations': {}}, model='synthetic/rejected'),
                        reply({'translations': {'0': '完整缓存译文。'}})])
        calls = []
        def send(*args, **kwargs):
            calls.append(kwargs)
            kwargs['on_dispatch']()
            return next(answers)
        with mock.patch.object(pipeline.transport, 'request', side_effect=send):
            value, _, _ = self.stage(pipeline, data=data)
        self.assertEqual(value['translations'], {'0': '完整缓存译文。'})
        self.assertEqual(len(calls), 2)
        self.assertEqual({c['json']['model'] for c in calls}, {'group/transcribe'})
        self.assertEqual(calls[0]['json']['messages'][1], calls[1]['json']['messages'][1])
        self.assertIn('重新生成', calls[1]['json']['messages'][0]['content'])
        self.assertEqual(data, original)
        with mock.patch.object(pipeline.transport, 'request', side_effect=AssertionError('unexpected HTTP')):
            self.assertEqual(self.stage(pipeline, data=data)[0], value)

    def test_format_repair_is_bounded_and_never_saves_invalid_results(self):
        pipeline = self.pipeline()
        calls = []
        def send(*args, **kwargs):
            calls.append(kwargs)
            kwargs['on_dispatch']()
            return reply({'translations': {}})
        with mock.patch.object(pipeline.transport, 'request', side_effect=send), self.assertRaises(RouteExhausted):
            self.stage(pipeline)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.store.mt_stages(self.job_id), [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)

    def test_repair_budget_is_reserved_before_dispatch_and_preserves_frozen_evidence(self):
        pipeline = self.pipeline()
        data = self.data()
        data['context'] = [{'position': 'before', 'text': 'Synthetic background. ' * 20}]
        entry = pipeline.router.entries[0]
        capabilities = pipeline.profiles[entry.id]['capabilities']
        initial = make_stage_payload('translate', data, entry.backend, capabilities=capabilities)
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
                pipeline.stage('translate', data, ids=['0'], originals=[data['sentences'][0]['text']], fixed_semantics=True)
        self.assertEqual(caught.exception.code, 'mt_input_budget')


    def test_transport_refusal_truncation_and_review_failures_do_not_repair(self):
        scenarios = [('translate', HTTPReply(429, b'{}')), ('translate', HTTPReply(503, b'{}')),
                     ('translate', TransportError('network')), ('translate', HTTPReply(200, b'{"choices":[]}')),
                     ('translate', reply({'translations': {}}, finish='length')),
                     ('translate', reply({'translations': {}}, finish=None)),
                     ('translate', HTTPReply(200, b'{"choices":[{"message":{"refusal":"blocked"}}]}')),
                     ('translate', HTTPReply(200, b'{"choices":[{"finish_reason":"stop",'
                                              b'"message":{"content":"{}","refusal":"blocked"}}]}')),
                     ('review', reply({'issues': 'invalid'})), ('judge', reply({'translations': {}}))]
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
                self.assertEqual(request.call_count, 2 if stage in {'review', 'judge'} or
                    isinstance(answer, HTTPReply) and b'length' in answer.body else 1)
                if stage == 'translate':
                    self.assertEqual(request.call_args_list[0].kwargs['json']['model'], 'group/transcribe')

    def test_repair_and_fallback_share_deadline_and_respect_cancel(self):
        for finish, action in [(finish, action) for finish in ('stop', 'length') for action in ('consume', 'expire', 'cancel')]:
            pipeline = self.pipeline()
            data = self.data(text=f'Synthetic deadline case {finish} {action}.')
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
                    return reply({'translations': {}}, finish=finish)
                return reply({'translations': {'0': '缓存。'}})
            with self.subTest(finish=finish, action=action), mock.patch('time.monotonic', side_effect=lambda: clock[0]), \
                    mock.patch.object(pipeline.transport, 'request', side_effect=send):
                if action == 'consume':
                    self.stage(pipeline, data=data)
                    self.assertEqual(len(calls), 2)
                    self.assertLessEqual(calls[1]['timeout'], 105)
                    self.assertEqual(calls[1]['json']['model'], 'group/transcribe-paid' if finish == 'length' else 'group/transcribe')
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
