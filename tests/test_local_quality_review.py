"""Local evidence remains actionable when the advisory reviewer is unavailable."""
import json
import threading

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from tests.fixtures import result
from tests.mt_fixtures import PoolPipelineFixture


class LocalQualityReviewTests(PoolPipelineFixture):
    repeated = '温度超过上限时整个测量系统会立即停止供电。'
    corrected = {'0': '电路提供恒定电压。', '1': '仪表测量电流。'}

    def prepare(self):
        self.config.settings.mt_workers = 1
        self.save_result(result(('The circuit supplies a constant voltage.', 'en'),
                                ('The meter measures the current.', 'en')))
        Engine(self.config, self.store).reflow(self.job_id)
        self.legacy_quality()
        self.draft_text = lambda fragment: self.repeated
        self.review_failure = True

    def final(self):
        return next(r['record'] for r in self.store.mt_stages(self.job_id) if r['stage'] == 'final')

    def test_reviewer_outage_does_not_suppress_local_repetition_judgment(self):
        self.prepare()
        def correction(data):
            self.review_failure = False
            return self.corrected
        self.corrected_translations = correction
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([stage for stage, _, _ in self.calls], ['glossary', 'translate', 'review', 'judge', 'review'])
        self.assertEqual([c['translated'] for c in self.store.cues(self.job_id)], list(self.corrected.values()))
        self.assertFalse(self.final()['warning'])
        self.assertEqual(len(self.final()['review_stage_keys']), 2)
        local = [r for r in self.store.mt_stages(self.job_id) if r['stage'] == 'local_review']
        self.assertEqual(len(local), 1)
        self.assertEqual({i['id'] for i in local[0]['record']['value']['issues']}, {'0', '1'})
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_local_dismissal_does_not_claim_a_complete_independent_review(self):
        self.prepare()
        self.dismiss_review = self.no_change_correction = True
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(sum(stage == 'judge' for stage, _, _ in self.calls), 1)
        self.assertTrue(self.final()['warning'])
        self.assertEqual([c['translated'] for c in self.store.cues(self.job_id)], [self.repeated] * 2)
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_judge_outage_preserves_local_evidence_and_the_complete_draft(self):
        self.prepare()
        self.fail_stage = 'judge'
        self.assertEqual(self.run_pool(), 'completed')
        self.assertTrue(self.final()['warning'])
        self.assertEqual(len(self.final()['review_stage_keys']), 1)
        self.assertEqual(self.final()['correction_stage_keys'], [])
        self.assertEqual([c['translated'] for c in self.store.cues(self.job_id)], [self.repeated] * 2)
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_resume_reuses_local_evidence_and_saved_correction_after_cancel(self):
        self.prepare()
        self.corrected_translations = lambda data: self.corrected
        self.cancel_after = 'judge'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        self.cancel_after = None
        self.review_failure = False
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([s for s, _, _ in self.calls], ['review'])
        self.assertEqual([c['translated'] for c in self.store.cues(self.job_id)], list(self.corrected.values()))
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_damaged_local_evidence_cannot_authorize_saved_corrections(self):
        self.prepare()
        self.corrected_translations = lambda data: self.corrected
        self.assertEqual(self.run_pool(), 'completed')
        local = next(r for r in self.store.mt_stages(self.job_id) if r['stage'] == 'local_review')
        local['record']['value']['issues'] = []
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                  (json.dumps(local['record']), self.job_id, local['key']))
        output = self.source.with_suffix('.srt')
        before = output.read_bytes()
        with self.assertRaises(ToolError) as caught:
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(caught.exception.code, 'cache_mismatch')
        self.assertEqual(output.read_bytes(), before)

    def test_local_evidence_does_not_allow_the_translation_model_to_judge_itself(self):
        self.prepare()
        original_model = self.actual_model
        self.actual_model = lambda stage, request: ('translate-model' if stage == 'judge'
                                                    else original_model(stage, request))
        self.corrected_translations = lambda data: self.corrected
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(self.final()['correction_stage_keys'], [])
        self.assertTrue(self.final()['warning'])
        self.assertEqual([c['translated'] for c in self.store.cues(self.job_id)], [self.repeated] * 2)

    def test_no_local_signal_does_not_invent_a_remote_review(self):
        self.prepare()
        self.draft_text = lambda fragment: self.corrected[fragment['id']]
        self.assertEqual(self.run_pool(), 'completed')
        self.assertFalse(any(s == 'judge' for s, _, _ in self.calls))
        self.assertEqual(self.final()['review_stage_keys'], [])
        self.assertTrue(self.final()['warning'])

    def test_legacy_task_keeps_original_failure_and_recovery_contract(self):
        self.prepare()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['execution_snapshot'].pop('local_review_version', None)
        self.store.update(self.job_id, manifest=manifest)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertFalse(any(s == 'judge' for s, _, _ in self.calls))
        self.assertTrue(self.final()['warning'])
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
