"""Output-aware grouping uses synthetic sources and the real private task store."""
import copy
import threading
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from tests.fixtures import result, timed_raw
from tests.mt_fixtures import PoolPipelineFixture


class OutputPlanningTests(PoolPipelineFixture):
    def install_many(self, *, output_tokens=512):
        self.config.settings.mt_workers = 1
        self.config.settings.mt_backends = [{'id': 'qiniu_free', 'capabilities': {'output_tokens': output_tokens}}]
        self.save_result(result(*[(f'Synthetic sentence {i} ' + 'explains a cache. ' * 8, 'en') for i in range(8)]))
        Engine(self.config, self.store).reflow(self.job_id)

    def test_small_output_budget_reduces_groups_before_any_request(self):
        self.install_many()
        def check_plan(stage, data):
            saved = self.store.job(self.job_id)['manifest']['mt_group_plan']
            self.assertEqual(saved['layout_round'], 0)
            self.assertGreater(len(saved['groups']), 1)
        self.stage_hook = check_plan
        self.assertEqual(self.run_pool(), 'completed')
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertGreater(len(drafts), 1)
        self.assertEqual([sentence['text'] for data in drafts for sentence in data['sentences']],
                         [source['text'] for source in self.store.sources(self.job_id)])
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))

    def test_unsplittable_target_is_preserved_despite_conservative_estimate(self):
        self.config.settings.mt_backends = [{'id': 'qiniu_free', 'capabilities': {'output_tokens': 32}}]
        text = 'An unsplittable synthetic sentence. ' * 20
        self.save_result(result((text, 'en')))
        Engine(self.config, self.store).reflow(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]['sentences'][0]['text'], text.strip())

    def test_long_sentence_uses_existing_reliable_cue_boundaries_without_losing_text(self):
        self.config.settings.mt_workers = 1
        self.config.settings.mt_backends = [{'id': 'qiniu_free', 'capabilities': {'output_tokens': 256}}]
        raw = timed_raw([f'cache{i:03}' for i in range(160)], word_ms=50)
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertGreater(len(drafts), 1)
        self.assertEqual(''.join(sentence['text'] for data in drafts for sentence in data['sentences']),
                         raw['transcripts'][0]['sentences'][0]['text'])

    def test_large_denied_paid_profile_does_not_hide_small_free_output_budget(self):
        self.install_many()
        self.config.settings.mt_backends.append({'id': 'qiniu_paid', 'capabilities': {'output_tokens': 32768}})
        Engine(self.config, self.store).reflow(self.job_id, mt_paid='deny')
        self.assertEqual(self.run_pool(), 'completed')
        self.assertGreater(sum(stage == 'draft' for stage, _, _ in self.calls), 1)
        self.assertTrue(all(data['_model'] == 'qiniu/conatus-free' for _, _, data in self.calls))

    def test_output_planning_uses_same_candidate_for_input_and_output_and_honors_global_cap(self):
        from subtitle_tool.mt_pipeline import Pipeline
        from subtitle_tool.subtitles import make_fragment
        self.install_many()
        self.config.settings.mt_backends = [
            {'id': 'qiniu_free', 'capabilities': {'input_bytes': 32768, 'output_tokens': 512}},
            {'id': 'qiniu_paid', 'tier': 'free', 'capabilities': {'input_bytes': 1, 'output_tokens': 32768}}]
        Engine(self.config, self.store).reflow(self.job_id)
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope)
        sources = self.store.sources(self.job_id)
        groups = pipeline.plan_groups(sources, [make_fragment(source) for source in sources])
        self.assertGreater(len(groups), 1)
        profile = pipeline.profiles['qiniu_free']
        profile['capabilities']['output_tokens'] = 32768
        data = {'sentences': [{'id': '0', 'text': 'Word ' * 1000}], 'context': [], 'glossary': []}
        self.assertEqual(pipeline._fit('draft', data), data)
        with self.assertRaises(ToolError) as error:
            pipeline._fit('draft', data, output_budget=True)
        self.assertEqual(error.exception.code, 'mt_input_budget')

    def test_expired_or_changed_large_candidate_cannot_supply_output_capacity(self):
        from subtitle_tool.mt_pipeline import Pipeline
        from subtitle_tool.subtitles import make_fragment
        for reason in ('expired', 'credential'):
            with self.subTest(reason=reason):
                self.install_many()
                self.config.settings.mt_backends = [
                    {'id': 'qiniu_free'}, {'id': 'openrouter_free', 'capabilities': {'output_tokens': 512}}]
                Engine(self.config, self.store).reflow(self.job_id)
                if reason == 'credential':
                    self.config.set_qiniu_key('changed-synthetic-key')
                manifest = self.store.job(self.job_id)['manifest']
                engine = Engine(self.config, self.store)
                _, scope = engine.identity(self.job_id, manifest, persist=False)
                sources = self.store.sources(self.job_id)
                pipeline = Pipeline(engine, self.job_id, manifest, scope)
                with mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=reason != 'expired'):
                    groups = pipeline.plan_groups(sources, [make_fragment(source) for source in sources])
                self.assertGreater(len(groups), 1)
                self.config.set_qiniu_key('synthetic-qiniu')

    def test_existing_current_contract_stages_without_plan_keep_original_group_targets(self):
        from subtitle_tool.mt_pipeline import Pipeline
        self.install_many()
        original_plan = Pipeline.plan_groups
        def input_only(pipeline, sources, cues, **kwargs):
            return original_plan(pipeline, sources, cues, output_budget=False)
        self.cancel_after = 'draft'
        with mock.patch.object(Pipeline, 'plan_groups', new=input_only):
            self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(len(manifest.pop('mt_group_plan')['groups']), 1)
        self.store.update(self.job_id, manifest=manifest)
        self.calls.clear()
        self.cancel_after = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([stage for stage, _, _ in self.calls], ['review'])
        self.assertEqual(len(self.store.job(self.job_id)['manifest']['mt_group_plan']['groups']), 1)

    def test_interrupt_before_or_after_group_plan_commit_preserves_confirmed_state(self):
        for after_commit in (False, True):
            self.install_many()
            saved = self.store.save_mt_group_plan
            def interrupted(job_id, manifest):
                if after_commit:
                    saved(job_id, manifest)
                raise OSError('Synthetic interruption')
            with self.subTest(after_commit=after_commit), \
                    mock.patch.object(self.store, 'save_mt_group_plan', side_effect=interrupted):
                self.assertEqual(self.run_pool(), 'failed')
            self.assertEqual('mt_group_plan' in self.store.job(self.job_id)['manifest'], after_commit)
            self.assertEqual(self.calls, [])
            self.assertFalse(self.store.has_mt_stages(self.job_id))
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')

    def test_plan_structure_is_validated_even_when_its_fingerprint_matches(self):
        from subtitle_tool.mt_planning import _fingerprint, restore_group_plan
        self.install_many()
        self.cancel_after = 'draft'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        sources, cues = self.store.sources(self.job_id), self.store.cues(self.job_id)
        original = self.store.job(self.job_id)['manifest']['mt_group_plan']
        for groups in ([[]], [original['groups'][0]], list(reversed(original['groups'])),
                       [original['groups'][0] * 2, *original['groups'][1:]]):
            saved = {**original, 'groups': groups, 'fingerprint': _fingerprint(sources, cues, groups, 0)}
            with self.assertRaises(ToolError) as error:
                restore_group_plan(saved, sources, cues, 0)
            self.assertEqual(error.exception.code, 'cache_mismatch')

    def test_saved_group_plan_does_not_cross_language_boundaries(self):
        from subtitle_tool.mt_planning import _fingerprint, restore_group_plan
        self.config.settings.mt_backends = [{'id': 'qiniu_free'}]
        self.save_result(result(('First sentence.', 'en'), ('Une autre phrase.', 'fr')))
        Engine(self.config, self.store).reflow(self.job_id)
        self.cancel_after = 'draft'
        self.config.settings.mt_workers = 1
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        sources, cues = self.store.sources(self.job_id), self.store.cues(self.job_id)
        original = self.store.job(self.job_id)['manifest']['mt_group_plan']
        groups = [[key for group in original['groups'] for key in group]]
        saved = {**original, 'groups': groups, 'fingerprint': _fingerprint(sources, cues, groups, 0)}
        with self.assertRaises(ToolError) as error:
            restore_group_plan(saved, sources, cues, 0)
        self.assertEqual(error.exception.code, 'cache_mismatch')

    def test_resume_uses_frozen_groups_and_saved_draft_without_replanning(self):
        from subtitle_tool.mt_pipeline import Pipeline
        self.install_many()
        self.cancel_after = 'draft'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        plan = self.store.job(self.job_id)['manifest']['mt_group_plan']
        self.cancel_after = None
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        with mock.patch.object(Pipeline, 'plan_groups', side_effect=AssertionError('Unexpected regrouping')):
            self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(self.store.job(self.job_id)['manifest']['mt_group_plan'], plan)
        self.assertEqual(sum(stage == 'draft' for stage, _, _ in self.calls), len(plan['groups']) - 1)

    def test_corrupted_saved_plan_stops_before_new_requests_or_changes_to_cues(self):
        self.install_many()
        self.cancel_after = 'draft'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        original = self.store.job(self.job_id)['manifest']
        self.assertIn('mt_group_plan', original)
        cues = self.store.cues(self.job_id)
        self.calls.clear()
        self.cancel_after = None
        for mutation in ('version', 'round', 'missing', 'duplicate', 'reordered', 'fingerprint'):
            manifest = copy.deepcopy(original)
            plan = manifest['mt_group_plan']
            if mutation == 'version':
                plan['version'] = True
            elif mutation == 'round':
                plan['layout_round'] += 1
            elif mutation == 'missing':
                plan['groups'].pop()
            elif mutation == 'duplicate':
                plan['groups'][0].append(plan['groups'][0][0])
            elif mutation == 'reordered':
                plan['groups'].reverse()
            else:
                plan['fingerprint'] = '0' * 64
            self.store.update(self.job_id, 'queued', manifest)
            with self.subTest(mutation=mutation):
                self.assertEqual(self.run_pool(), 'failed')
                self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')
                self.assertEqual(self.calls, [])
                self.assertEqual(self.store.cues(self.job_id), cues)

    def test_reflow_discards_old_group_plan_before_rebuilding(self):
        self.install_many()
        self.cancel_after = 'draft'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        self.assertIn('mt_group_plan', self.store.job(self.job_id)['manifest'])
        Engine(self.config, self.store).reflow(self.job_id)
        self.assertNotIn('mt_group_plan', self.store.job(self.job_id)['manifest'])

    def test_manual_language_review_clears_plan_and_dependent_stages_atomically(self):
        from subtitle_tool.review import ReviewDecision
        self.install_many()
        self.cancel_after = 'draft'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        before = self.store.job(self.job_id)['manifest']
        self.assertIn('mt_group_plan', before)
        self.store.update(self.job_id, 'needs_review')
        cue = self.store.cues(self.job_id)[0]
        with mock.patch.object(self.store, '_replace_records', side_effect=OSError()), self.assertRaises(OSError):
            self.store.apply_review(self.job_id, cue['cue_id'], ReviewDecision('original'))
        self.assertEqual(self.store.job(self.job_id)['manifest'], before)
        self.assertTrue(self.store.has_mt_stages(self.job_id))
        self.store.apply_review(self.job_id, cue['cue_id'], ReviewDecision('original'))
        self.assertNotIn('mt_group_plan', self.store.job(self.job_id)['manifest'])
        self.assertFalse(self.store.has_mt_stages(self.job_id))
