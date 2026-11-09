"""Semantic input regressions with synthetic text, fake HTTP and real SQLite."""
import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_pipeline import GlossaryPrefix, Pipeline
from subtitle_tool.mt_quality import BatchValidationError, correction_stalled, term_evidence, validate_stage_value
from subtitle_tool.subtitles import normalize, source_context
from tests.fixtures import result, timed_raw
from tests.mt_fixtures import PoolPipelineFixture


class SemanticValidationTests(unittest.TestCase):
    def test_wire_context_uses_source_order_without_mutating_retention_priority(self):
        from subtitle_tool.mt_backend import MTBackend
        from subtitle_tool.mt_quality import make_stage_payload
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]), 0, '0000')[0]
        context = source_context(sources, 2)
        original = copy.deepcopy(context)
        data = {'sentences': [{'id': '0', 'text': sources[2]['text']}], 'context': context, 'glossary': []}
        backend = MTBackend('magpie', 'http://127.0.0.1:3425/v1', 'group/transcribe-free', 'synthetic')
        payload = make_stage_payload('draft', data, backend,
                                     capabilities={'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096})
        wire = json.loads(payload['messages'][1]['content'])['context']
        self.assertEqual([item['text'] for item in wire], [sources[i]['text'] for i in (0, 1, 3, 4)])
        self.assertEqual(context, original)

    def test_glossary_rejects_unknown_sentence_and_absent_or_partial_term(self):
        for term in [
                {'source': 'cache', 'translation': '缓存', 'sentence': 'future'},
                {'source': 'network', 'translation': '网络', 'sentence': 'current'},
                {'source': 'RAM', 'translation': '内存', 'sentence': 'current'}]:
            with self.subTest(term=term['source']), self.assertRaises(BatchValidationError):
                validate_stage_value('glossary', {'terms': [term]}, ids=['current'],
                                     originals=['The program uses a cache.'])

    def test_glossary_accepts_only_evidence_in_sent_target_slice(self):
        value = {'terms': [{'source': 'cache', 'translation': '緩存', 'sentence': 'same-source'}]}
        valid = validate_stage_value('glossary', value, ids=['same-source'], originals=['A cache stores results.'])
        self.assertEqual(valid['terms'][0]['translation'], '缓存')
        with self.assertRaises(BatchValidationError):
            validate_stage_value('glossary', value, ids=['same-source'], originals=['The next call reuses them.'])

    def test_context_change_invalidates_stalled_correction(self):
        previous = {'sentences': [], 'draft': {}, 'fragments': [{'id': '0', 'sentence': '0'}],
                    'glossary': [], 'translations': {'0': '原译文'},
                    'context': [{'position': 'before', 'text': 'The subject is electric charge.'}],
                    'issues': [{'id': '0', 'category': 'meaning', 'evidence': 'Synthetic evidence.'}]}
        changed = copy.deepcopy(previous)
        changed['context'][0]['text'] = 'The subject is a service charge.'
        self.assertFalse(correction_stalled(previous, changed))

    def test_group_context_reaches_both_edges_and_excludes_targets(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(10)]), 0, '0000')[0]
        for i, source in enumerate(sources):
            source.update(begin_ms=i * 5000, end_ms=i * 5000 + 1000)
        context = source_context(sources, 1, 8)
        self.assertEqual({item['text'] for item in context}, {sources[0]['text'], sources[9]['text']})
        self.assertTrue(all('source_position' in item for item in context))

    def test_prefix_retains_distinct_senses_with_evidence_and_manual_priority(self):
        sources = [
            {'sentence_id': 'a', 'text': 'Electric charge moves.', 'decision': 'translate'},
            {'sentence_id': 'b', 'text': 'A service charge applies.', 'decision': 'translate'}]
        def prefix(manual):
            pipeline = SimpleNamespace(engine=SimpleNamespace(mt_control=MTControl(threading.Event())),
                                       snapshot={'quality': {'manual_glossary': manual}})
            return GlossaryPrefix(pipeline, sources)
        first = {'source': 'charge', 'translation': '电荷', 'sentence': 'a', 'origin': 'model', 'evidence': sources[0]['text']}
        second = {'source': 'charge', 'translation': '费用', 'sentence': 'b', 'origin': 'model', 'evidence': sources[1]['text']}
        terms = prefix({})
        terms.extend([first])
        retained = terms.extend([second])
        self.assertEqual({term['translation'] for term in retained}, {'电荷', '费用'})
        self.assertTrue(all(term.get('evidence') for term in retained))
        overridden = prefix({'charge': '人工译法'}).extend([first, second])
        self.assertEqual({term['translation'] for term in overridden}, {'人工译法'})

    def test_term_evidence_uses_the_valid_whole_word_occurrence(self):
        text = 'The program runs. ' + 'Background detail. ' * 30 + 'RAM stores data.'
        self.assertIn('RAM stores data.', term_evidence('RAM', text))

    def test_long_source_context_keeps_coverage_without_thousands_of_budget_steps(self):
        from subtitle_tool.mt_context import sentence_context
        from subtitle_tool.subtitles import make_fragment
        source = normalize(timed_raw([f'word{i:05}' for i in range(10000)], word_ms=100), 0, '0000')[0][0]
        cue = make_fragment(source, 4995, 5005)
        context = sentence_context([{'source': source, 'text': cue['text'], 'cues': [cue]}])
        self.assertLess(len(context), 40)
        self.assertEqual(sum(item['required'] for item in context), 2)
        ordered = sorted(context, key=lambda item: item['char_start'])
        before = ''.join(item['text'] for item in ordered if item['position'] == 'before')
        after = ''.join(item['text'] for item in ordered if item['position'] == 'after')
        self.assertEqual(before + cue['text'] + after, source['text'])


