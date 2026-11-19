"""Content evidence boundaries, using fictional material and offline storage."""
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.mt_pipeline import Pipeline
from subtitle_tool.mt_quality import BatchValidationError, make_stage_payload, request_cache_key, validate_judgment
from tests.mt_fixtures import PoolPipelineFixture


def quality_data():
    return {'sentences': [
                {'id': '0', 'text': 'If the temperature exceeds 35 degrees, stop the pump.'},
                {'id': '1', 'text': 'The cache keeps 12 records.'},
                {'id': '2', 'text': 'The valve stays closed.'}],
            'fragments': [
                {'id': '0', 'sentence': '0', 'text': 'If the temperature exceeds 35 degrees,'},
                {'id': '1', 'sentence': '0', 'text': 'stop the pump.'},
                {'id': '2', 'sentence': '1', 'text': 'The cache keeps 12 records.'},
                {'id': '3', 'sentence': '2', 'text': 'The valve stays closed.'}],
            'translations': {'0': '如果温度低于35度，', '1': '停止水泵。', '2': '缓存保留12条记录。', '3': '阀门保持关闭。'},
            'issues': [{'id': '0', 'category': 'condition', 'evidence': '温度条件方向相反。'},
                       {'id': '2', 'category': 'number', 'evidence': '请核对记录数。'}],
            'context': [], 'glossary': []}


