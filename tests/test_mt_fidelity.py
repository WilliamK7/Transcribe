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
    def test_uncertainty_warning_only_uses_the_current_cue_production(self):
        from subtitle_tool.mt_pipeline import _source_uncertainty_warnings
        issue = {'id': '0', 'category': 'source_uncertain', 'evidence': 'private evidence'}
        translations = {'0': '合成译文。'}
        stages = [
            {'key': 'old-review', 'stage': 'review', 'record': {'stage': 'review', 'value': {'issues': [issue]}}},
            {'key': 'old-judge', 'stage': 'judge', 'record': {
                'value': {'issues': [issue], 'translations': translations}}},
            {'key': 'old-final', 'stage': 'final', 'record': {
                'warning': True, 'cue_ids': ['cue'], 'review_stage_keys': ['old-review'],
                'correction_stage_keys': ['old-judge'], 'value': {'translations': translations}}},
            {'key': 'new-final', 'stage': 'final', 'record': {'warning': False}},
        ]
        cue = {'cue_id': 'cue', 'begin_ms': 1000, 'end_ms': 2000, 'mt_stage_key': 'new-final'}
        self.assertEqual(_source_uncertainty_warnings(stages, [cue]), [])
        warnings = _source_uncertainty_warnings(stages, [{**cue, 'mt_stage_key': 'old-final'}])
        self.assertEqual(len(warnings), 1)
        self.assertIn('00:00:01,000', warnings[0])
        self.assertNotIn('private evidence', warnings[0])

    def test_uncertainty_warning_requires_the_judged_sentence_to_remain_unchanged(self):
        from subtitle_tool.mt_pipeline import _source_uncertainty_warnings
        uncertainty = {'id': '0', 'category': 'source_uncertain', 'evidence': 'private evidence'}
        issues = [uncertainty, {'id': '1', 'category': 'meaning', 'evidence': 'synthetic error'},
                  {'id': '2', 'category': 'meaning', 'evidence': 'synthetic error'}]
        translations = {'0': '保留疑点。', '1': '同句后半段。', '2': '另一句。'}
        cues = [{'cue_id': identity, 'begin_ms': i * 1000, 'end_ms': (i + 1) * 1000, 'mt_stage_key': 'final'}
                for i, identity in enumerate(['sentence:0', 'sentence:1', 'other:0'])]
        for changed_id, expected in [('1', 0), ('2', 1)]:
            current = {**translations, changed_id: '修正后译文。'}
            stages = [
                {'key': 'review-1', 'stage': 'review', 'record': {'stage': 'review', 'value': {'issues': issues}}},
                {'key': 'judge-1', 'stage': 'judge', 'record': {
                    'value': {'issues': issues, 'translations': translations}}},
                {'key': 'review-2', 'stage': 'review', 'record': {'stage': 'review', 'value': {'issues': issues[1:]}}},
                {'key': 'judge-2', 'stage': 'judge', 'record': {
                    'value': {'issues': issues[1:], 'translations': current}}},
                {'key': 'review-3', 'stage': 'review', 'record': {'stage': 'review', 'value': {'issues': [uncertainty]}}},
                {'key': 'final', 'stage': 'final', 'record': {
                    'warning': True, 'cue_ids': [cue['cue_id'] for cue in cues],
                    'review_stage_keys': ['review-1', 'review-2', 'review-3'],
                    'correction_stage_keys': ['judge-1', 'judge-2'], 'value': {'translations': current}}},
            ]
            with self.subTest(changed_id=changed_id):
                warnings = _source_uncertainty_warnings(stages, cues)
                self.assertEqual(len(warnings), expected)
                self.assertNotIn('private evidence', str(warnings))

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
    def test_warn_policy_publishes_complete_draft_with_locations_and_valid_provenance(self):
        self.config.settings.mt_quality = {'on_failure': 'warn', 'source_uncertain': 'warn'}
        self.install()
        self.review_issue = self.no_change_correction = True
        self.assertEqual(self.run_pool(), 'completed')
        final = next(s['record'] for s in self.store.mt_stages(self.job_id) if s['stage'] == 'final')
        self.assertTrue(final['warning'])
        self.assertEqual(final['warning_reason'], 'revision_cycle')
        warnings = self.store.job(self.job_id)['manifest']['warnings']
        self.assertTrue(any('时间' in w and '修订' in w for w in warnings))
        self.assertTrue(all(c['translated'] for c in self.store.cues(self.job_id)))
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_warn_policy_distinguishes_unreviewed_draft_and_still_rejects_missing_translation(self):
        self.config.settings.mt_quality = {'on_failure': 'warn'}
        self.install()
        self.review_failure = True
        self.assertEqual(self.run_pool(), 'completed')
        final = next(s['record'] for s in self.store.mt_stages(self.job_id) if s['stage'] == 'final')
        self.assertEqual(final['warning_reason'], 'review_unavailable')
        self.assertTrue(any('未完成复核' in w for w in self.store.job(self.job_id)['manifest']['warnings']))
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.config.settings.mt_policy_version += 1
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.review_failure = False
        self.draft_text = lambda fragment: ''
        self.assertEqual(self.run_pool(), 'failed')
        self.assertFalse(any(s['stage'] == 'final' for s in self.store.mt_stages(self.job_id)))

    def test_warning_publication_cannot_be_replayed_under_a_strict_snapshot(self):
        from subtitle_tool.mt_pipeline import validate_provenance
        self.config.settings.mt_quality = {'on_failure': 'warn'}
        self.install()
        self.review_failure = True
        self.assertEqual(self.run_pool(), 'completed')
        manifest = self.store.job(self.job_id)['manifest']
        manifest['mt_route_snapshot']['quality'].pop('on_failure')
        with self.assertRaises(ToolError):
            validate_provenance(Engine(self.config, self.store), self.job_id, manifest)

    def test_explicit_warning_resume_archives_policy_and_reuses_completed_stages(self):
        self.install()
        self.review_issue = self.no_change_correction = True
        self.assertEqual(self.run_pool(), 'failed')
        previous = self.store.mt_stages(self.job_id)
        engine = Engine(self.config, self.store)
        engine.resume(self.job_id, quality_failure='warn')
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['mt_route_snapshot']['quality']['on_failure'], 'warn')
        change = manifest['quality_policy_changes'][-1]
        archived = json.loads((self.config.root / change['snapshot']).read_text(encoding='utf-8'))
        self.assertEqual(archived['mt_stages'], previous)
        self.assertNotIn('on_failure', archived['job']['manifest']['mt_route_snapshot']['quality'])
        self.calls.clear()
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(self.calls, [])
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_warn_policy_keeps_stalled_judgment_provenance_when_retry_is_unavailable(self):
        self.config.settings.mt_quality = {'on_failure': 'warn'}
        self.install()
        self.review_issue = self.no_change_correction = True
        def unavailable_retry(stage, data):
            if data.get('repair_reason') == 'no_progress':
                self.fail_stage = 'judge'
        self.stage_hook = unavailable_retry
        self.assertEqual(self.run_pool(), 'completed')
        final = next(s['record'] for s in self.store.mt_stages(self.job_id) if s['stage'] == 'final')
        self.assertEqual(final['warning_reason'], 'judge_unavailable')
        self.assertEqual(final['production_stage_key'], final['correction_stage_keys'][-1])
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def uncertain_review(self):
        self.review_issues = lambda data: [
            {'id': '0', 'category': 'source_uncertain', 'evidence': 'private uncertain evidence'}]

    def test_authorized_uncertainty_needs_judgment_and_survives_retime_with_a_warning(self):
        self.config.settings.mt_quality = {'source_uncertain': 'warn'}
        self.install()
        self.no_change_correction = True
        self.uncertain_review()
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([s for s, _, _ in self.calls], ['glossary', 'translate', 'review', 'judge'])
        self.assertTrue(self.source.with_suffix('.srt').exists())
        warnings = self.store.job(self.job_id)['manifest']['warnings']
        self.assertTrue(any('source_uncertain' in w and '时间' in w for w in warnings))
        self.assertNotIn('private uncertain evidence', str(warnings))
        self.config.settings.mt_quality = None
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(self.run_pool(), 'completed')

    def test_uncertainty_option_cannot_bypass_a_judge_outage(self):
        self.config.settings.mt_quality = {'source_uncertain': 'warn'}
        self.install()
        self.fail_stage = 'judge'
        self.uncertain_review()
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_judge_unavailable')
        self.assertFalse(self.source.with_suffix('.srt').exists())

    def test_new_uncertainty_after_an_unrelated_correction_is_not_labeled_as_judged(self):
        self.config.settings.mt_quality = {'source_uncertain': 'warn', 'on_failure': 'warn'}
        self.install()
        reviews = []
        def review(data):
            reviews.append(data)
            new_uncertainty = len(reviews) > 1
            if new_uncertainty:
                self.fail_stage = 'judge'
            return [{'id': '1' if new_uncertainty else '0',
                     'category': 'source_uncertain' if new_uncertainty else 'meaning',
                     'evidence': 'private synthetic evidence'}]
        self.review_issues = review
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(len(reviews), 2)
        final = next(s['record'] for s in self.store.mt_stages(self.job_id) if s['stage'] == 'final')
        self.assertEqual(len(final['correction_stage_keys']), 1)
        self.assertEqual(final['warning_reason'], 'judge_unavailable')
        warnings = self.store.job(self.job_id)['manifest']['warnings']
        self.assertTrue(any('judge_unavailable' in warning for warning in warnings))
        self.assertFalse(any('独立裁决' in warning or 'source_uncertain' in warning for warning in warnings))
        self.assertNotIn('private synthetic evidence', str(warnings))
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_uncertainty_option_cannot_publish_unresolved_translation_errors(self):
        self.config.settings.mt_quality = {'source_uncertain': 'warn'}
        self.install()
        self.review_issue = self.no_change_correction = True
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_quality_unresolved')
        self.assertFalse(self.source.with_suffix('.srt').exists())

    def test_fixed_translation_errors_leave_previously_judged_source_uncertainty_publishable(self):
        self.config.settings.mt_quality = {'source_uncertain': 'warn'}
        self.install()
        reviews = []
        def review(data):
            reviews.append(data)
            return [{'id': '0', 'category': 'source_uncertain', 'evidence': 'private uncertainty'},
                    *([{'id': '1', 'category': 'addition', 'evidence': '合成错译'}] if len(reviews) <= 2 else [])]
        self.review_issues = review
        self.corrected_translations = lambda data: {**data['translations'], '1': '已修正。' + str(len(reviews))}
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(len(reviews), 3)
        self.assertEqual(sum(stage == 'judge' for stage, _, _ in self.calls), 2)
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_new_source_uncertainty_after_revision_limit_still_requires_read_only_judgment(self):
        self.config.settings.mt_quality = {'source_uncertain': 'warn'}
        self.install()
        reviews = []
        def review(data):
            reviews.append(data)
            final = len(reviews) == 3
            return [{'id': '0' if final else '1', 'category': 'source_uncertain' if final else 'addition',
                     'evidence': '合成疑点'}]
        self.review_issues = review
        self.corrected_translations = lambda data: (data['translations'] if len(reviews) == 3 else
            {**data['translations'], '1': '已修正。' + str(len(reviews))})
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(len(reviews), 3)
        self.assertEqual(sum(stage == 'judge' for stage, _, _ in self.calls), 3)
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_final_read_only_judge_cannot_publish_a_new_translation_issue(self):
        self.config.settings.mt_quality = {'source_uncertain': 'warn'}
        self.install()
        reviews = []
        def review(data):
            reviews.append(data)
            return [{'id': '0', 'category': 'source_uncertain' if len(reviews) == 3 else 'meaning',
                     'evidence': '合成疑点'}]
        self.review_issues = review
        self.judged_issues = lambda data: [{'id': '0', 'category': 'meaning', 'evidence': '仍需修正'}]
        self.corrected_translations = lambda data: (data['translations'] if len(reviews) == 3 else
            {**data['translations'], '0': '已修正。' + str(len(reviews))})
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_quality_unresolved')
        self.assertFalse(self.source.with_suffix('.srt').exists())
        self.assertFalse(any(s['stage'] == 'final' for s in self.store.mt_stages(self.job_id)))

    def test_uncertainty_option_rejects_a_tampered_warning_final(self):
        from subtitle_tool.mt_pipeline import validate_provenance
        self.config.settings.mt_quality = {'source_uncertain': 'warn'}
        self.install()
        self.no_change_correction = True
        self.uncertain_review()
        self.assertEqual(self.run_pool(), 'completed')
        judge = next(s for s in self.store.mt_stages(self.job_id) if s['stage'] == 'judge')
        judge['record']['value']['issues'][0]['category'] = 'meaning'
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                (json.dumps(judge['record']), self.job_id, judge['key']))
        with self.assertRaises(ToolError):
            validate_provenance(Engine(self.config, self.store), self.job_id, self.store.job(self.job_id)['manifest'])

    def test_review_outage_stops_publication_and_resume_only_retries_review(self):
        self.install()
        self.review_failure = True
        self.assertEqual(self.run_pool(), 'failed')
        failed = self.store.job(self.job_id)
        self.assertEqual(failed['error_code'], 'mt_review_unavailable')
        self.assertIn('http_permanent', failed['error'])
        self.assertIn('第 1 组', failed['error'])
        self.assertRegex(failed['request_id'], r'^[0-9a-f]{32}$')
        self.assertFalse(self.source.with_suffix('.srt').exists())
        self.assertTrue(any(s['stage'] == 'translate' for s in self.store.mt_stages(self.job_id)))
        self.review_failure = False
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([s for s, _, _ in self.calls], ['review'])

    def test_no_progress_judgment_gets_one_frozen_retry_before_publication(self):
        self.install()
        self.review_issue = self.no_change_correction = True
        retries = []
        def correct(data):
            if data.get('repair_reason') == 'no_progress':
                retries.append(data)
                self.review_issue = False
                return {**data['translations'], '0': '已修正的译文。'}
            return data['translations']
        self.corrected_translations = correct
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]['translations'], {'0': '完整句译文。', '1': '完整句译文。'})
        stages = self.store.mt_stages(self.job_id)
        self.assertEqual(sum(s['stage'] == 'judge' for s in stages), 2)
        final = next(s['record'] for s in stages if s['stage'] == 'final')
        self.assertFalse(final['warning'])
        self.assertEqual(len(final['correction_stage_keys']), 1)
        self.assertEqual(len(final['review_stage_keys']), 2)
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_no_progress_retry_remains_bounded_and_resumable(self):
        self.install()
        self.review_issue = self.no_change_correction = True
        self.assertEqual(self.run_pool(), 'failed')
        judgments = [data for stage, _, data in self.calls if stage == 'judge']
        self.assertEqual(len(judgments), 2)
        self.assertNotIn('repair_reason', judgments[0])
        self.assertEqual(judgments[1]['repair_reason'], 'no_progress')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_quality_unresolved')
        self.assertIn('revision_cycle', self.store.job(self.job_id)['error'])
        self.assertFalse(self.source.with_suffix('.srt').exists())
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.calls, [])

    def test_judge_outage_is_distinct_from_a_completed_negative_review(self):
        self.install()
        self.review_issue = True
        self.fail_stage = 'judge'
        self.assertEqual(self.run_pool(), 'failed')
        failed = self.store.job(self.job_id)
        self.assertEqual(failed['error_code'], 'mt_judge_unavailable')
        self.assertIn('http_permanent', failed['error'])
        self.assertFalse(self.source.with_suffix('.srt').exists())
        self.fail_stage = None
        self.dismiss_review = self.no_change_correction = True
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([stage for stage, _, _ in self.calls], ['judge'])

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
        self.install()
        self.uncertain_review()
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_quality_unresolved')
        self.assertFalse(any(s == 'judge' for s, _, _ in self.calls))
        self.assertFalse(self.source.with_suffix('.srt').exists())
        self.assertTrue(any(r['stage'] == 'review' for r in self.store.mt_stages(self.job_id)))
        failed = self.store.job(self.job_id)
        self.assertIn('source_uncertain', failed['error'])
        self.assertIn('第 1 组', failed['error'])
        self.assertIn('时间', failed['error'])
        self.assertIn('单独 --resume', failed['error'])
        self.assertNotIn('private uncertain evidence', failed['error'])
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.calls, [])
        self.assertIn('source_uncertain', self.store.job(self.job_id)['error'])

    def test_saved_warning_final_cannot_pass_current_fidelity_validation(self):
        from subtitle_tool.mt_pipeline import validate_provenance
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['execution_snapshot'].pop('fidelity_version')
        self.store.update(self.job_id, manifest=manifest)
        self.review_failure = True
        self.assertEqual(self.run_pool(), 'completed')
        manifest = self.store.job(self.job_id)['manifest']
        manifest['execution_snapshot']['fidelity_version'] = 2
        with self.assertRaises(ToolError) as caught:
            validate_provenance(Engine(self.config, self.store), self.job_id, manifest)
        self.assertEqual(caught.exception.code, 'cache_mismatch')
        self.store.update(self.job_id, manifest=manifest)
        self.config.settings.mt_quality = {'output_tokens': 16384, 'source_uncertain': 'warn'}
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        restored = self.store.job(self.job_id)['manifest']
        backup = json.loads((self.config.root / restored['reflow']['snapshot']).read_text(encoding='utf-8'))
        self.assertTrue(any(s['stage'] == 'final' and s['record']['warning'] for s in backup['mt_stages']))
        self.assertEqual(self.store.mt_stages(self.job_id), [])
        self.assertFalse(any(c['translated'] for c in self.store.cues(self.job_id)))
        self.review_failure = False
        self.assertEqual(self.run_pool(), 'completed')
