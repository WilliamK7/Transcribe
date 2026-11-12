"""Behavior regressions for free-first routing and the merged pipeline."""
import threading
from unittest import mock
from types import SimpleNamespace

from subtitle_tool.mt_quality import make_stage_payload, validate_stage_value
from tests.mt_fixtures import PoolPipelineFixture
from tests.fixtures import result
from subtitle_tool.engine import Engine
from subtitle_tool.mt_pipeline import GlossaryPrefix, Pipeline


class TranslationWorkflowTests(PoolPipelineFixture):
    def test_translation_schema_targets_fragments_in_one_request(self):
        data = {'sentences': [{'id': '0', 'text': 'Do not disable the cache.'}],
                'fragments': [{'id': '0', 'sentence': '0', 'text': 'Do not disable'},
                              {'id': '1', 'sentence': '0', 'text': 'the cache.'}],
                'context': [], 'glossary': []}
        payload = make_stage_payload('translate', data, SimpleNamespace(model='group/transcribe'),
            capabilities={'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096})
        self.assertIn('fragments', payload['messages'][0]['content'])
        self.assertEqual(validate_stage_value('translate', {'translations': {'0': '不要禁用', '1': '缓存。'}},
            ids=['0', '1'], originals=['Do not disable', 'the cache.'])['translations']['0'], '不要禁用')

    def test_judge_can_dismiss_review_without_forcing_changes(self):
        value = {'issues': [], 'translations': {'0': '不要禁用缓存。'}}
        self.assertEqual(validate_stage_value('judge', value, ids=['0'],
            originals=['Do not disable the cache.']), value)

    def test_actual_model_collision_preserves_translation_and_marks_review_incomplete(self):
        self.install()
        self.actual_model = lambda stage, request: 'supplier/same-model'
        self.assertEqual(self.run_pool(), 'completed')
        self.assertTrue(all(c['translated'] for c in self.store.cues(self.job_id)))
        self.assertGreater(self.store.counts(self.job_id)['mt_quality_warning'], 0)
        self.assertEqual(sum(stage == 'review' for stage, _, _ in self.calls), 2)

    def test_group_alias_is_not_evidence_of_an_independent_reviewer(self):
        self.install()
        self.actual_model = lambda stage, request: request if stage == 'review' else 'synthetic/' + stage
        self.assertEqual(self.run_pool(), 'completed')
        self.assertGreater(self.store.counts(self.job_id)['mt_quality_warning'], 0)

    def test_reused_worker_keeps_glossary_after_judge_dismisses_review(self):
        self.config.settings.mt_workers = 1
        self.install_groups(10)
        self.review_issue = self.dismiss_review = True
        self.corrected_translations = lambda data: dict(data['translations'])
        self.glossary_terms = lambda data: [{'source': 'Cache', 'translation': '缓存',
                                            'sentence': data['sentences'][0]['id']}]
        self.assertEqual(self.run_pool(), 'completed')
        self.assertGreater(sum(s == 'glossary' for s, _, _ in self.calls), 1)
        self.assertGreater(sum(s == 'judge' for s, _, _ in self.calls), 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_glossary_unavailable', 0), 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_quality_warning', 0), 0)
        glossary_calls = sum(s == 'glossary' for s, _, _ in self.calls)
        self.assertEqual(glossary_calls, sum(row['stage'] == 'glossary' for row in self.store.mt_stages(self.job_id)))

    def assert_glossary_reuse_after_judgment(self, *, cache_only):
        self.install()
        self.review_issue = self.dismiss_review = True
        self.corrected_translations = lambda data: dict(data['translations'])
        self.glossary_terms = lambda data: [{'source': 'cache', 'translation': '缓存',
                                            'sentence': data['sentences'][0]['id']}]
        def exercise():
            engine = Engine(self.config, self.store)
            manifest = self.store.job(self.job_id)['manifest']
            _, scope = engine.identity(self.job_id, manifest, persist=False)
            pipeline = Pipeline(engine, self.job_id, manifest, scope)
            try:
                group = pipeline.plan_groups(self.store.sources(self.job_id), self.store.cues(self.job_id))[0]
                expected = pipeline.glossary(group, [])
                self.assertTrue(expected)
                pipeline.group(group, (), [])
                self.assertTrue(any(s == 'judge' for s, _, _ in self.calls))
                if cache_only:
                    with self.store.db:
                        self.store.db.execute("DELETE FROM mt_stages WHERE job_id=? AND stage='glossary'", (self.job_id,))
                before = len(self.calls)
                self.assertEqual(pipeline.glossary(group, []), expected)
                self.assertEqual(len(self.calls), before)
                self.assertEqual(self.store.counts(self.job_id).get('mt_glossary_unavailable', 0), 0)
            finally:
                pipeline.close()
        self.run_pool(run=exercise)

    def test_saved_glossary_remains_usable_after_independent_judgment(self):
        self.assert_glossary_reuse_after_judgment(cache_only=False)

    def test_cached_glossary_remains_usable_after_independent_judgment(self):
        self.assert_glossary_reuse_after_judgment(cache_only=True)

    def install_groups(self, count):
        raw = result(*[(f'Cache stores item {i}.', 'en') for i in range(count * 8)])
        for i, sentence in enumerate(raw['transcripts'][0]['sentences']):
            sentence['begin_time'], sentence['end_time'] = i * 100, i * 100 + 90
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id)

    def test_glossary_windows_keep_source_order_and_resume_exact_prefix(self):
        fit = Pipeline._fit
        def roomy(pipeline, stage, data, **kw):
            if stage == 'glossary':
                kw['output_budget'] = False
            return fit(pipeline, stage, data, **kw)
        patch = mock.patch.object(Pipeline, '_fit', roomy)
        patch.start()
        self.addCleanup(patch.stop)
        self.config.settings.mt_workers = 1
        self.install_groups(10)
        self.glossary_terms = lambda data: [{'source': 'Cache', 'translation': '缓存',
            'sentence': data['sentences'][-1]['id']}]
        self.cancel_after = 'translate'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        first = next(data for name, _, data in self.calls if name == 'translate')
        self.assertFalse(first['glossary'])  # The term is discovered in group 8.
        self.calls.clear()
        self.cancel_after = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        windows = [data for name, _, data in self.calls if name == 'glossary']
        self.assertEqual(len(windows), 1)  # First 8-group window reused.
        self.assertEqual(len(windows[0]['sentences']), 16)
        self.assertTrue(windows[0]['glossary'])
        translated = [data for name, _, data in self.calls if name == 'translate']
        self.assertTrue(all(not data['glossary'] for data in translated[:6]))
        self.assertTrue(all(data['glossary'] for data in translated[6:]))

    def test_cancel_during_prefix_consumption_keeps_whole_window(self):
        self.install_groups(3)
        engine = Engine(self.config, self.store)
        extend = GlossaryPrefix.extend
        def cancel(prefix, terms):
            engine.request_cancel()
            return extend(prefix, terms)
        with mock.patch.object(GlossaryPrefix, 'extend', cancel):
            self.assertEqual(self.run_pool(engine=engine), 'cancelled')
        self.assertEqual([s for s, _, _ in self.calls], ['glossary'])
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertNotIn('glossary', [s for s, _, _ in self.calls])

    def test_glossary_window_splits_to_input_budget(self):
        self.install_groups(4)
        fit = Pipeline._fit
        def limited(pipeline, stage, data, **kw):
            from subtitle_tool.common import ToolError
            if stage == 'glossary' and len(data['sentences']) > 16:
                raise ToolError('Synthetic window limit', 'mt_input_budget')
            return fit(pipeline, stage, data, **kw)
        with mock.patch.object(Pipeline, '_fit', limited):
            self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([len(d['sentences']) for s, _, d in self.calls if s == 'glossary'], [16, 16])
