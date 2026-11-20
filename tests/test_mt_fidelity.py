"""Per-screen fidelity, independent terminology review and publication gates."""
import copy
import json
import unittest
from types import SimpleNamespace

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.mt_quality import (BatchValidationError, DEFAULT_CAPABILITIES, make_stage_payload,
                                      validate_stage_value, validate_judgment)
from tests.mt_fixtures import PoolPipelineFixture


def data():
    return {'sentences': [{'id': '0', 'text': 'The complex plane contains I1 and I2.'}],
            'fragments': [{'id': '0', 'sentence': '0', 'text': 'The complex plane contains I1'},
                          {'id': '1', 'sentence': '0', 'text': 'and I2.'}],
            'translations': {'0': '这个复杂平面包含 I。', '1': '以及 I₂。'}, 'context': [],
            'glossary': [{'source': 'complex plane', 'translation': '复杂平面', 'origin': 'model',
                          'sentence': '0', 'evidence': 'The complex plane contains I1 and I2.'},
                         {'source': 'I1', 'translation': 'I₁', 'origin': 'manual', 'sentence': '0', 'evidence': 'I1'}]}


class FidelityValidationTests(unittest.TestCase):
    def test_review_and_judge_do_not_receive_model_generated_term_translations(self):
        original = data()
        before = copy.deepcopy(original)
        for stage in ('translate', 'review', 'judge'):
            value = {**original, **({'issues': []} if stage == 'judge' else {})}
            payload = make_stage_payload(stage, value, SimpleNamespace(model='synthetic'),
                                         capabilities=DEFAULT_CAPABILITIES, fidelity=True)
            wire = json.loads(payload['messages'][1]['content'])
            self.assertEqual([t['origin'] for t in wire['glossary']],
                             ['model', 'manual'] if stage == 'translate' else ['manual'])
        self.assertEqual(original, before)

    def test_each_screen_requires_an_explicit_consistent_review_verdict(self):
        for value in ({'issues': []}, {'issues': [], 'checks': {'0': 'ok'}},
                      {'issues': [], 'checks': {'0': 'issue', '1': 'ok'}},
                      {'issues': [{'id': '0', 'category': 'meaning', 'evidence': '错义'}],
                       'checks': {'0': 'ok', '1': 'ok'}}):
            with self.subTest(value=value), self.assertRaises(BatchValidationError):
                validate_stage_value('review', value, ids=['0', '1'], fidelity=True)
        good = {'issues': [], 'checks': {'0': 'ok', '1': 'ok'}}
        self.assertEqual(validate_stage_value('review', good, ids=['0', '1'], fidelity=True), good)

    def test_source_uncertainty_is_not_permission_to_invent_a_formula(self):
        original = data()
        original['issues'] = [{'id': '0', 'category': 'source_uncertain', 'evidence': '运算关系缺失。'}]
        value = {'issues': original['issues'], 'translations': {**original['translations'], '0': '补写的公式。'}}
        with self.assertRaises(BatchValidationError):
            validate_judgment(value, original, fidelity=True)

    def test_indexed_symbols_are_checked_with_equivalent_written_and_spoken_forms(self):
        from subtitle_tool.mt_fidelity import source_anchors, anchor_checks
        originals = ['I one is equal to I2.', 'Map R2 to R2.', 'A room for one person.']
        anchors = source_anchors(originals)
        good = {'0': 'I₁ 等于 I₂。', '1': '把 R² 映到 R²。', '2': '供一人使用的房间。'}
        self.assertEqual(anchor_checks(good, anchors), [])
        bad = {**good, '0': 'I 等于 I₂。'}
        self.assertEqual([i['id'] for i in anchor_checks(bad, anchors)], ['0'])
        self.assertEqual(source_anchors(['x + y = z', 'The Lord of the Rings', 'S0 and s1.']), [[], [], ['s0', 's1']])
        self.assertEqual(source_anchors(['in 12 seconds, go to 30 rooms', 'I 1 equals R2.']), [[], ['i1', 'r2']])


class FidelityPipelineTests(PoolPipelineFixture):
    def test_review_outage_stops_publication_and_resume_only_retries_review(self):
        self.install()
        self.review_failure = True
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_quality_unresolved')
        self.assertFalse(self.source.with_suffix('.srt').exists())
        self.assertTrue(any(s['stage'] == 'translate' for s in self.store.mt_stages(self.job_id)))
        self.review_failure = False
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([s for s, _, _ in self.calls], ['review'])

    def test_unresolved_semantic_issue_is_not_published_as_a_completed_job(self):
        self.install()
        self.review_issue = self.no_change_correction = True
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_quality_unresolved')
        self.assertFalse(self.source.with_suffix('.srt').exists())

    def test_legacy_job_retains_advisory_only_publication(self):
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['execution_snapshot'].pop('fidelity_version', None)
        self.store.update(self.job_id, manifest=manifest)
        self.review_failure = True
        self.assertEqual(self.run_pool(), 'completed')
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_source_anchors_are_saved_and_rechecked_before_retime(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        stage = next(s for s in self.store.mt_stages(self.job_id) if s['stage'] == 'translate')
        self.assertIn('source_anchors', stage['record'])
        stage['record']['source_anchors'][0] = ['i9']
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                  (json.dumps(stage['record']), self.job_id, stage['key']))
        with self.assertRaises(ToolError) as caught:
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(caught.exception.code, 'cache_mismatch')

    def test_source_uncertainty_is_saved_without_rewriting_or_publishing(self):
        from unittest import mock
        self.install()
        from subtitle_tool.mt_pipeline import Pipeline
        invoke = Pipeline._invoke_stage

        def uncertain(pipeline, request, *args):
            if request.name == 'review':
                value = {'issues': [{'id': '0', 'category': 'source_uncertain', 'evidence': '公式关系缺失。'}],
                         'checks': {key: 'uncertain' if key == '0' else 'ok' for key in request.ids}}
                return {'value': value, 'actual_model': 'synthetic/reviewer', 'resolved_model': 'reviewer', 'usage': None}
            return invoke(pipeline, request, *args)

        with mock.patch.object(Pipeline, '_invoke_stage', uncertain):
            self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_quality_unresolved')
        self.assertFalse(any(s == 'judge' for s, _, _ in self.calls))
        self.assertFalse(self.source.with_suffix('.srt').exists())
        self.assertTrue(any(r['stage'] == 'review' for r in self.store.mt_stages(self.job_id)))
