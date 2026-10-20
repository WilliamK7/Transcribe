from __future__ import annotations

import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import requests

from subtitle_tool.api import Bailian
from subtitle_tool.common import Cancelled, MT_INPUT_BUDGET, MT_TEMPLATE_RESERVE, ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.mt import (BatchValidationError, batch_cache_key, batch_cache_value, batch_input_size,
                              batch_recovery_cache_key, parse_batch_response, recovery_context)
from subtitle_tool.mt_diagnostics import MTDiagnostics, safe_request_id
from subtitle_tool.policy import load_policy
from subtitle_tool.subtitles import make_fragment, normalize
from tests.fixtures import PipelineFixture, TemporaryTest, response, timed_raw
from tests.test_translation_v3 import batch_input, batch_response


class BatchRecoveryTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.cancel = threading.Event()
        self.cloud = Bailian('offline-recovery-key', 'beijing', self.cancel,
                             workspace_id='offline-space', access_mode='workspace')

    def test_three_fragment_failure_recovers_in_smaller_groups_with_original_ids(self):
        fragments = ['She decided to', 'wait until', 'morning.']
        translations = dict(zip(fragments, ['她决定', '等到', '早上。']))
        seen = []

        def handler(*args, **kwargs):
            items = batch_input(kwargs['json'])
            seen.append([item['text'] for item in items])
            self.assertEqual([item['id'] for item in items], [str(i) for i in range(len(items))])
            return batch_response([] if len(items) == 3 else [translations[item['text']] for item in items])

        with mock.patch('requests.request', side_effect=handler):
            result = self.cloud.translate_batch(fragments, 'English')
        self.assertEqual(result.translations, {'0': '她决定', '1': '等到', '2': '早上。'})
        self.assertEqual(seen, [fragments, fragments, fragments[:1], fragments[1:]])
        self.assertTrue(result.recovered)

    def test_recursive_singletons_bound_requests_and_register_original_indices(self):
        fragments = ['alpha', 'bravo', 'charlie', 'delta', 'echo']
        repaired, attempts, events = set(), [], []

        def handler(*args, **kwargs):
            payload = kwargs['json']
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) + MT_TEMPLATE_RESERVE,
                                 MT_INPUT_BUDGET)
            items = batch_input(payload)
            text = items[0]['text']
            if len(items) == 1 and text in repaired:
                return batch_response(['译文' + text])
            if len(items) == 1:
                repaired.add(text)
            return batch_response([])

        with mock.patch('requests.request', side_effect=handler) as request:
            translated = self.cloud.translate_batch(fragments, on_attempt=attempts.append, on_event=events.append)
        self.assertEqual(request.call_count, 4 * len(fragments) - 2)
        self.assertEqual(translated.translations, {str(i): '译文' + t for i, t in enumerate(fragments)})
        self.assertEqual([indices for indices in attempts if len(indices) == 1],
                         [(i,) for i in range(len(fragments)) for _ in range(2)])
        self.assertEqual(sum(e['kind'] == 'split' for e in events), len(fragments) - 1)

    def test_singleton_failure_stops_before_right_subtree_with_safe_detail(self):
        events = []
        with mock.patch('requests.request', side_effect=lambda *a, **k: batch_response([])) as request:
            with self.assertRaises(ToolError) as error:
                self.cloud.translate_batch(['private first', 'private second', 'private third'], on_event=events.append)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(error.exception.code, 'mt_batch_invalid')
        self.assertIn('编号缺失', str(error.exception))
        self.assertIn('预期 1 项，实际 0 项', str(error.exception))
        self.assertIn('[0,1)', str(error.exception))
        self.assertNotIn('private', str(error.exception))
        self.assertEqual([e['kind'] for e in events], ['invalid_response'] * 2 + ['split'] + ['invalid_response'] * 2)

    def test_normal_success_uses_one_request_without_recovery(self):
        attempts, events = [], []
        with mock.patch('requests.request', return_value=batch_response(['甲', '乙'])) as request:
            translated = self.cloud.translate_batch(['alpha', 'bravo'], on_attempt=attempts.append, on_event=events.append)
        self.assertFalse(translated.recovered)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(attempts, [(0, 1)])
        self.assertEqual(events, [])

    def test_auth_quota_network_and_cancellation_do_not_trigger_splitting(self):
        self.cloud.http.attempts = 1
        for outcome, code in [(response({}, 401), 'http_error'),
                              (response({}, 429, {'Retry-After': '0'}), 'http_error'),
                              (requests.ConnectionError('private original exception'), 'network')]:
            with self.subTest(code=code):
                events = []
                with mock.patch('requests.request', side_effect=[outcome]) as request:
                    with self.assertRaises(ToolError) as error:
                        self.cloud.translate_batch(['alpha', 'bravo'], on_event=events.append)
                self.assertEqual(error.exception.code, code)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(events, [])
        self.cancel.set()
        with mock.patch('requests.request') as request, self.assertRaises(Cancelled):
            self.cloud.translate_batch(['alpha', 'bravo'])
        request.assert_not_called()

    def test_safe_handling_of_every_invalid_outer_response_type(self):
        for body in [None, [], 'private source', 12, True, {}, {'choices': None}, {'choices': {}},
                     {'choices': []}, {'choices': [None]}, {'choices': [[]]},
                     {'choices': [{'message': []}]}, {'choices': [{'message': {'content': 123}}]}]:
            with self.subTest(body=body):
                events = []
                with mock.patch.object(self.cloud.http, 'json', return_value=body):
                    with self.assertRaises(ToolError) as error:
                        self.cloud.translate_batch(['private source'], on_event=events.append)
                self.assertEqual(error.exception.code, 'mt_batch_invalid')
                self.assertEqual([e['category'] for e in events], ['response_structure'] * 2)
                self.assertNotIn('private source', str(error.exception))

    def test_classifies_invalid_json_ids_text_and_finish_reason(self):
        cases = [
            ('not JSON', 'json_invalid', None),
            ('{"translations":[],"translations":[]}', 'json_duplicate_key', None),
            ('{"translations":[{"id":"0","id":"0","text":"甲"}]}', 'json_duplicate_key', None),
            ({'translations': []}, 'id_missing', 0),
            ({'translations': [{'text': '甲'}]}, 'id_missing', 1),
            ({'translations': [{'id': '0', 'text': '甲'}] * 2}, 'id_duplicate', 2),
            ({'translations': [{'id': '9', 'text': '甲'}]}, 'id_out_of_range', 1),
            ({'translations': [{'id': 0, 'text': '甲'}]}, 'id_out_of_range', 1),
            ({'translations': [{'id': [], 'text': '甲'}]}, 'id_out_of_range', 1),
            ({'translations': [{'id': '0', 'text': ' '}]}, 'translation_invalid', 1),
            ({'translations': [{'id': '0', 'text': '-->'}]}, 'translation_invalid', 1),
            ({'translations': [{'id': '0', 'text': '。'}]}, 'translation_invalid', 1),
            ({'translations': [{'id': '0', 'text': '字' * 501}]}, 'translation_invalid', 1),
            ({'translations': None}, 'response_structure', None),
        ]
        for value, category, count in cases:
            with self.subTest(category=category, value=value):
                content = value if isinstance(value, str) else json.dumps(value)
                body = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
                with self.assertRaises(BatchValidationError) as error:
                    parse_batch_response(body, ['alpha', 'bravo'])
                self.assertEqual((error.exception.category, error.exception.actual_count), (category, count))
        for finish in ['length', None, 'content_filter', []]:
            body = batch_response(['甲', '乙'], finish=finish).json()
            with self.assertRaises(BatchValidationError) as error:
                parse_batch_response(body, ['alpha', 'bravo'])
            self.assertEqual((error.exception.category, error.exception.actual_count), ('unfinished', 2))

    def test_context_uses_all_original_siblings_in_distance_order_then_unique_background(self):
        fragments = ['alpha', 'bravo', 'charlie', 'delta', 'echo']
        neighbors = [{'position': 'same_sentence_before', 'text': 'bravo'},
                     {'position': 'before', 'text': 'near'}, {'position': 'after', 'text': 'far'}]
        context = recovery_context(fragments, 2, 3, context=neighbors)
        self.assertEqual([c['text'] for c in context], ['bravo', 'delta', 'alpha', 'echo', 'near', 'far'])
        self.assertEqual(neighbors[0]['text'], 'bravo')
        huge = [*neighbors, {'position': 'after', 'text': 'x' * 10000}]
        self.assertEqual(recovery_context(fragments, 2, 3, context=huge), context)
        large = ['a' * 1900, 'b' * 1900, 'c' * 1900, 'd' * 1900]
        kept = recovery_context(large, 2, 3, context=neighbors)
        self.assertEqual([c['text'] for c in kept], [large[1]])
        self.assertLessEqual(batch_input_size(large[2:3], context=kept), MT_INPUT_BUDGET)

    def test_recovery_cache_key_includes_original_key_and_version(self):
        original = batch_cache_key(['alpha', 'bravo'], 'English', account='account-a')
        recovered = batch_recovery_cache_key(original)
        self.assertNotEqual(original, recovered)
        self.assertNotEqual(recovered, batch_recovery_cache_key(
            batch_cache_key(['alpha', 'bravo'], 'English', account='account-b')))
        with mock.patch('subtitle_tool.mt.RECOVERY_VERSION', 99):
            self.assertNotEqual(recovered, batch_recovery_cache_key(original))


