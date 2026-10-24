"""Structured sentence translation regressions; all text and services are synthetic."""
from __future__ import annotations

import json
import threading
import sqlite3
import copy
from dataclasses import replace
from unittest import mock

from subtitle_tool.api import OpenAICompatible
from subtitle_tool.common import MT_INPUT_BUDGET, ToolError
from subtitle_tool.mt import (BatchValidationError, batch_cache_key, batch_input_size, batch_payload,
                              parse_batch_response)
from subtitle_tool.mt_backend import MTBackend, key_fingerprint, saved_mt
from subtitle_tool.engine import Engine
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_runtime import FreeQuota, RecoveryCoordinator
from subtitle_tool.mt_transport import HTTPReply, TransportError
from subtitle_tool.policy import current_execution_snapshot, load_policy
from subtitle_tool.subtitles import validate_unit_coverage
from tests.fixtures import SavedResultFixture, TemporaryTest, response, result, timed_raw
from tests.test_mt_runtime import Clock, quota
from tests.test_openrouter import InlineTransport


def mapped_response(values, *, finish='stop'):
    content = json.dumps({'translations': {str(i): text for i, text in enumerate(values)}})
    return response({'choices': [{'finish_reason': finish, 'message': {'content': content}}]})


class StructuredProtocolTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.backend = MTBackend('openai_compatible', 'https://openrouter.ai/api/v1', 'openrouter/free',
                                 key_fingerprint('synthetic-mt-key'), 3)

    def test_fixed_mapping_schema_and_provider_controls(self):
        fragments = ['She said, "Wait', 'until morning."']
        options = self.backend.request_options()
        payload = batch_payload(fragments, context=[{'position': 'before', 'text': 'A reported instruction.'}], **options)
        spec = payload['response_format']['json_schema']
        self.assertTrue(spec['strict'])
        schema = spec['schema']
        self.assertEqual(schema['required'], ['translations'])
        self.assertFalse(schema['additionalProperties'])
        mapping = schema['properties']['translations']
        self.assertEqual(mapping, {'type': 'object', 'properties': {
            '0': {'type': 'string', 'minLength': 1}, '1': {'type': 'string', 'minLength': 1}},
            'required': ['0', '1'], 'additionalProperties': False})
        self.assertEqual(payload['provider'], {'require_parameters': True})
        self.assertEqual(payload['plugins'], [{'id': 'response-healing'}])

    def test_http_200_stop_with_unclosed_cross_fragment_quote_fails_once_without_repair_or_split(self):
        content = '{"translations":{"0":"她说：\\"等到","1":"早上。}}'
        bad = response({'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})
        cloud = OpenAICompatible('synthetic-mt-key', self.backend, threading.Event(), transport=InlineTransport())
        events = []
        with mock.patch('requests.request', return_value=bad) as request, self.assertRaises(ToolError) as error:
            cloud.translate_batch(['She said, "Wait', 'until morning."'], on_event=events.append)
        self.assertEqual(error.exception.code, 'mt_batch_invalid')
        self.assertEqual(request.call_count, 1)
        self.assertEqual([(event['kind'], event['category']) for event in events], [('invalid_response', 'json_invalid')])
        self.assertNotIn(content, str(error.exception))

    def test_local_validation_rejects_missing_duplicate_extra_empty_punctuation_truncated_and_controls(self):
        cases = [('{"translations":{"0":"甲"}}', 'stop', 'id_missing'),
                 ('{"translations":{"0":"甲","0":"乙","1":"丙"}}', 'stop', 'json_duplicate_key'),
                 ('{"translations":{"0":"甲","1":"乙","2":"丙"}}', 'stop', 'id_out_of_range'),
                 ('{"translations":{"0":" ","1":"乙"}}', 'stop', 'translation_invalid'),
                 ('{"translations":{"0":"。","1":"乙"}}', 'stop', 'translation_invalid'),
                 ('{"translations":{"0":"甲","1":"乙"}}', 'length', 'unfinished'),
                 (None, 'length', 'unfinished'),
                 ('{"translations":{"0":"甲\\u001b乙","1":"丙"}}', 'stop', 'translation_invalid'),
                 ('{"translations":{"0":"甲\\ud800","1":"乙"}}', 'stop', 'translation_invalid')]
        for content, finish, expected in cases:
            with self.subTest(category=expected):
                body = {'choices': [{'finish_reason': finish, 'message': {'content': content}}]}
                with self.assertRaises(BatchValidationError) as error:
                    parse_batch_response(body, ['alpha', 'bravo'], request_version=3)
                self.assertEqual(error.exception.category, expected)

    def test_valid_quotes_keep_program_owned_index_mapping(self):
        texts = ['她说：“等到', '早上。”']
        self.assertEqual(parse_batch_response(mapped_response(texts).json(), ['She said, "Wait', 'until morning."'],
                                              request_version=3), {'0': texts[0], '1': texts[1]})

    def test_budget_planning_splits_only_at_reliable_times_and_trims_background(self):
        from subtitle_tool.subtitles import make_fragment, normalize, plan_translation_batches, split_mt_units
        source = normalize(timed_raw(['a' * 900] * 8, word_ms=1100), 0, '0000', preserve_sentences=True)[0][0]
        options = self.backend.request_options()
        units = split_mt_units(source, [make_fragment(source)], request_options=options)
        validate_unit_coverage(source, units)
        groups = plan_translation_batches(units, [{'position': 'before', 'text': 'background' * 2000}],
                                           request_options=options)
        self.assertGreater(len(groups), 1)
        self.assertEqual([c['cue_id'] for group, _ in groups for c in group], [c['cue_id'] for c in units])
        for group, context in groups:
            self.assertFalse(any(c['position'] == 'before' for c in context))
            self.assertLessEqual(batch_input_size([c['text'] for c in group], context=context, request_options=options), MT_INPUT_BUDGET)
        source['words'] = []
        with self.assertRaises(ToolError) as error:
            split_mt_units(source, [make_fragment(source)], request_options=options)
        self.assertEqual(error.exception.code, 'mt_input_budget')

    def test_cache_isolates_all_backend_identity_and_wire_contract_fields(self):
        backend = self.backend
        def key(backend, **kwargs):
            return batch_cache_key(['Input'], request_options=backend.request_options(), **backend.cache_scope(), **kwargs)
        base = key(backend)
        variants = [replace(backend, request_version=2), replace(backend, model='fixed/model:free'),
                    replace(backend, key_fingerprint=key_fingerprint('changed-key')),
                    replace(backend, base_url='https://other.test/v1')]
        self.assertTrue(all(key(changed) != base for changed in variants))
        self.assertNotEqual(base, key(backend, context=[{'position': 'before', 'text': 'A background.'}]))
        with mock.patch('subtitle_tool.mt.STRUCTURED_PROMPT_VERSION', 99):
            self.assertNotEqual(base, key(backend))
        fallback = MTBackend('bailian', 'https://space.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
                             'qwen3.8-flash', key_fingerprint('asr-key'), 3)
        self.assertTrue(fallback.cache_scope())
        self.assertNotEqual(key(fallback), key(replace(fallback, model='qwen-mt-flash', request_version=1)))


class StructuredPipelineTests(SavedResultFixture):
    def setUp(self):
        super().setUp()
        self.config.settings.mt_provider = 'openai_compatible'
        self.config.settings.mt_base_url = 'https://openrouter.ai/api/v1'
        self.config.settings.mt_model = 'openrouter/free'
        self.config.settings.mt_workers = 1
        (self.config.data / 'mt-key.txt').write_text('synthetic-mt-key', encoding='utf-8')
        self.clock, self.resources, self.calls = Clock(), {}, []
        def resources(backend):
            key = (backend.provider, backend.model)
            if key not in self.resources:
                self.resources[key] = (RecoveryCoordinator(clock=self.clock, jitter=lambda: 0, wait=self.clock.wait),
                    FreeQuota(clock=self.clock, utcnow=lambda: self.clock.day, wait=self.clock.wait))
            return self.resources[key]
        patch = mock.patch('subtitle_tool.mt_runtime.runtime_resources', side_effect=resources)
        patch.start()
        self.addCleanup(patch.stop)
        from subtitle_tool.api import RequestPacer
        patch = mock.patch('subtitle_tool.mt_runtime.mt_pacer',
                           side_effect=lambda *a, **k: RequestPacer(clock=self.clock, requests_per_window=0))
        patch.start()
        self.addCleanup(patch.stop)

    def install(self, raw=None):
        self.save_result(raw or result(('She said, "Wait until morning."', 'en'), ('Another sentence.', 'en')))
        Engine(self.config, self.store).reflow(self.job_id, mt_fallback='bailian')
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual((manifest['translation_strategy_version'], manifest['mt_snapshot']['request_version']), (4, 3))
        self.assertEqual(manifest['execution_snapshot'], current_execution_snapshot(4))
        self.assertEqual(manifest['mt_fallback_policy']['backend']['model'], 'qwen3.8-flash')
        return manifest

    def run_backend(self, handler, *, cancel=None):
        transport = mock.Mock()
        def request(method, url, **kwargs):
            if method == 'GET':
                return handler('quota', {}, kwargs)
            role = 'primary' if url.startswith('https://openrouter.ai/') else 'fallback'
            self.calls.append((role, kwargs['json']))
            targets = json.loads(kwargs['json']['messages'][1]['content'])['targets']
            return handler(role, targets, kwargs)
        transport.request.side_effect = request
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', return_value=transport):
            return Engine(self.config, self.store, cancel).run(self.job_id)

    @staticmethod
    def success(targets):
        return HTTPReply(200, mapped_response(['完整译文'] * len(targets)).content)

    def events(self):
        path = self.path / '.build' / 'mt-diagnostics' / (self.job_id + '.jsonl')
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def test_no_reference_stage_and_format_failure_falls_back_whole_group_once(self):
        raw = timed_raw(['She', 'said,', '"Wait', 'until', 'the', 'morning."'], word_ms=1400)
        manifest = self.install(raw)
        def handler(role, targets, kwargs):
            if role == 'quota':
                return quota()
            if role == 'primary':
                return HTTPReply(200, b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"translations\\":{\\"0\\":\\"unclosed}}"}}]}')
            return self.success(targets)
        self.assertEqual(self.run_backend(handler), 'completed')
        self.assertEqual([role for role, _ in self.calls], ['primary', 'fallback'])
        self.assertGreater(len(json.loads(self.calls[0][1]['messages'][1]['content'])['targets']), 1)
        backup = self.calls[1][1]
        self.assertEqual(backup['model'], 'qwen3.8-flash')
        self.assertIs(backup['enable_thinking'], False)
        self.assertNotIn('provider', backup)
        self.assertNotIn('plugins', backup)
        self.assertEqual(self.store.references(self.job_id), [])
        counts = self.store.counts(self.job_id)
        self.assertEqual(counts.get('mt_reference_request', 0), 0)
        self.assertEqual(counts.get('mt_split', 0), 0)
        self.assertTrue(all(cue['mt_origin'] == 'fallback' for cue in self.store.cues(self.job_id)))
        commits = [event for event in self.events() if event['kind'] == 'runtime_commit']
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]['committed_count'], len(self.store.cues(self.job_id)))
        old = json.loads((self.path / manifest['reflow']['snapshot']).read_text(encoding='utf-8'))
        self.assertEqual(old['job']['manifest']['translation_strategy_version'], 3)

    def check_transient(self, failure):
        self.install(result(('Only sentence.', 'en')))
        def handler(role, targets, kwargs):
            if role == 'quota':
                return quota()
            if role == 'primary':
                if isinstance(failure, Exception):
                    raise failure
                return failure
            return self.success(targets)
        self.assertEqual(self.run_backend(handler), 'completed')
        self.assertEqual([role for role, _ in self.calls], ['primary'] * 4 + ['fallback'])
        self.assertTrue(all(body == self.calls[0][1] for _, body in self.calls[:4]))

    def test_502_uses_shared_four_attempt_budget_before_fallback(self):
        self.check_transient(HTTPReply(502, b'{}'))

    def test_deadline_uses_shared_four_attempt_budget_before_fallback(self):
        self.check_transient(TransportError('deadline'))

    def test_quota_observation_exhaustion_uses_fallback_without_primary_http(self):
        self.install()
        self.assertEqual(self.run_backend(lambda role, targets, kwargs:
            quota(0) if role == 'quota' else self.success(targets)), 'completed')
        self.assertEqual([role for role, _ in self.calls], ['fallback', 'fallback'])
        self.assertTrue(any(event.get('quota_source') == 'server_observation' for event in self.events()))

    def test_local_ledger_exhaustion_is_distinct_from_remote_observation(self):
        ledger = FreeQuota(clock=self.clock, utcnow=lambda: self.clock.day, wait=self.clock.wait)
        control = MTControl(threading.Event())
        ledger.ensure(control, lambda: quota(1))
        self.assertTrue(ledger.try_reserve(control))
        ledger.complete()
        with self.assertRaises(ToolError) as error:
            ledger.ensure(control, lambda: quota(1))
        self.assertEqual(error.exception.quota_source, 'local_ledger')
        self.assertIn('本地', str(error.exception))
        self.assertEqual(ledger.observation['remaining'], 1)

    def test_authentication_does_not_invoke_fallback(self):
        self.install()
        self.assertEqual(self.run_backend(lambda role, targets, kwargs:
            quota() if role == 'quota' else HTTPReply(401, b'{}')), 'failed')
        self.assertEqual([role for role, _ in self.calls], ['primary'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)

    def test_unknown_quota_stops_before_both_backends(self):
        self.install()
        self.assertEqual(self.run_backend(lambda *args: HTTPReply(200, b'{}')), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_quota_unknown')
        self.assertEqual(self.calls, [])

    def test_both_invalid_backends_save_no_partial_group_and_resume_retries_primary(self):
        self.install()
        def invalid(role, targets, kwargs):
            return quota() if role == 'quota' else HTTPReply(200, mapped_response([]).content)
        self.assertEqual(self.run_backend(invalid), 'failed')
        self.assertEqual([role for role, _ in self.calls], ['primary', 'fallback'])
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_fallback_failed')
        self.assertFalse(any(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_split', 0), 0)
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_backend(lambda role, targets, kwargs:
            quota() if role == 'quota' else self.success(targets)), 'completed')
        self.assertEqual([role for role, _ in self.calls], ['primary', 'primary'])

    def test_translation_expansion_has_two_rounds_and_invalidates_only_affected_sentence(self):
        self.config.settings.zh_line_width = 12
        raw = timed_raw([f'word{i:02}' for i in range(16)], word_ms=400)
        raw['transcripts'][0]['sentences'].extend(timed_raw(['Short', 'sentence.'], word_ms=600,
                                                          start=7500)['transcripts'][0]['sentences'])
        self.install(raw)
        def handler(role, targets, kwargs):
            if role == 'quota':
                return quota()
            long = next(iter(targets.values()))['text'].startswith('word')
            return HTTPReply(200, mapped_response(['译' * 32 if long else '短句'] * len(targets)).content)
        self.assertEqual(self.run_backend(handler), 'completed')
        sizes = [len(json.loads(body['messages'][1]['content'])['targets']) for _, body in self.calls]
        self.assertEqual(sizes, [1, 1, 2, 4])
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['layout_round'], 2)
        self.assertTrue(manifest['warnings'])
        self.assertEqual(self.store.counts(self.job_id).get('mt_reference_request', 0), 0)
        cues = self.store.cues(self.job_id)
        self.assertEqual(len(cues), 5)
        for source in self.store.sources(self.job_id):
            validate_unit_coverage(source, [c for c in cues if c['sentence_id'] == source['sentence_id']])
        self.assertTrue(all(c['translated'] for c in cues))

    def test_cancel_after_invalid_reply_never_starts_fallback(self):
        self.install()
        cancel = threading.Event()
        def handler(role, targets, kwargs):
            if role == 'quota':
                return quota()
            cancel.set()
            return HTTPReply(200, mapped_response([]).content)
        self.assertEqual(self.run_backend(handler, cancel=cancel), 'cancelled')
        self.assertEqual([role for role, _ in self.calls], ['primary'])

    def test_new_snapshot_cannot_resume_with_old_prompt_or_missing_identity(self):
        manifest = self.install()
        for change in [{'execution_snapshot': current_execution_snapshot(3)},
                       {'execution_snapshot': None}, {'mt_snapshot': {**manifest['mt_snapshot'], 'request_version': 2}}]:
            candidate = {**manifest, **change}
            with self.subTest(change=list(change)), self.assertRaises(ToolError):
                load_policy(candidate)
                saved_mt(candidate)
        for field in ['execution_snapshot', 'mt_snapshot']:
            candidate = copy.deepcopy(manifest)
            candidate.pop(field)
            with self.assertRaises(ToolError):
                load_policy(candidate)
                saved_mt(candidate)

    def test_fallback_transaction_rolls_back_translation_origin_cache_and_counts(self):
        self.install(result(('One complete group.', 'en')))
        self.store.db.execute("CREATE TRIGGER reject_group BEFORE INSERT ON counters "
            "WHEN NEW.name='mt_fallback_success' BEGIN SELECT RAISE(ABORT,'synthetic rejection'); END")
        self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_backend(lambda role, targets, kwargs: quota(0) if role == 'quota' else self.success(targets))
        self.assertFalse(any(c['translated'] or c.get('mt_origin') for c in self.store.cues(self.job_id)))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_fallback_completed', 0), 0)
        self.assertFalse(any(e['kind'] == 'runtime_commit' for e in self.events()))

    def test_primary_cache_reflow_is_local_and_fallback_cache_is_consulted_only_after_cutover(self):
        self.install()
        handler = lambda role, targets, kwargs: quota(0) if role == 'quota' else self.success(targets)
        self.assertEqual(self.run_backend(handler), 'completed')
        fallback_keys = {row[0] for row in self.store.db.execute('SELECT key FROM mt_cache')}
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.resources.clear()
        self.calls.clear()
        lookup = self.store.cache_get
        with mock.patch.object(self.store, 'cache_get', wraps=lookup) as cache:
            self.assertEqual(self.run_backend(lambda role, targets, kwargs:
                quota() if role == 'quota' else self.success(targets)), 'completed')
        self.assertFalse(fallback_keys.intersection(call.args[0] for call in cache.call_args_list))
        self.assertEqual([role for role, _ in self.calls], ['primary', 'primary'])
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        with mock.patch('subtitle_tool.mt_runtime.ProcessTransport', side_effect=AssertionError('Cache-only run')):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'completed')

    def test_actual_route_model_is_allowlisted_in_runtime_diagnostics(self):
        from subtitle_tool.mt_diagnostics import MTDiagnostics
        writer = MTDiagnostics(self.path, self.job_id)
        for value in ['vendor/model:free', 'https://private.test/key', 'sk-private-key', 'private text\nbody']:
            writer.write_runtime({'kind': 'runtime_request', 'actual_model': value, 'model': 'openrouter/free',
                                  'request_version': 3, 'stage': 'subtitle', 'category': 'response_received'})
        self.assertEqual([event.get('actual_model') for event in self.events()], ['vendor/model:free', None, None, None])
