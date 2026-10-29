from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace
from unittest import mock

import requests

from subtitle_tool.api import OpenAICompatible
from subtitle_tool.common import Cancelled, MT_INPUT_BUDGET, MT_TEMPLATE_RESERVE, ToolError, atomic_bytes
from subtitle_tool.engine import Engine
from subtitle_tool.mt import (BatchValidationError, batch_cache_key, batch_input_size, batch_payload,
                              batch_recovery_cache_key, parse_batch_response)
from subtitle_tool.mt_backend import MTBackend, configured_mt, key_fingerprint, saved_mt
from subtitle_tool.mt_diagnostics import MTDiagnostics
from subtitle_tool.mt_transport import HTTPReply
from subtitle_tool.mt_runtime import RecoveryCoordinator
from subtitle_tool.api import RequestPacer
from subtitle_tool.subtitles import plan_reference_units, plan_translation_batches
from tests.fixtures import FakeService, SavedResultFixture, TemporaryTest, response, result
from tests.test_translation_v3 import batch_input, batch_response
from tests.test_mt_runtime import Clock


OPENROUTER = 'https://openrouter.ai/api/v1'


class InlineTransport:
    """Protocol tests retain parent mocks; real subprocess I/O is covered separately."""
    def request(self, method, url, *, headers, json=None, **kwargs):
        if method == 'GET':
            return HTTPReply(200, b'{"data":{"free_model_daily_requests":{"used":0,"limit":1000,"remaining":1000}}}')
        if not isinstance(requests.request, mock.Mock):
            raise AssertionError('Offline protocol test requires an HTTP mock')
        reply = requests.request(method, url, headers=headers, json=json)
        return HTTPReply(reply.status_code, reply.content, reply.headers.get('Retry-After', ''))


class OpenRouterProtocolTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.backend = MTBackend('openai_compatible', OPENROUTER, 'openrouter/free',
                                 key_fingerprint('private-mt-key'), request_version=2)
        self.cancel = threading.Event()
        self.cloud = OpenAICompatible('private-mt-key', self.backend, self.cancel, transport=InlineTransport())

    def assert_schema(self, body, count):
        self.assertEqual(set(body), {'model', 'messages', 'stream', 'response_format', 'provider', 'plugins'})
        self.assertEqual(body['model'], 'openrouter/free')
        self.assertIs(body['stream'], False)
        self.assertEqual(body['provider'], {'require_parameters': True})
        self.assertEqual(body['plugins'], [{'id': 'response-healing'}])
        self.assertEqual(body['response_format']['type'], 'json_schema')
        spec = body['response_format']['json_schema']
        self.assertEqual(spec['name'], 'subtitle_translations')
        self.assertIs(spec['strict'], True)
        schema = spec['schema']
        self.assertEqual(schema['type'], 'object')
        self.assertEqual(schema['required'], ['translations'])
        self.assertIs(schema['additionalProperties'], False)
        self.assertEqual(set(schema['properties']), {'translations'})
        array = schema['properties']['translations']
        self.assertEqual(array['type'], 'array')
        item = array['items']
        self.assertEqual(item['type'], 'object')
        self.assertEqual(item['required'], ['id', 'text'])
        self.assertIs(item['additionalProperties'], False)
        self.assertEqual(item['properties'], {'id': {'type': 'string', 'enum': [str(i) for i in range(count)]},
                                              'text': {'type': 'string'}})

    def test_single_and_multiple_fragments_send_strict_schema_without_sampling_parameters(self):
        for fragments in [['Input'], ['One', 'Two', 'Three']]:
            with self.subTest(count=len(fragments)), mock.patch('requests.request',
                    return_value=batch_response(['繁體譯文'] * len(fragments))) as request:
                translated = self.cloud.translate_batch(fragments)
            self.assertEqual(translated.translations, {str(i): '繁体译文' for i in range(len(fragments))})
            self.assert_schema(request.call_args.kwargs['json'], len(fragments))

    def test_version_one_requests_keep_the_historical_wire_shape_and_prompt(self):
        for provider, fields in [('bailian', {'model', 'messages', 'stream', 'temperature', 'seed'}),
                                 ('openai_compatible', {'model', 'messages', 'stream'})]:
            with self.subTest(provider=provider):
                old = batch_payload(['Input'], provider=provider)
                explicit = batch_payload(['Input'], provider=provider, request_version=1)
                self.assertEqual(explicit, old)
                self.assertEqual(set(explicit), fields)
                current = batch_payload(['Input'], **self.backend.request_options())
                self.assertEqual(current['messages'], old['messages'])

    def test_repair_and_recursive_subgroups_regenerate_local_id_enums(self):
        calls, events = [], []
        def handler(*args, **kwargs):
            body = kwargs['json']
            calls.append(body)
            items = batch_input(body)
            return batch_response([] if len(items) > 1 else ['单片译文'])
        with mock.patch('requests.request', side_effect=handler):
            translated = self.cloud.translate_batch(['alpha', 'bravo', 'charlie'], on_event=events.append)
        self.assertTrue(translated.recovered)
        self.assertEqual(translated.translations, {'0': '单片译文', '1': '单片译文', '2': '单片译文'})
        self.assertEqual([len(batch_input(body)) for body in calls], [3, 3, 1, 2, 2, 1, 1])
        for body in calls:
            self.assert_schema(body, len(batch_input(body)))
            self.assertLessEqual(len(json.dumps(body, ensure_ascii=False).encode()) + MT_TEMPLATE_RESERVE,
                                 MT_INPUT_BUDGET)
        self.assertIn('上次响应', calls[1]['messages'][0]['content'])
        self.assertIn('same_sentence_after: bravo', calls[2]['messages'][0]['content'])
        self.assertTrue(all(event.get('request_id', '') == '' for event in events))

    def test_markdown_explanation_and_invalid_local_values_require_a_valid_repair(self):
        valid = batch_response(['甲', '乙']).json()['choices'][0]['message']['content']
        invalid = [f'```json\n{valid}\n```', 'Here is the JSON:\n' + valid,
                   '{"translations":', '{"translations":[{"id":"0","text":"甲"}]}',
                   '{"translations":[{"id":"0","text":"甲"},{"id":"0","text":"乙"}]}',
                   '{"translations":[{"id":"0","text":"甲"},{"id":"1","text":" "}]}',
                   '{"translations":[{"id":"0","text":"-->"},{"id":"1","text":"乙"}]}']
        for content in invalid:
            with self.subTest(content=content):
                bad = response({'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})
                with mock.patch('requests.request', side_effect=[bad, batch_response(['甲', '乙'])]) as request:
                    self.assertEqual(self.cloud.translate_batch(['alpha', 'bravo']).translations, {'0': '甲', '1': '乙'})
                self.assertEqual(request.call_count, 2)
                for call in request.call_args_list:
                    self.assert_schema(call.kwargs['json'], 2)

    def test_budget_includes_schema_router_and_plugin_before_network(self):
        options = self.backend.request_options()
        context = [{'position': 'before', 'text': 'Some context.'}]
        payload = batch_payload(['Input'], context=context, repair=True, **options)
        self.assertEqual(batch_input_size(['Input'], context=context, request_options=options),
                         len(json.dumps(payload, ensure_ascii=False).encode()) + MT_TEMPLATE_RESERVE)
        old_options = replace(self.backend, request_version=1).request_options()
        self.assertGreater(batch_input_size(['Input'], request_options=options),
                           batch_input_size(['Input'], request_options=old_options))
        text = 'x' * (MT_INPUT_BUDGET - batch_input_size(['x'], request_options=old_options) + 1)
        self.assertEqual(batch_input_size([text], request_options=old_options), MT_INPUT_BUDGET)
        with mock.patch('requests.request') as request, self.assertRaises(ToolError) as error:
            self.cloud.translate_batch([text])
        self.assertEqual(error.exception.code, 'mt_input_budget')
        request.assert_not_called()

    def test_non_stop_finish_precedes_null_or_malformed_content_only_in_version_two(self):
        for content, finish, category in [(None, 'length', 'unfinished'), ({}, 'length', 'unfinished'),
                                          (None, 'stop', 'response_structure'), ('', 'stop', 'json_invalid')]:
            with self.subTest(content=content, finish=finish):
                body = {'choices': [{'finish_reason': finish, 'message': {'content': content}}]}
                with self.assertRaises(BatchValidationError) as error:
                    parse_batch_response(body, ['Input'], request_version=2)
                self.assertEqual(error.exception.category, category)
                if content is None:
                    with self.assertRaises(BatchValidationError) as old:
                        parse_batch_response(body, ['Input'])
                    self.assertEqual(old.exception.category, 'response_structure')
        events = []
        body = {'choices': [{'finish_reason': 'length', 'message': {'content': None}}]}
        with mock.patch('requests.request', side_effect=lambda *a, **k: response(body)) as request, \
                self.assertRaises(ToolError):
            self.cloud.translate_batch(['Input'], on_event=events.append)
        self.assertEqual(request.call_count, 2)
        self.assertEqual([event['category'] for event in events], ['unfinished', 'unfinished'])
        self.assertTrue(all(event['finish_reason'] == 'length' and event['content_type'] == 'null' for event in events))

    def test_business_errors_and_error_finish_never_trigger_format_repair(self):
        secret = 'private-mt-key-and-input'
        for body, attempts in [({'error': {'message': secret}}, 1),
                ({'choices': [{'error': {'message': secret}, 'finish_reason': 'length', 'message': {'content': None}}]}, 1),
                ({'choices': [{'finish_reason': 'error', 'message': {'content': secret}}]}, 4)]:
            with self.subTest(body=body):
                self.cloud.http.control.begin()
                clock = Clock()
                self.cloud.http.recovery = RecoveryCoordinator(clock=clock, jitter=lambda: 0, wait=clock.wait)
                self.cloud.http.pacer = RequestPacer(clock=clock, requests_per_window=0)
                events = []
                with mock.patch('requests.request', return_value=response(body)) as request, self.assertRaises(ToolError) as error:
                    self.cloud.translate_batch(['alpha', 'bravo'], on_event=events.append)
                self.assertEqual(error.exception.code, 'mt_service_error')
                self.assertEqual(request.call_count, attempts)
                self.assertEqual(events, [])
                self.assertNotIn(secret, str(error.exception) + error.exception.request_id)

    def test_rejected_parameters_endpoints_and_quota_keep_constraints_and_safe_hints(self):
        for status, hint in [(400, '参数'), (404, '服务或模型'), (503, '暂不可用'), (429, '限流')]:
            with self.subTest(status=status):
                clock = Clock()
                self.cloud.http.control.begin()
                self.cloud.http.recovery = RecoveryCoordinator(clock=clock, wait=clock.wait, jitter=lambda: 0)
                self.cloud.http.pacer = RequestPacer(clock=clock, requests_per_window=0)
                events = []
                with mock.patch('requests.request', return_value=response({'error': {'message': 'private-input'}},
                        status, {'Retry-After': '0'})) as request, self.assertRaises(ToolError) as error:
                    self.cloud.translate_batch(['Input'], on_event=events.append)
                self.assertEqual(error.exception.code, f'mt_http_{status}')
                self.assertIn(hint, str(error.exception))
                self.assertNotIn('private-input', str(error.exception))
                self.assertEqual(events, [])
                self.assert_schema(request.call_args.kwargs['json'], 1)

    def test_cancel_after_invalid_response_prevents_repair(self):
        def handler(*args, **kwargs):
            self.cancel.set()
            return batch_response([])
        with mock.patch('requests.request', side_effect=handler) as request, self.assertRaises(Cancelled):
            self.cloud.translate_batch(['Input'])
        self.assertEqual(request.call_count, 1)

    def test_diagnostics_capture_only_format_metadata_and_locally_mapped_finish(self):
        events = []
        content = '```json\n{"translations":[]}\n```'
        body = {'id': '01234567-89ab-cdef-0123-456789abcdef',
                'choices': [{'finish_reason': 'private-input', 'message': {'content': content}}]}
        with mock.patch('requests.request', side_effect=lambda *a, **k: response(body)), self.assertRaises(ToolError):
            self.cloud.translate_batch(['Input'], on_event=events.append)
        writer = MTDiagnostics(self.path, 'a' * 32)
        self.assertTrue(all(writer.write(event) for event in events))
        raw = writer.path.read_text(encoding='utf-8')
        record = json.loads(raw.splitlines()[0])
        self.assertEqual(record['finish_reason'], 'other')
        self.assertEqual(record['content_type'], 'string')
        self.assertEqual(record['content_length'], len(content))
        self.assertIs(record['code_fence'], True)
        self.assertEqual(record['json_root_type'], 'invalid')
        self.assertNotIn('private-input', raw)
        self.assertNotIn(body['id'], raw)
        self.assertNotIn('translations', raw)
        self.assertTrue(writer.write({'kind': 'invalid_response', 'finish_reason': 'private-input',
            'content_type': ['private-input'], 'content_length': True, 'code_fence': 'private-input',
            'json_root_type': {'private-input': 1}}))
        injected = json.loads(writer.path.read_text(encoding='utf-8').splitlines()[-1])
        for name in ['finish_reason', 'content_type', 'content_length', 'code_fence', 'json_root_type']:
            self.assertNotIn(name, injected)


class OpenRouterRecoveryTests(SavedResultFixture):
    def setUp(self):
        super().setUp()
        self.config.settings.mt_provider = 'openai_compatible'
        self.config.settings.mt_base_url = OPENROUTER
        self.config.settings.mt_model = 'openrouter/free'
        atomic_bytes(self.config.data / 'mt-key.txt', b'private-mt-key')
        transport = mock.patch('subtitle_tool.mt_runtime.ProcessTransport', side_effect=InlineTransport)
        transport.start()
        self.addCleanup(transport.stop)

    def run_fake(self, service):
        # Each explicit resume in this fixture represents a fresh CLI process.
        with mock.patch.dict('subtitle_tool.mt_runtime._QUOTAS', clear=True), \
                mock.patch.dict('subtitle_tool.mt_runtime._RECOVERY', clear=True):
            return super().run_fake(service)

    def install(self, request_version=1, raw=None):
        self.save_result(raw)
        manifest = self.store.job(self.job_id)['manifest']
        Engine(self.config, self.store).identity(self.job_id, manifest)
        backend = MTBackend('openai_compatible', OPENROUTER, 'openrouter/free',
                            key_fingerprint('private-mt-key'), request_version)
        manifest['mt_snapshot'] = backend.snapshot()
        self.store.update(self.job_id, manifest=manifest)
        return backend

    def test_current_configuration_selects_three_only_for_exact_normalized_openrouter(self):
        for url, version in [('https://OPENROUTER.ai:443/api/v1///', 3), (OPENROUTER, 3),
                             ('https://openrouter.ai/api/v1/extra', 1), ('https://openrouter.ai/API/v1', 1),
                             ('https://openrouter.ai:444/api/v1', 1), ('https://sub.openrouter.ai/api/v1', 1),
                             ('https://other.test/api/v1', 1)]:
            with self.subTest(url=url):
                self.config.settings.mt_base_url = url
                backend = configured_mt(self.config, self.store.job(self.job_id)['manifest'])
                self.assertEqual(backend.request_version, version)
                self.assertEqual(backend.request_options()['request_version'], version)
        self.config.settings.mt_provider = 'bailian'
        self.assertEqual(configured_mt(self.config, self.store.job(self.job_id)['manifest']).request_version, 1)

    def test_snapshot_format_one_reads_both_requests_but_rejects_invalid_version_two_combinations(self):
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        for version in [1, 2]:
            manifest['mt_snapshot']['request_version'] = version
            self.assertEqual(saved_mt(manifest).snapshot(), manifest['mt_snapshot'])
        for change in [{'request_version': True}, {'request_version': 3}, {'provider': 'bailian'},
                       {'base_url': 'https://other.test/api/v1'}]:
            candidate = copy.deepcopy(manifest)
            candidate['mt_snapshot'].update(change)
            with self.subTest(change=change), self.assertRaises(ToolError):
                saved_mt(candidate)

    def test_cache_keys_include_request_version_and_full_snapshot_without_changing_v1(self):
        old = self.install()
        current = replace(old, request_version=2)
        def key(backend):
            return batch_cache_key(['Input'], **backend.cache_scope(), request_options=backend.request_options())
        self.assertNotEqual(key(old), key(current))
        self.assertNotEqual(batch_recovery_cache_key(key(old)), batch_recovery_cache_key(key(current)))
        # Version 1's pre-upgrade serialized request and identity remain byte-for-byte compatible.
        self.assertEqual(key(old), batch_cache_key(['Input'], **old.cache_scope(),
                         request_options={'provider': old.provider, 'model': old.model}))
        self.assertNotEqual(batch_cache_key(['Input'], request_options=old.request_options()),
                            batch_cache_key(['Input'], request_options=current.request_options()))

    def test_old_resume_keeps_request_one_even_with_current_openrouter_configuration(self):
        self.install()
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertTrue(fake.calls)
        self.assertTrue(all(set(call[2]['json']) == {'model', 'messages', 'stream'} for call in fake.calls))
        self.assertEqual(saved_mt(self.store.job(self.job_id)['manifest']).request_version, 1)
        self.assertEqual(fake.uploads, [])

    def test_reflow_snapshots_old_results_and_retranslates_locally_with_version_three(self):
        self.install()
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), 'completed')
        before = self.store.job(self.job_id)
        old_references = self.store.references(self.job_id)
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        prepared = self.store.job(self.job_id)['manifest']
        self.assertEqual(saved_mt(prepared).request_version, 3)
        snapshot = json.loads((self.config.root / prepared['reflow']['snapshot']).read_text(encoding='utf-8'))
        self.assertEqual(snapshot['job']['manifest']['mt_snapshot']['request_version'], 1)
        self.assertEqual(snapshot['references'], old_references)
        self.assertEqual(self.store.references(self.job_id), [])
        for name in ['segments', 'fingerprint', 'cache_identity']:
            self.assertEqual(prepared[name], before['manifest'][name])
        from subtitle_tool.policy import current_execution_snapshot
        self.assertEqual(prepared['execution_snapshot'], current_execution_snapshot(4))
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertTrue(fake.calls)
        self.assertTrue(all(call[1] == OPENROUTER + '/chat/completions' and
                            'response_format' in call[2]['json'] for call in fake.calls))
        self.assertEqual(fake.uploads, [])

    def test_failed_reflow_commit_preserves_all_original_task_records_and_srt(self):
        self.install()
        self.assertEqual(self.run_fake(FakeService(result())), 'completed')
        before = self.store.job(self.job_id)
        records = self.store.sources(self.job_id), self.store.cues(self.job_id), self.store.references(self.job_id)
        old_srt = self.source.with_suffix('.srt').read_bytes()
        with mock.patch.object(self.store, '_replace_records', side_effect=OSError()), self.assertRaises(OSError):
            Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual((self.store.sources(self.job_id), self.store.cues(self.job_id),
                          self.store.references(self.job_id)), records)
        self.assertEqual(self.source.with_suffix('.srt').read_bytes(), old_srt)

    def test_planner_accounts_for_schema_and_trims_context_to_fit(self):
        backend = self.install(request_version=2)
        sources = self.store.sources(self.job_id)
        options = backend.request_options()
        units = plan_reference_units(sources, strategy_version=3, request_options=options)
        groups = plan_translation_batches(units, [{'position': 'before', 'text': 'context' * 1000}],
                                           request_options=options)
        for members, context in groups:
            self.assertLessEqual(batch_input_size([m['text'] for m in members], context=context,
                                                  request_options=options), MT_INPUT_BUDGET)
            self.assertLess(len(context), 1)

    def test_failed_format_result_never_enters_cache_and_resume_keeps_version_two(self):
        self.install(request_version=2)
        with mock.patch('requests.request', side_effect=lambda *a, **k: batch_response([])):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'failed')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
        self.assertFalse(any(r['translated'] for r in self.store.references(self.job_id)))
        self.config.settings.mt_base_url = 'https://other.test/v1'
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertTrue(all('response_format' in call[2]['json'] and call[1].startswith(OPENROUTER) for call in fake.calls))

    def test_failed_group_preserves_another_inflight_success_without_caching_failure(self):
        self.install(request_version=2, raw=result(('First sentence.', 'en'), ('Second sentence.', 'en')))
        self.config.settings.mt_workers = 2
        entered = threading.Barrier(2)
        def handler(*args, **kwargs):
            text = batch_input(kwargs['json'])[0]['text']
            entered.wait(timeout=5)
            return batch_response(['成功译文']) if text.startswith('First') else response({
                'error': {'message': 'private', 'metadata': {'error_type': 'permission_denied'}}}, 403)
        with mock.patch('requests.request', side_effect=handler):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'failed')
        self.assertEqual([bool(r['translated']) for r in self.store.references(self.job_id)], [True, False])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 1)

    def test_cancellation_still_saves_a_complete_inflight_version_two_result(self):
        self.install(request_version=2)
        cancel = threading.Event()
        def handler(*args, **kwargs):
            cancel.set()
            return batch_response(['成功译文'])
        with mock.patch('requests.request', side_effect=handler):
            self.assertEqual(Engine(self.config, self.store, cancel).run(self.job_id), 'cancelled')
        self.assertTrue(all(r['translated'] for r in self.store.references(self.job_id)))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 1)
