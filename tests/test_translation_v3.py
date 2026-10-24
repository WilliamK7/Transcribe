from __future__ import annotations

import copy
import json
import sqlite3
import threading
from unittest import mock

from subtitle_tool.api import Bailian
from subtitle_tool.common import Cancelled, ToolError, MT_INPUT_BUDGET
from subtitle_tool.engine import Engine
from subtitle_tool.mt import batch_cache_key, batch_input_size
from subtitle_tool.policy import load_policy
from subtitle_tool.subtitles import (make_fragment, normalize, plan_reference_units, plan_translation_batches,
                                     source_context, split_mt_units, validate_unit_coverage)
from tests.fixtures import FakeService, PipelineFixture, TemporaryTest, response, result, timed_raw


def batch_response(texts, *, finish='stop'):
    return response({'id': 'offline-batch', 'choices': [{'finish_reason': finish, 'message': {
        'content': json.dumps({'translations': [{'id': str(i), 'text': text}
                                               for i, text in enumerate(texts)]})}}]})


def batch_input(body):
    if len(body['messages']) == 2:
        targets = json.loads(body['messages'][1]['content'])['targets']
        return [{'id': key, 'text': item['text']} for key, item in targets.items()]
    content = body['messages'][0]['content']
    return json.loads(content.split('INPUT_JSON:\n', 1)[1])['translations']


class BatchProtocolTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.cancel = threading.Event()
        self.cloud = Bailian('offline-batch-key', 'beijing', self.cancel,
                             workspace_id='offline-space', access_mode='workspace')

    def test_joint_request_separates_context_and_preserves_real_repetition(self):
        fragments = ['Go,', 'go now.']
        context = [{'position': 'before', 'text': 'He repeated the instruction.'}]
        with mock.patch('requests.request', return_value=batch_response(['走，', '现在就走。'])) as request:
            translated = self.cloud.translate_batch(fragments, 'English', context=context).translations
        self.assertEqual(translated, {'0': '走，', '1': '现在就走。'})
        body = request.call_args.kwargs['json']
        self.assertEqual(len(body['messages']), 1)
        self.assertEqual(body['messages'][0]['role'], 'user')
        self.assertNotIn('translation_options', body)
        data = batch_input(body)
        self.assertIn('before: He repeated the instruction.', body['messages'][0]['content'])
        self.assertEqual(data, [{'id': str(i), 'text': t} for i, t in enumerate(fragments)])
        self.assertIn('源语言：English。', body['messages'][0]['content'])
        self.assertIn('真实重复', body['messages'][0]['content'])
        self.assertEqual(body['temperature'], 0)
        self.assertEqual(body['seed'], 0)
        self.assertIn('分段边界示例', body['messages'][0]['content'])

    def test_reference_omits_fragment_example_and_punctuation_only_is_not_a_translation(self):
        with mock.patch('requests.request', return_value=batch_response(['参考译文'])) as request:
            self.cloud.translate_batch(['A complete sentence.'])
        self.assertNotIn('分段边界示例', request.call_args.kwargs['json']['messages'][0]['content'])
        with mock.patch('requests.request', side_effect=lambda *a, **k: batch_response(['。'])) as request:
            with self.assertRaises(ToolError) as error:
                self.cloud.translate_batch(['Words with real content.'])
        self.assertEqual(error.exception.code, 'mt_batch_invalid')
        self.assertEqual(request.call_count, 2)

    def test_invalid_numbering_empty_text_and_truncation_repair_before_splitting(self):
        invalid = [[], [{'id': '0', 'text': '甲'}],
                   [{'id': '0', 'text': '甲'}, {'id': '0', 'text': '乙'}],
                   [{'id': '0', 'text': '甲'}, {'id': '1', 'text': '乙'}, {'id': '2', 'text': '丙'}],
                   [{'id': '0', 'text': '甲'}, {'id': '1', 'text': ' '}],
                   [{'id': 0, 'text': '甲'}, {'id': '1', 'text': '乙'}]]
        for items in invalid:
            with self.subTest(items=items):
                bad = response({'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps({'translations': items})}}]})
                with mock.patch('requests.request', side_effect=[bad, batch_response(['甲', '乙'])]) as request:
                    self.assertEqual(self.cloud.translate_batch(['first', 'second']).translations, {'0': '甲', '1': '乙'})
                self.assertEqual(request.call_count, 2)
        with mock.patch('requests.request', side_effect=lambda *a, **k: batch_response(['甲', '乙'], finish='length')) as request:
            with self.assertRaises(ToolError) as error:
                self.cloud.translate_batch(['first', 'second'])
        self.assertEqual(error.exception.code, 'mt_batch_invalid')
        self.assertEqual(request.call_count, 4)

    def test_malformed_json_duplicate_keys_and_unsafe_text_fail_without_raw_error(self):
        invalid = ['not JSON', '{"translations":', '{"translations":[],"translations":[]}',
                   '{"translations":[{"id":"0","text":"a","text":"b"}]}',
                   '{"translations":[{"id":"0","text":"-->"}]}']
        for content in invalid:
            with self.subTest(content=content):
                def bad(*args, **kwargs):
                    return response({'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})
                with mock.patch('requests.request', side_effect=bad) as request, self.assertRaises(ToolError) as error:
                    self.cloud.translate_batch(['sample'])
                self.assertEqual(request.call_count, 2)
                self.assertEqual(error.exception.code, 'mt_batch_invalid')
                self.assertNotIn(content, str(error.exception))

    def test_exact_prompt_and_context_budget_checked_before_network(self):
        with mock.patch('requests.request') as request, self.assertRaises(ToolError) as error:
            self.cloud.translate_batch(['x' * 6000])
        request.assert_not_called()
        self.assertEqual(error.exception.code, 'mt_input_budget')


class BatchPlanningTests(TemporaryTest):
    def test_boundaries_keep_language_review_and_five_second_gap(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(7)]), 0, '0000', preserve_sentences=True)[0]
        self.assertEqual(len(source_context(sources, 3)), 4)
        for changes in [{'language': 'fr'}, {'source_lang': 'French'}, {'decision_origin': 'manual'},
                        {'decision': 'keep'}, {'begin_ms': 9001, 'end_ms': 10000}]:
            with self.subTest(changes=changes):
                changed = copy.deepcopy(sources)
                changed[4].update(changes)
                self.assertTrue(all(c['position'] == 'before' for c in source_context(changed, 3)))
        changed = copy.deepcopy(sources)
        changed[4].update(begin_ms=8900, end_ms=9900)  # exactly 5000ms from the previous end
        self.assertTrue(any(c['position'] == 'after' for c in source_context(changed, 3)))
        unknown = copy.deepcopy(sources)
        for source in unknown:
            source['language'] = ''
        self.assertEqual(source_context(unknown, 3), [])

    def test_budget_removes_far_context_then_groups_contiguous_fragments(self):
        source = normalize(timed_raw([chr(97 + i) * 600 for i in range(8)], word_ms=1100),
                           0, '0000', preserve_sentences=True)[0][0]
        units = [make_fragment(source, i, i + 1) for i in range(8)]
        neighbors = [{'position': 'before', 'text': 'near'}, {'position': 'after', 'text': 'far' * 3000}]
        groups = plan_translation_batches(units[:2], neighbors)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][1], neighbors[:1])
        groups = plan_translation_batches(units, neighbors)
        self.assertGreater(len(groups), 1)
        self.assertEqual([u['cue_id'] for members, _ in groups for u in members], [u['cue_id'] for u in units])
        for members, context in groups:
            self.assertLessEqual(batch_input_size([u['text'] for u in members], context=context), MT_INPUT_BUDGET)
            if context:
                self.assertTrue(context[0]['position'].startswith('same_sentence_'))
        validate_unit_coverage(source, [u for members, _ in groups for u in members])

    def test_single_fragment_splits_at_real_word_times_or_returns_budget_error(self):
        source = normalize(timed_raw(['a' * 900] * 8, word_ms=1100), 0, '0000', preserve_sentences=True)[0][0]
        units = split_mt_units(source, [make_fragment(source)])
        self.assertGreater(len(units), 1)
        validate_unit_coverage(source, units)
        endpoints = {w[k] for w in source['words'] for k in ['begin_ms', 'end_ms']}
        self.assertTrue(all(u['begin_ms'] in endpoints and u['end_ms'] in endpoints for u in units))
        self.assertTrue(all(u['end_ms'] - u['begin_ms'] >= 1000 for u in units))
        references = plan_reference_units([source], strategy_version=3)
        validate_unit_coverage(source, references, reference_stage=True)
        source['words'] = []
        with self.assertRaises(ToolError) as error:
            split_mt_units(source, [make_fragment(source)])
        self.assertEqual(error.exception.code, 'mt_input_budget')

    def test_cache_includes_context_order_grouping_scope_model_and_prompt_version(self):
        context = [{'position': 'before', 'text': 'source context'}]
        base = batch_cache_key(['first', 'second'], context=context, account='a')
        variants = [batch_cache_key(['first', 'second'], context=[], account='a'),
                    batch_cache_key(['second', 'first'], context=context, account='a'),
                    batch_cache_key(['first second'], context=context, account='a'),
                    batch_cache_key(['first', 'second'], context=context, account='b'),
                    batch_cache_key(['first', 'second'], 'English', context=context, account='a')]
        with mock.patch('subtitle_tool.mt.PROMPT_VERSION', 99):
            variants.append(batch_cache_key(['first', 'second'], context=context, account='a'))
        with mock.patch('subtitle_tool.mt.MT_MODEL', 'another-model'):
            variants.append(batch_cache_key(['first', 'second'], context=context, account='a'))
        self.assertTrue(all(value != base for value in variants))