class ContentQualityTests(unittest.TestCase):
    def test_partial_dismissal_does_not_authorize_rewriting_the_dismissed_sentence(self):
        data = quality_data()
        value = {'issues': data['issues'][:1], 'translations': {**data['translations'],
                 '0': '如果温度超过35度，', '2': '缓存保留30条记录。'}}
        with self.assertRaises(BatchValidationError) as caught:
            validate_judgment(value, data)
        self.assertEqual(caught.exception.category, 'judge_scope')

    def test_judge_cannot_invent_new_scope_by_adding_an_unreviewed_sentence(self):
        data = quality_data()
        value = {'issues': [*data['issues'][:1], {'id': '3', 'category': 'meaning', 'evidence': '另一个问题。'}],
                 'translations': {**data['translations'], '0': '如果温度超过35度，', '3': '阀门保持打开。'}}
        with self.assertRaises(BatchValidationError) as caught:
            validate_judgment(value, data)
        self.assertEqual(caught.exception.category, 'judge_scope')

    def test_evidenced_same_sentence_fragments_can_be_corrected_together(self):
        data = quality_data()
        value = {'issues': data['issues'][:1], 'translations': {**data['translations'],
                 '0': '温度超过35度时，', '1': '应停止水泵。'}}
        self.assertEqual(validate_judgment(value, data), value)

    def test_review_receives_complete_sentence_translation_with_fragment_mapping(self):
        data = quality_data()
        before = copy.deepcopy(data)
        payload = make_stage_payload('review', data, SimpleNamespace(model='synthetic'),
            capabilities={'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096})
        wire = json.loads(payload['messages'][1]['content'])
        self.assertEqual(wire['sentences'][0]['fragment_ids'], ['0', '1'])
        self.assertEqual(wire['sentences'][0]['joined_translation'], '如果温度低于35度，\n停止水泵。')
        self.assertEqual(wire['sentences'][1]['fragment_ids'], ['2'])
        self.assertNotIn('joined_translation', wire['sentences'][1])
        self.assertEqual(data, before)


class ContentQualityPipelineTests(PoolPipelineFixture):
    def corrected_group(self):
        self.install()
        self.review_issue = True

        def fix_first(data):
            self.review_issue = False
            return {**data['translations'], '0': '缓存会保存结果。'}

        self.corrected_translations = fix_first
        self.assertEqual(self.run_pool(), 'completed')
        return next(row['record'] for row in self.store.mt_stages(self.job_id) if row['stage'] == 'final')

    def test_retime_checks_source_fingerprints_after_judgment(self):
        final = self.corrected_group()
        key = final['translation_stage_key']
        draft = self.store.mt_stage_entry(self.job_id, key)['record']
        self.assertEqual(len(set(draft['source_text_hashes'])), 2)
        draft['source_text_hashes'].reverse()
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                  (json.dumps(draft), self.job_id, key))
        with self.assertRaises(ToolError) as caught:
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(caught.exception.code, 'cache_mismatch')

    def test_retime_rejects_damaged_or_unresolved_last_review(self):
        final = self.corrected_group()
        self.assertEqual(len(final['review_stage_keys']), 2)
        self.assertFalse(final['warning'])
        key = final['review_stage_keys'][-1]
        record = self.store.mt_stage_entry(self.job_id, key)['record']
        target = Path(self.store.job(self.job_id)['output'])
        original = target.read_bytes()
        for value in ({'issues': None}, {'issues': [{'id': '0', 'category': 'meaning', 'evidence': '仍需核对'}]}):
            with self.subTest(value=value):
                with self.store.db:
                    self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                          (json.dumps({**record, 'value': value}), self.job_id, key))
                with self.assertRaises(ToolError) as caught:
                    Engine(self.config, self.store).retime(self.job_id, overwrite=True)
                self.assertEqual(caught.exception.code, 'cache_mismatch')
                self.assertEqual(target.read_bytes(), original)

    def test_saved_stage_and_request_cache_reject_out_of_scope_corrections(self):
        self.install()
        data = quality_data()
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope)
        self.addCleanup(pipeline.close)
        ids = list(data['translations'])
        originals = [part['text'] for part in data['fragments']]
        corrected = {**data['translations'], '0': '温度超过35度时，'}
        entry = pipeline.router_for('judge').entries[0]
        evidence = {'value': {'issues': data['issues'][:1], 'translations': {**corrected, '3': '无关改写。'}},
                    'actual_model': 'synthetic/judge', 'resolved_model': 'judge', 'usage': None}
        cache_key = request_cache_key('judge', data, entry.backend, capabilities=pipeline.profiles[entry.id]['capabilities'],
                                    scope=pipeline.cache_scope)
        stage_key = pipeline._task_stage_key('judge', data)
        with mock.patch.object(pipeline.transport, 'request', side_effect=AssertionError('Unexpected network')):
            for location in ('cache', 'saved'):
                if location == 'cache':
                    with self.store.db:
                        self.store.db.execute('INSERT INTO mt_cache VALUES(?, ?)', (cache_key, json.dumps(evidence)))
                else:
                    with self.store.db:
                        self.store.db.execute('DELETE FROM mt_cache WHERE key=?', (cache_key,))
                    self.store.commit_mt_stage(self.job_id, stage_key, 'judge', {
                        **evidence, 'stage': 'judge', 'backend': entry.id, 'request_model': entry.backend.model})
                with self.subTest(location=location), self.assertRaises(ToolError) as caught:
                    pipeline.stage('judge', data, ids=ids, originals=originals)
                self.assertEqual(caught.exception.code, 'cache_mismatch')

    def test_retime_rejects_a_judgment_whose_saved_review_no_longer_supports_it(self):
        final = self.corrected_group()
        job = self.store.job(self.job_id)
        output = Path(job['output']).read_bytes()
        review_key = final['review_stage_keys'][0]
        record = self.store.mt_stage_entry(self.job_id, review_key)['record']
        record['value']['issues'][0]['id'] = '1'
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                  (json.dumps(record), self.job_id, review_key))
        with self.assertRaises(ToolError) as caught:
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(caught.exception.code, 'cache_mismatch')
        self.assertEqual(self.store.job(self.job_id), job)
        self.assertEqual(Path(job['output']).read_bytes(), output)

    def test_unrelated_correction_is_rejected_and_never_published_or_cached(self):
        self.install()
        self.review_issue = True
        self.corrected_translations = lambda data: {**data['translations'], '1': '未经复核的额外改写。'}
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([cue['translated'] for cue in self.store.cues(self.job_id)], ['完整句译文。'] * 2)
        stages = self.store.mt_stages(self.job_id)
        self.assertFalse(any(row['stage'] == 'judge' for row in stages))
        final = next(row['record'] for row in stages if row['stage'] == 'final')
        self.assertTrue(final['warning'])
        self.assertEqual(final['production_stage_key'], final['translation_stage_key'])
        self.assertEqual(sum(stage == 'judge' for stage, _, _ in self.calls), 2)
        self.assertFalse(any('未经复核的额外改写。' in json.loads(row[0])['value'].get('translations', {}).values()
                             for row in self.store.db.execute('SELECT text FROM mt_cache')))

    def test_valid_fallback_correction_only_replaces_the_reviewed_sentence(self):
        self.install()
        self.review_issue = True

        def correction(data):
            if data['_model'] == self.config.settings.mt_judge_fallback_model:
                self.review_issue = False
                return {**data['translations'], '0': '已纠正的本句。'}
            return {**data['translations'], '0': '已纠正的本句。', '1': '无关改写。'}

        self.corrected_translations = correction
        self.assertEqual(self.run_pool(), 'completed')
        final = next(row['record'] for row in self.store.mt_stages(self.job_id) if row['stage'] == 'final')
        self.assertEqual(final['value']['translations'], {'0': '已纠正的本句。', '1': '完整句译文。'})
        self.assertFalse(final['warning'])
        self.assertEqual(final['backend'], 'magpie_judge_fallback')
        self.assertEqual(len(final['correction_stage_keys']), 1)