class SemanticPipelineTests(PoolPipelineFixture):
    def setUp(self):
        super().setUp()
        self.config.settings.mt_workers = 1

    def test_alignment_review_and_corrections_receive_the_draft_context(self):
        raw = result(*[(f'Synthetic sentence {i}.', 'en') for i in range(8)])
        sentences = raw['transcripts'][0]['sentences']
        for i, sentence in enumerate(sentences):
            sentence.update(begin_time=i * 100, end_time=i * 100 + 90)
        sentences.extend(timed_raw(['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel'],
                                   word_ms=1000, start=1000)['transcripts'][0]['sentences'])
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id)
        self.review_issue = True
        self.assertEqual(self.run_pool(), 'completed')
        tail = [(stage, data) for stage, _, data in self.calls if len(data['sentences']) == 1]
        draft = next(data for stage, data in tail if stage == 'draft')
        self.assertTrue(draft['context'])
        self.assertTrue({'align', 'review', 'correct'} <= {stage for stage, _ in tail})
        for stage, data in tail:
            if stage in {'align', 'review', 'correct'}:
                self.assertEqual(data.get('context'), draft['context'])

    def test_split_requests_keep_neighboring_parts_and_explicit_target_ranges(self):
        self.save_result(timed_raw([f'word{i:03}' for i in range(400)], word_ms=20))
        Engine(self.config, self.store).reflow(self.job_id)
        manifest = self.store.job(self.job_id)['manifest']
        for entry in manifest['mt_route_snapshot']['entries']:
            entry['capabilities']['input_bytes'] = 4000
        self.store.update(self.job_id, manifest=manifest)
        self.assertEqual(self.run_pool(), 'completed')
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertGreater(len(drafts), 1)
        whole = self.store.sources(self.job_id)[0]['text']
        self.assertEqual(''.join(s['text'] for data in drafts for s in data['sentences']), whole)
        for data in drafts:
            target = data['sentences'][0]
            self.assertEqual(whole[target['char_start']:target['char_end']], target['text'])
            self.assertTrue(data['context'])
            for item in data['context']:
                self.assertEqual(item['sentence'], target['id'])
                self.assertEqual(item['text'], whole[item['char_start']:item['char_end']])
                self.assertTrue(item['char_end'] <= target['char_start'] or item['char_start'] >= target['char_end'])
            review = next(value for stage, _, value in self.calls
                          if stage == 'review' and value['sentences'] == data['sentences'])
            self.assertEqual(review['context'], data['context'])

    def test_bad_glossary_is_not_saved_or_used_by_later_groups(self):
        self.save_result(result(*[(f'Synthetic sentence {i}.', 'en') for i in range(8)], ('Quasar appears.', 'en')))
        Engine(self.config, self.store).reflow(self.job_id)
        future = self.store.sources(self.job_id)[-1]['sentence_id']
        self.glossary_terms = lambda data: [{'source': 'Quasar', 'translation': '类星体', 'sentence': future}]
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(self.store.counts(self.job_id).get('mt_glossary_unavailable'), 1)
        first = next(data for stage, _, data in self.calls if stage == 'draft')
        self.assertFalse(first['glossary'])
        saved = [row for row in self.store.mt_stages(self.job_id) if row['stage'] == 'glossary']
        self.assertEqual(len(saved), 1)

    def test_saved_glossary_and_cache_are_revalidated_against_request(self):
        self.install()
        self.glossary_terms = lambda data: [{'source': 'cache', 'translation': '缓存',
                                             'sentence': data['sentences'][0]['id']}]
        self.cancel_after = 'draft'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        row = next(item for item in self.store.mt_stages(self.job_id) if item['stage'] == 'glossary')
        cache_key, cached = next((key, json.loads(value)) for key, value in self.store.db.execute('SELECT key,text FROM mt_cache')
                                if json.loads(value)['value'] == row['record']['value'])
        invalid = {'terms': [{'source': 'unseen', 'translation': '不存在', 'sentence': 'future'}]}
        for storage in ('stage', 'cache'):
            with self.subTest(storage=storage), self.store.db:
                if storage == 'stage':
                    self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                          (json.dumps({**row['record'], 'value': invalid}), self.job_id, row['key']))
                else:
                    self.store.db.execute('DELETE FROM mt_stages WHERE job_id=? AND stage_key=?', (self.job_id, row['key']))
                    self.store.db.execute('UPDATE mt_cache SET text=? WHERE key=?',
                                          (json.dumps({**cached, 'value': invalid}), cache_key))
            self.calls.clear()
            self.cancel_after = None
            Engine(self.config, self.store).resume(self.job_id)
            self.assertEqual(self.run_pool(), 'failed')
            self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')
            self.assertEqual(self.calls, [])

    def test_budget_does_not_silently_remove_same_sentence_context(self):
        self.install()
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope)
        data = {'sentences': [{'id': '0', 'text': 'Target.'}], 'glossary': [],
                'context': [{'kind': 'same_sentence', 'required': True, 'text': 'Background. ' * 4000}]}
        with self.assertRaises(ToolError) as caught:
            pipeline._fit('draft', data)
        self.assertEqual(caught.exception.code, 'mt_input_budget')

    def test_context_survives_cancel_and_resume_without_retranslating_draft(self):
        self.save_result(result(*[(f'Synthetic sentence {i}.', 'en') for i in range(9)]))
        Engine(self.config, self.store).reflow(self.job_id)
        self.cancel_after = 'draft'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        draft = next(data for stage, _, data in self.calls if stage == 'draft')
        self.assertTrue(draft['context'])
        self.calls.clear()
        self.cancel_after = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        review = next(data for stage, _, data in self.calls if stage == 'review')
        self.assertEqual(review.get('context'), draft['context'])
        self.assertFalse(any(stage == 'draft' and data['sentences'] == draft['sentences']
                             for stage, _, data in self.calls))

    def test_tampered_semantic_input_stops_before_any_new_requests(self):
        self.install()
        self.cancel_after = 'draft'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        row = next(item for item in self.store.mt_stages(self.job_id) if item['stage'] == 'semantic_input')
        row['record']['context'] = [{'text': 'An unrelated instruction.'}]
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                  (json.dumps(row['record']), self.job_id, row['key']))
        self.calls.clear()
        self.cancel_after = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')
        self.assertEqual(self.calls, [])

    def test_failure_after_semantic_commit_reuses_committed_evidence(self):
        self.install()
        commit = self.store.commit_mt_stage
        def interrupted(job_id, key, stage, record, **kwargs):
            commit(job_id, key, stage, record, **kwargs)
            if stage == 'semantic_input':
                raise OSError('Synthetic interruption after commit')
        with mock.patch.object(self.store, 'commit_mt_stage', side_effect=interrupted):
            self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual([stage for stage, _, _ in self.calls], ['glossary'])
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([stage for stage, _, _ in self.calls], ['draft', 'review'])

    def test_trimmed_glossary_stays_saved_when_paid_permission_changes(self):
        from subtitle_tool.mt_backend import MTBackend
        from subtitle_tool.mt_quality import make_stage_payload
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        data = {'sentences': [{'id': 'source', 'text': 'A cache stores results.'}], 'manual_glossary': [],
                'context': [{'position': 'before', 'text': ('Background. ' * 60) + str(i)} for i in range(4)]}
        for entry, count in zip(manifest['mt_route_snapshot']['entries'], (1, 2)):
            payload = make_stage_payload('glossary', {**data, 'context': data['context'][:count]},
                                         MTBackend.from_snapshot(entry['backend']), capabilities=entry['capabilities'])
            entry['capabilities']['input_bytes'] = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        self.store.update(self.job_id, manifest=manifest)
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        # Construct inside the fake transport scope so no real HTTP can be reached.
        def run():
            pipeline = Pipeline(engine, self.job_id, manifest, scope)
            first = pipeline.stage('glossary', data)
            manifest['mt_route_snapshot']['routing']['paid_enabled'] = False
            pipeline = Pipeline(engine, self.job_id, manifest, scope)
            with mock.patch.object(pipeline.transport, 'request', side_effect=AssertionError('Unexpected repeated glossary')):
                second = pipeline.stage('glossary', data)
            self.assertEqual(first[0], second[0])
            self.assertEqual(first[2]['key'], second[2]['key'])
        self.run_pool(run=run)

    def test_split_sentence_term_evidence_comes_from_the_sent_slice(self):
        from subtitle_tool.mt_context import sentence_context
        from subtitle_tool.subtitles import make_fragment
        tokens = ['Electric', 'charge', 'moves.'] + ['Background.'] * 30 + ['A', 'service', 'charge', 'applies.']
        self.save_result(timed_raw(tokens, word_ms=200))
        Engine(self.config, self.store).reflow(self.job_id)
        sources = self.store.sources(self.job_id)
        source = sources[0]
        cue = make_fragment(source, len(tokens) - 4, len(tokens))
        unit = {'source': source, 'text': cue['text'], 'cues': [cue]}
        self.glossary_terms = lambda data: [{'source': 'charge', 'translation': '费用', 'sentence': data['sentences'][0]['id']}]
        def run():
            engine = Engine(self.config, self.store)
            manifest = self.store.job(self.job_id)['manifest']
            _, scope = engine.identity(self.job_id, manifest, persist=False)
            pipeline = Pipeline(engine, self.job_id, manifest, scope)
            prefix = GlossaryPrefix(pipeline, sources)
            terms = prefix.extend(pipeline.glossary([unit], sentence_context([unit])))
            self.assertIn('service charge', terms[0]['evidence'])
            self.assertNotIn('Electric charge', terms[0]['evidence'])
        self.run_pool(run=run)