class ContextTranslationTests(PipelineFixture):
    fixture_seconds = 60

    def prepare_sources(self, raw):
        sources = normalize(raw, 0, '0000', preserve_sentences=True)[0]
        manifest = self.store.job(self.job_id)['manifest']
        self.store.initialize_subtitles(self.job_id, sources, [make_fragment(s) for s in sources], manifest)
        self.scope = self.engine.identity(self.job_id, manifest)[1]
        return sources

    @property
    def engine(self):
        return Engine(self.config, self.store)

    def translate(self, sources, handler, *, cancel=None, progress=lambda *args: None):
        engine = Engine(self.config, self.store, cancel, progress)
        client = lambda: Bailian('offline-key', 'beijing', engine.cancel,
                                workspace_id='offline-space', access_mode='workspace',
                                count=lambda name: self.store.count(self.job_id, name))
        with mock.patch('requests.request', side_effect=handler):
            policy = load_policy(self.store.job(self.job_id)['manifest'])
            engine.translate_batches(self.job_id, self.store.cues(self.job_id), sources, self.scope, client,
                                     context_version=policy.context_version)

    def test_same_sentence_fragments_use_one_request_without_parent_translation(self):
        sources = self.prepare_sources(timed_raw(['The', 'charges', 'move', 'through', 'the', 'wire.'], word_ms=1000))
        source = sources[0]
        cues = [make_fragment(source, 0, 3), make_fragment(source, 3, 6)]
        self.store.save_layout(self.job_id, cues, self.store.job(self.job_id)['manifest'])
        def handler(method, url, **kwargs):
            data = batch_input(kwargs['json'])
            self.assertEqual([f['text'] for f in data], [c['text'] for c in cues])
            self.assertNotIn('translation_options', kwargs['json'])
            return batch_response(['电荷运动', '穿过导线。'])
        request = mock.Mock(side_effect=handler)
        self.translate(sources, request)
        self.assertEqual(request.call_count, 1)
        self.assertEqual([c['translated'] for c in self.store.cues(self.job_id)], ['电荷运动', '穿过导线。'])

    def test_isolated_short_sentence_gets_source_context_only(self):
        sources = self.prepare_sources(result(('We connect the wire.', 'en'), ('The conductor.', 'en'),
                                               ('Charge flows through it.', 'en')))
        observed = []
        def handler(method, url, **kwargs):
            data = batch_input(kwargs['json'])
            if data[0]['text'] == 'The conductor.':
                observed.append(kwargs['json']['messages'][0]['content'].split('\n\n', 1)[0])
            return batch_response(['模拟译文'] * len(data))
        self.translate(sources, handler)
        self.assertEqual(len(observed), 1)
        self.assertIn('We connect the wire.', observed[0])
        self.assertIn('Charge flows through it.', observed[0])

    def test_transaction_failure_rolls_back_distinct_results_cache_and_counts(self):
        sources = self.prepare_sources(timed_raw(['alpha', 'bravo', 'charlie', 'delta'], word_ms=1000))
        cues = [make_fragment(sources[0], 0, 2), make_fragment(sources[0], 2, 4)]
        self.store.save_layout(self.job_id, cues, self.store.job(self.job_id)['manifest'])
        self.store.db.execute("CREATE TEMP TRIGGER reject_batch BEFORE UPDATE OF translated ON cues "
                              "WHEN NEW.translated='乙' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.translate(sources, lambda *a, **k: batch_response(['甲', '乙']))
        self.assertFalse(any(c['translated'] for c in self.store.cues(self.job_id)))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_subtitle_completed', 0), 0)

    def test_bad_group_has_no_cache_and_cancelled_success_is_saved(self):
        sources = self.prepare_sources(result(('First sentence.', 'en'), ('Second sentence.', 'en')))
        self.config.settings.mt_workers = 1
        with self.assertRaises(ToolError):
            self.translate(sources, lambda *a, **k: batch_response([]))
        self.assertFalse(any(c['translated'] for c in self.store.cues(self.job_id)))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)
        cancel = threading.Event()
        def success(*args, **kwargs):
            cancel.set()
            return batch_response(['完成一组'])
        with self.assertRaises(Cancelled):
            self.translate(sources, success, cancel=cancel)
        self.assertTrue(self.store.cues(self.job_id)[0]['translated'])
        self.assertFalse(self.store.cues(self.job_id)[1]['translated'])
        retry = mock.Mock(side_effect=lambda *a, **k: batch_response(['补齐']))
        self.translate(sources, retry)
        self.assertEqual(retry.call_count, 1)

    def test_repair_attempts_count_for_all_group_members(self):
        sources = self.prepare_sources(timed_raw(['alpha', 'bravo', 'charlie', 'delta'], word_ms=1000))
        cues = [make_fragment(sources[0], 0, 2), make_fragment(sources[0], 2, 4)]
        self.store.save_layout(self.job_id, cues, self.store.job(self.job_id)['manifest'])
        responses = iter([batch_response(['missing']), batch_response(['甲', '乙'])])
        self.translate(sources, lambda *a, **k: next(responses))
        self.assertEqual([c['attempts'] for c in self.store.cues(self.job_id)], [2, 2])
        counts = self.store.counts(self.job_id)
        self.assertEqual((counts['mt'], counts['mt_subtitle_request'], counts['mt_subtitle_completed']), (2, 2, 2))

    def test_identical_batches_share_one_request_and_restore_missing_record_from_complete_cache(self):
        raw = timed_raw(['alpha', 'bravo', 'charlie', 'delta'], word_ms=750)
        template = raw['transcripts'][0]['sentences'][0]
        sentences = [copy.deepcopy(template) for _ in range(4)]
        for i, sentence in enumerate(sentences):
            for item in [sentence, *sentence['words']]:
                # A sixteen-second gap keeps each request's context empty.
                item['begin_time'] += i * 19000
                item['end_time'] += i * 19000
        raw['transcripts'][0]['sentences'] = sentences
        sources = self.prepare_sources(raw)
        cues = [cue for source in sources for cue in [make_fragment(source, 0, 2), make_fragment(source, 2, 4)]]
        self.store.save_layout(self.job_id, cues, self.store.job(self.job_id)['manifest'])
        request = mock.Mock(side_effect=lambda *a, **k: batch_response(['前半句', '后半句。']))
        self.translate(sources, request)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.store.counts(self.job_id)['mt_subtitle_completed'], 8)
        cues = self.store.cues(self.job_id)
        with self.store.db:
            self.store.db.execute("UPDATE cues SET translated='' WHERE job_id=? AND cue_id=?",
                                  (self.job_id, cues[0]['cue_id']))
        no_network = mock.Mock(side_effect=AssertionError('cache should restore full mapping'))
        progress = mock.Mock()
        self.translate(sources, no_network, progress=progress)
        no_network.assert_not_called()
        self.assertEqual(self.store.counts(self.job_id)['mt_subtitle_cache_hit'], 2)
        restored = self.store.cues(self.job_id)
        self.assertEqual([c['translated'] for c in restored], ['前半句', '后半句。'] * 4)
        self.assertEqual([c['attempts'] for c in restored], [1] * 8)
        self.assertEqual(progress.call_args.args[-2:], (8, 8))

        # The same partially populated group must also recover without a cache hit.
        with self.store.db:
            self.store.db.execute('DELETE FROM mt_cache')
        with self.store.db:
            self.store.db.execute("UPDATE cues SET translated='' WHERE job_id=? AND cue_id=?",
                                  (self.job_id, cues[0]['cue_id']))
        request.reset_mock()
        self.translate(sources, request, progress=progress)
        self.assertEqual(request.call_count, 1)
        self.assertEqual([c['attempts'] for c in self.store.cues(self.job_id)], [2, 2] + [1] * 6)
        self.assertEqual(progress.call_args.args[-2:], (8, 8))

    def test_context_and_group_changes_miss_previous_complete_batch_cache(self):
        raw = timed_raw(['alpha', 'bravo', 'charlie', 'delta'], word_ms=1000)
        raw['transcripts'][0]['sentences'].append({'begin_time': 4000, 'end_time': 4900,
                                                  'text': 'Old context.', 'language': 'en'})
        sources = self.prepare_sources(raw)
        handler = mock.Mock(side_effect=lambda *a, **k: batch_response(
            ['译文'] * len(batch_input(k['json']))))
        self.translate(sources, handler)
        handler.reset_mock()
        source = sources[0]
        cues = [make_fragment(source, 0, 2), make_fragment(source, 2, 4), self.store.cues(self.job_id)[-1]]
        self.store.save_layout(self.job_id, cues, self.store.job(self.job_id)['manifest'])
        self.translate(sources, handler)
        self.assertEqual(handler.call_count, 1)
        handler.reset_mock()
        changed = copy.deepcopy(sources)
        changed[1]['text'] = 'Changed context.'
        # Only inspect missing fragments of the first source; keep the neighbor complete and valid.
        neighbor = make_fragment(changed[1])
        neighbor['translated'] = '已完成邻句'
        self.store.save_layout(self.job_id, [make_fragment(source, 0, 2), make_fragment(source, 2, 4), neighbor],
                               self.store.job(self.job_id)['manifest'])
        self.translate(changed, handler)
        self.assertEqual(handler.call_count, 1)
        self.assertIn('after: Changed context.', handler.call_args.kwargs['json']['messages'][0]['content'])

    def test_failed_batch_stops_scheduling_and_keeps_inflight_distinct_successes(self):
        sources = self.prepare_sources(result(*[(f'Sentence {i}.', 'en') for i in range(5)]))
        self.config.settings.mt_workers = 4
        barrier, saved_failure = threading.Barrier(4), threading.Event()
        def progress(*args):
            if self.store.cues(self.job_id)[0]['error']:
                saved_failure.set()
        def handler(method, url, **kwargs):
            data = batch_input(kwargs['json'])
            barrier.wait(timeout=5)
            if data[0]['text'] == 'Sentence 0.':
                return response({'code': 'Unauthorized'}, 403)
            self.assertTrue(saved_failure.wait(5))
            return batch_response(['在途成功'])
        request = mock.Mock(side_effect=handler)
        with self.assertRaises(ToolError):
            self.translate(sources, request, progress=progress)
        self.assertEqual(request.call_count, 4)
        cues = self.store.cues(self.job_id)
        self.assertFalse(cues[0]['translated'])
        self.assertTrue(all(c['translated'] for c in cues[1:4]))
        self.assertEqual(cues[4]['attempts'], 0)
        retry = mock.Mock(side_effect=lambda *a, **k: batch_response(['补齐']))
        self.translate(sources, retry)
        self.assertEqual(retry.call_count, 2)

    def test_counter_failure_rolls_back_results_and_complete_cache(self):
        sources = self.prepare_sources(result(('Atomic sentence.', 'en')))
        self.store.db.execute("CREATE TEMP TRIGGER reject_batch_count BEFORE INSERT ON counters "
                              "WHEN NEW.name='mt_subtitle_completed' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.translate(sources, lambda *a, **k: batch_response(['应回滚']))
        self.assertFalse(self.store.cues(self.job_id)[0]['translated'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mt_cache').fetchone()[0], 0)

    def test_append_split_retranslates_all_ranges_of_affected_source_and_only_twice(self):
        raw = timed_raw(['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel'], word_ms=1000)
        raw['transcripts'][0]['sentences'].append({'begin_time': 8000, 'end_time': 8900, 'text': 'Neighbor.', 'language': 'en'})
        manifest = self.store.job(self.job_id)['manifest']
        manifest['subtitle_settings']['zh_line_width'] = 12
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(raw)
        original = fake.request
        seen = []
        def handler(method, url, **kwargs):
            if not url.endswith('/chat/completions'):
                return original(method, url, **kwargs)
            data = batch_input(kwargs['json'])
            fragments = [f['text'] for f in data]
            seen.append(fragments)
            # Only the left fragment overflows. Its unchanged right sibling must be retranslated too.
            return batch_response(['中' * 30 if len(fragments) > 1 and f.startswith('alpha') else '短译文'
                                   for f in fragments])
        with mock.patch('requests.request', side_effect=handler), mock.patch('requests.post', side_effect=fake.upload):
            self.assertEqual(self.engine.run(self.job_id), 'completed')
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['layout_round'], 2)
        self.assertEqual(sum(f == ['Neighbor.'] for f in seen), 1)
        groups = [f for f in seen if len(f) > 1]
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(g[-1] == groups[0][-1] for g in groups))
        self.assertEqual([len(g) for g in groups], [2, 3, 4])
        source = self.store.sources(self.job_id)[0]
        validate_unit_coverage(source, [c for c in self.store.cues(self.job_id) if c['sentence_id'] == source['sentence_id']])

    def test_strategy_two_resume_then_explicit_reflow_uses_three_without_asr(self):
        manifest = self.store.job(self.job_id)['manifest']
        manifest['translation_strategy_version'] = 2
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(result(('An old saved sentence.', 'en')))
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(self.store.job(self.job_id)['manifest']['translation_strategy_version'], 2)
        old = self.source.with_suffix('.srt').read_bytes()
        self.engine.reflow(self.job_id, overwrite=True)
        self.assertEqual(self.store.job(self.job_id)['manifest']['translation_strategy_version'], 3)
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(len(fake.named('/chat/completions')), 1)
        self.assertEqual(fake.named('/asr/transcription'), [])
        self.assertEqual(len(fake.uploads), 1)
        self.assertEqual(self.source.with_suffix('.srt').read_bytes(), old)

    def test_interrupted_append_transaction_keeps_all_affected_ranges_pending(self):
        raw = timed_raw(['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel'], word_ms=1000)
        raw['transcripts'][0]['sentences'].append({'begin_time': 8000, 'end_time': 8900,
                                                  'text': 'Neighbor.', 'language': 'en'})
        manifest = self.store.job(self.job_id)['manifest']
        manifest['subtitle_settings']['zh_line_width'] = 12
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(raw)
        original = fake.request
        def handler(method, url, **kwargs):
            if not url.endswith('/chat/completions'):
                return original(method, url, **kwargs)
            fragments = [f['text'] for f in batch_input(kwargs['json'])]
            return batch_response(['中' * 30 if len(fragments) > 1 and f.startswith('alpha') else '短译文'
                                   for f in fragments])
        save = self.store.save_layout
        def interrupt(job_id, cues, manifest, **kwargs):
            save(job_id, cues, manifest, **kwargs)
            if manifest.get('layout_round') == 1:
                raise KeyboardInterrupt()
        with mock.patch('requests.request', side_effect=handler), mock.patch('requests.post', side_effect=fake.upload):
            with mock.patch.object(self.store, 'save_layout', side_effect=interrupt):
                self.assertEqual(self.engine.run(self.job_id), 'cancelled')
            cues = self.store.cues(self.job_id)
            source = self.store.sources(self.job_id)[0]
            self.assertTrue(all(not c['translated'] for c in cues if c['sentence_id'] == source['sentence_id']))
            self.assertTrue(cues[-1]['translated'])
            self.assertFalse(self.source.with_suffix('.srt').exists())
            self.store.update(self.job_id, 'queued')
            self.assertEqual(self.engine.run(self.job_id), 'completed')