class DiagnosticTests(TemporaryTest):
    def test_concurrent_whitelisted_content_free_diagnostics(self):
        job_id = 'a' * 32
        writers = [MTDiagnostics(self.path, job_id), MTDiagnostics(self.path, job_id)]
        event = {'kind': 'invalid_response', 'category': 'id_missing', 'expected_count': 3, 'actual_count': 1,
                 'fragment_start': 0, 'fragment_end': 3, 'depth': 0, 'repair': False,
                 'request_id': 'chatcmpl-' + 'a' * 32, 'group_key': 'f' * 64,
                 'stage': 'subtitle', 'text': 'private transcript', 'error': 'private exception',
                 'key': 'sk-private', 'url': 'https://private.example/signature'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(pool.map(lambda i: writers[i % 2].write(event), range(80))))
        raw = writers[0].path.read_text(encoding='utf-8')
        records = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(len(records), 80)
        self.assertTrue(all(r['actual_count'] == 1 and r['request_id'] == event['request_id'] for r in records))
        self.assertNotIn('private', raw)
        self.assertNotIn('https:', raw)

    def test_unsafe_ids_and_fields_are_dropped_and_io_failure_is_best_effort(self):
        writer = MTDiagnostics(self.path, 'a' * 32)
        for value in [None, [], {}, 'private text', 'sk-private', 'chatcmpl-sk-private',
                      'https://private.example', 'chatcmpl-abc\nprivate']:
            self.assertEqual(safe_request_id(value), '')
            self.assertTrue(writer.write({'kind': 'split', 'request_id': value,
                                          'actual_count': 'private', 'group_key': 'private'}))
        raw = writer.path.read_text(encoding='utf-8')
        self.assertNotIn('private', raw)
        with mock.patch.object(Path, 'open', side_effect=OSError('private exception')):
            self.assertFalse(writer.write({'kind': 'split'}))


class RecoveryPersistenceTests(PipelineFixture):
    fixture_seconds = 60

    def prepare(self, *, duplicate=False, neighbor=False):
        raw = timed_raw(['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot'], word_ms=1000)
        if duplicate:
            second = copy.deepcopy(raw['transcripts'][0]['sentences'][0])
            for item in [second, *second['words']]:
                item['begin_time'] += 22000
                item['end_time'] += 22000
            raw['transcripts'][0]['sentences'].append(second)
        if neighbor:
            raw['transcripts'][0]['sentences'].append({'text': 'Neighbor.', 'language': 'en',
                                                      'begin_time': 20000, 'end_time': 20900})
        sources = normalize(raw, 0, '0000', preserve_sentences=True)[0]
        cues = [c for s in sources for c in ([make_fragment(s, i, i + 2) for i in range(0, 6, 2)]
                                          if s.get('words') else [make_fragment(s)])]
        manifest = self.store.job(self.job_id)['manifest']
        self.store.initialize_subtitles(self.job_id, sources, cues, manifest)
        self.scope = Engine(self.config, self.store).identity(self.job_id, manifest)[1]
        self.sources, self.cues = sources, cues
        return cues

    def translate(self, handler, cancel=None, *, reference_stage=False, progress=lambda *args: None):
        engine = Engine(self.config, self.store, cancel, progress)
        cloud = Bailian('offline-key', 'beijing', engine.cancel, count=lambda name: self.store.count(self.job_id, name))
        with mock.patch('requests.request', side_effect=handler):
            records = self.store.references(self.job_id) if reference_stage else self.store.cues(self.job_id)
            policy = load_policy(self.store.job(self.job_id)['manifest'])
            engine.translate_batches(self.job_id, records, self.sources, self.scope, lambda: cloud,
                                     context_version=policy.context_version, reference_stage=reference_stage)

    @staticmethod
    def singletons(*args, **kwargs):
        items = batch_input(kwargs['json'])
        return batch_response([] if len(items) > 1 else ['译文' + items[0]['text']])

    def keys(self):
        args = {'context': [], 'stage': 'subtitle', 'asr_language': 'en', **self.scope}
        fragments = [c['text'] for c in self.cues[:3]]
        key = batch_cache_key(fragments, self.cues[0]['source_lang'], **args)
        return key, batch_recovery_cache_key(key)

    def test_subgroup_attempts_complete_commit_and_recovery_cache_reuse(self):
        self.prepare(duplicate=True)
        self.translate(self.singletons)
        cues = self.store.cues(self.job_id)
        self.assertEqual([c['attempts'] for c in cues], [3, 5, 5] * 2)
        self.assertTrue(all(c['translated'] for c in cues))
        counts = self.store.counts(self.job_id)
        self.assertEqual((counts['mt'], counts['mt_subtitle_request'], counts['mt_subtitle_completed']), (7, 7, 6))
        self.assertEqual((counts['mt_invalid_response'], counts['mt_split'], counts['mt_recovery_success']), (4, 2, 1))
        normal_key, recovery_key = self.keys()
        self.assertIsNone(self.store.cache_get(normal_key))
        self.assertIsNotNone(self.store.cache_get(recovery_key))
        with self.store.db:
            self.store.db.execute("UPDATE cues SET translated='' WHERE job_id=? AND cue_id=?",
                                  (self.job_id, cues[1]['cue_id']))
        no_network = mock.Mock(side_effect=AssertionError('must use recovery cache'))
        self.translate(no_network)
        no_network.assert_not_called()
        self.assertEqual(self.store.counts(self.job_id)['mt_recovery_cache_hit'], 3)
        self.assertEqual([c['attempts'] for c in self.store.cues(self.job_id)], [3, 5, 5] * 2)
        # An ordinary complete cache has precedence when both are present.
        normal = {'0': '普通甲', '1': '普通乙', '2': '普通丙'}
        with self.store.db:
            self.store.db.execute('INSERT INTO mt_cache VALUES(?,?)', (normal_key, batch_cache_value(normal)))
            self.store.db.execute("UPDATE cues SET translated='' WHERE job_id=? AND cue_id=?",
                                  (self.job_id, cues[0]['cue_id']))
        self.translate(no_network)
        self.assertEqual([c['translated'] for c in self.store.cues(self.job_id)[:3]], list(normal.values()))
        self.assertEqual(self.store.counts(self.job_id)['mt_recovery_cache_hit'], 3)

    def test_recovery_transaction_rolls_back_results_cache_and_completion_counter(self):
        self.prepare()
        self.store.db.execute("CREATE TEMP TRIGGER reject_recovery BEFORE INSERT ON counters "
                              "WHEN NEW.name='mt_recovery_success' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.translate(self.singletons)
        self.assertFalse(any(c['translated'] for c in self.store.cues(self.job_id)))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_subtitle_completed', 0), 0)

    def test_reference_recovery_uses_reference_records_and_counters(self):
        cues = self.prepare()
        references = [{**c, 'cue_id': c['cue_id'] + ':reference'} for c in cues]
        self.store.save_layout(self.job_id, cues, self.store.job(self.job_id)['manifest'], references=references)
        self.translate(self.singletons, reference_stage=True)
        self.assertEqual([c['attempts'] for c in self.store.references(self.job_id)], [3, 5, 5])
        self.assertTrue(all(c['translated'] for c in self.store.references(self.job_id)))
        self.assertFalse(any(c['translated'] for c in self.store.cues(self.job_id)))
        counts = self.store.counts(self.job_id)
        self.assertEqual((counts['mt_reference_request'], counts['mt_reference_completed']), (7, 3))
        self.assertNotIn('mt_subtitle_request', counts)
        path = self.config.root / '.build' / 'mt-diagnostics' / (self.job_id + '.jsonl')
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(all(e['stage'] == 'reference' for e in events))

    def test_child_http_retries_increase_http_count_without_extra_logical_attempts(self):
        self.prepare()
        retried = False
        def handler(*args, **kwargs):
            nonlocal retried
            items = batch_input(kwargs['json'])
            if len(items) == 3:
                return batch_response([])
            if len(items) == 1 and not retried:
                retried = True
                return response({}, 503)
            return batch_response(['译文'] * len(items))
        with mock.patch('subtitle_tool.api.Http.pause'):
            self.translate(handler)
        counts = self.store.counts(self.job_id)
        self.assertEqual((counts['mt'], counts['mt_subtitle_request']), (5, 4))
        self.assertEqual([c['attempts'] for c in self.store.cues(self.job_id)], [3, 3, 3])

    def test_failed_singleton_does_not_save_successful_left_subtree(self):
        self.prepare()
        def handler(*args, **kwargs):
            items = batch_input(kwargs['json'])
            return batch_response(['左子树成功'] if len(items) == 1 and items[0]['text'].startswith('alpha') else [])
        with self.assertRaises(ToolError) as error:
            self.translate(handler)
        self.assertEqual(error.exception.code, 'mt_batch_invalid')
        self.assertFalse(any(c['translated'] for c in self.store.cues(self.job_id)))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_recovery_success', 0), 0)

    def test_cancelled_partial_subtree_is_not_saved_but_final_inflight_success_is_saved(self):
        for stop_at, saved in [('alpha', False), ('echo', True)]:
            with self.subTest(stop_at=stop_at):
                self.prepare()
                with self.store.db:
                    self.store.db.execute('DELETE FROM mt_cache')
                cancel = threading.Event()
                def handler(*args, **kwargs):
                    items = batch_input(kwargs['json'])
                    if len(items) == 1 and items[0]['text'].startswith(stop_at):
                        cancel.set()
                    return self.singletons(*args, **kwargs)
                with self.assertRaises(Cancelled):
                    self.translate(handler, cancel)
                self.assertEqual(all(c['translated'] for c in self.store.cues(self.job_id)), saved)
                self.assertEqual(self.store.cache_get(self.keys()[1]) is not None, saved)

    def test_failed_recovery_keeps_other_inflight_success_and_stops_new_work(self):
        self.prepare(neighbor=True)
        sources = self.sources
        for i in range(1, 4):
            sources.extend(normalize(timed_raw([f'Neighbor{i}.'], start=20000 + i * 10000),
                                     0, f'{i:04d}', preserve_sentences=True)[0])
        self.store.save_layout(self.job_id, [*self.cues, *[make_fragment(s) for s in sources[2:]]],
                               self.store.job(self.job_id)['manifest'])
        started, failed = threading.Barrier(4), threading.Event()
        self.config.settings.mt_workers = 4
        def progress(*args):
            if self.store.cues(self.job_id)[0]['error']:
                failed.set()
        def handler(*args, **kwargs):
            items = batch_input(kwargs['json'])
            neighbor = items[0]['text'].startswith('Neighbor')
            if len(items) == 3 or neighbor:
                if '上次响应' not in kwargs['json']['messages'][0]['content']:
                    started.wait(timeout=5)
            if neighbor:
                self.assertTrue(failed.wait(5))
                return batch_response(['邻句在途成功'])
            return batch_response([])
        with self.assertRaises(ToolError):
            self.translate(handler, progress=progress)
        cues = self.store.cues(self.job_id)
        self.assertFalse(any(c['translated'] for c in cues[:3]))
        self.assertTrue(all(c['translated'] == '邻句在途成功' for c in cues[3:6]))
        self.assertEqual(cues[6]['attempts'], 0)

    def test_diagnostic_io_failure_does_not_override_invalid_response(self):
        self.prepare()
        with mock.patch.object(Path, 'open', side_effect=OSError('private exception')):
            with self.assertRaises(ToolError) as error:
                self.translate(lambda *a, **k: batch_response([]))
        self.assertEqual(error.exception.code, 'mt_batch_invalid')
