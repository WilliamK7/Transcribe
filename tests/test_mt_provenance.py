"""Subtitle production evidence and interruption recovery."""
import copy
import json
from pathlib import Path
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.mt_quality import PROMPTS
from tests.fixtures import FakeService, SavedResultFixture, response, result, timed_raw


class ProductionService(FakeService):
    def __init__(self):
        super().__init__(result())
        self.stages = []
        self.cancel_stage = None
        self.cancel = None
        self.review_issue = False
        self.fail_review = False

    def request(self, method, url, **kwargs):
        if not url.endswith('/chat/completions'):
            return super().request(method, url, **kwargs)
        payload = kwargs['json']
        stage = next(name for name, prompt in PROMPTS.items()
                     if payload['messages'][0]['content'].startswith(prompt))
        data = json.loads(payload['messages'][1]['content'])
        self.stages.append((stage, data))
        if stage == 'glossary':
            value = {'terms': []}
        elif stage == 'review':
            if self.fail_review:
                return response({}, 400)
            value = {'issues': [{'id': '0', 'category': 'number', 'evidence': 'Synthetic issue.'}]
                     if self.review_issue else []}
        else:
            items = data['fragments']
            value = {'translations': {item['id']: stage + ' 译文 ' + item['id'] for item in items}}
        if stage == 'judge':
            value['issues'] = data['issues']
        if self.cancel_stage == stage:
            self.cancel.set()
        return response({'model': 'synthetic/' + stage, 'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(value, ensure_ascii=False)}}]})


class ProductionProvenanceTests(SavedResultFixture):
    def setUp(self):
        super().setUp()
        self.save_result(result(('First sentence.', 'en'), ('Second sentence.', 'en')))
        self.fake = ProductionService()

    def execute(self):
        engine = Engine(self.config, self.store)
        self.fake.cancel = engine.cancel
        with mock.patch('requests.request', side_effect=self.fake.request):
            return engine.run(self.job_id)

    def final(self):
        cue = self.store.cues(self.job_id)[0]
        return cue['mt_stage_key'], self.store.mt_stage_entry(self.job_id, cue['mt_stage_key'])['record']

    def test_draft_is_reviewed_before_publication(self):
        self.assertEqual(self.execute(), 'completed')
        self.assertEqual([name for name, _ in self.fake.stages], ['glossary', 'translate', 'review'])
        _, final = self.final()
        self.assertEqual(final['production_stage_key'], final['translation_stage_key'])
        self.assertEqual(self.store.mt_stage_entry(self.job_id, final['production_stage_key'])['stage'], 'translate')
        self.assertEqual([cue['translated'] for cue in self.store.cues(self.job_id)], ['translate 译文 0', 'translate 译文 1'])
        self.assertEqual(self.store.counts(self.job_id).get('mt'), 3)

    def test_split_groups_translate_all_fragments(self):
        raw = timed_raw(['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel'], word_ms=1000)
        raw['transcripts'][0]['sentences'].append(
            {'begin_time': 8500, 'end_time': 9400, 'text': 'A short ending.', 'language': 'en'})
        self.save_result(raw)
        self.assertEqual(self.execute(), 'completed')
        self.assertEqual([name for name, _ in self.fake.stages], ['glossary', 'translate', 'review'])
        self.assertGreater(len(self.store.cues(self.job_id)), len(self.store.sources(self.job_id)))
        _, final = self.final()
        self.assertEqual(self.store.mt_stage_entry(self.job_id, final['production_stage_key'])['stage'], 'translate')

    def test_cancelled_draft_resumes_directly_and_retimes_without_network(self):
        self.fake.cancel_stage = 'translate'
        self.assertEqual(self.execute(), 'cancelled')
        draft = next(item for item in self.store.mt_stages(self.job_id) if item['stage'] == 'translate')
        self.fake.cancel_stage = None
        self.fake.stages.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.execute(), 'completed')
        self.assertEqual([name for name, _ in self.fake.stages], ['review'])
        self.assertEqual(self.store.mt_stage_entry(self.job_id, draft['key'])['record'], draft['record'])
        before = Path(self.store.job(self.job_id)['output']).read_bytes()
        self.fake.stages.clear()
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(self.execute(), 'completed')
        self.assertEqual(self.fake.stages, [])
        self.assertEqual(Path(self.store.job(self.job_id)['output']).read_bytes(), before)

    def test_current_split_stages_resume_and_retime(self):
        self.save_result(timed_raw(['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel'],
                                   word_ms=1000))
        for boundary in ['translate', 'review', 'judge']:
            with self.subTest(boundary=boundary):
                # Reflow clears private stages but retains the ordinary response cache.
                Engine(self.config, self.store).reflow(self.job_id, output_dir=self.path / boundary)
                with self.store.db:
                    self.store.db.execute('DELETE FROM mt_cache')
                self.fake.review_issue = boundary == 'judge'
                self.fake.cancel_stage = boundary
                self.fake.stages.clear()
                self.assertEqual(self.execute(), 'cancelled')
                saved = self.store.mt_stages(self.job_id)
                self.fake.cancel_stage = None
                self.fake.stages.clear()
                Engine(self.config, self.store).resume(self.job_id)
                self.assertEqual(self.execute(), 'completed')
                for item in saved:
                    self.assertEqual(self.store.mt_stage_entry(self.job_id, item['key'])['record'], item['record'])
                if boundary == 'translate':
                    self.assertIn('review', [name for name, _ in self.fake.stages])
                _, final = self.final()
                self.assertEqual(self.store.mt_stage_entry(self.job_id, final['production_stage_key'])['stage'],
                                 'judge' if boundary == 'judge' else 'translate')
                self.assertEqual(self.store.job(self.job_id)['manifest']['execution_snapshot']['alignment_version'], 3)
                self.fake.stages.clear()
                Engine(self.config, self.store).retime(self.job_id, overwrite=True)
                self.assertEqual(self.execute(), 'completed')
                self.assertEqual(self.fake.stages, [])

    def test_reflow_reuses_matching_draft_cache(self):
        self.assertEqual(self.execute(), 'completed')
        self.fake.stages.clear()
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.execute(), 'completed')
        self.assertEqual(self.fake.stages, [])
        _, final = self.final()
        self.assertEqual(final['production_stage_key'], final['translation_stage_key'])
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(self.execute(), 'completed')

    def test_review_failure_preserves_draft_and_valid_issues_reach_judge(self):
        self.fake.fail_review = True
        self.assertEqual(self.execute(), 'completed')
        _, final = self.final()
        self.assertTrue(final['warning'])
        self.assertEqual(final['production_stage_key'], final['translation_stage_key'])
        self.fake.fail_review = False
        self.fake.review_issue = True
        self.fake.stages.clear()
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.execute(), 'completed')
        _, final = self.final()
        self.assertTrue(final['warning'])
        self.assertEqual(self.store.mt_stage_entry(self.job_id, final['production_stage_key'])['stage'], 'judge')

    def test_direct_provenance_rejects_changed_source_mapping(self):
        self.assertEqual(self.execute(), 'completed')
        key, saved = self.final()
        draft = self.store.mt_stage_entry(self.job_id, saved['translation_stage_key'])['record']
        self.assertIn('source_text_hashes', draft)
        for mutation in ['swap_cues', 'swap_hashes', 'missing_hashes', 'wrong_draft_link']:
            with self.subTest(mutation=mutation):
                final, proof = copy.deepcopy(saved), copy.deepcopy(draft)
                if mutation == 'swap_cues':
                    final['cue_ids'].reverse()
                elif mutation == 'swap_hashes':
                    proof['source_text_hashes'].reverse()
                elif mutation == 'missing_hashes':
                    proof.pop('source_text_hashes')
                else:
                    final['translation_stage_key'] = 'f' * 64
                with self.store.db:
                    self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                        (json.dumps(final), self.job_id, key))
                    self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                        (json.dumps(proof), self.job_id, saved['translation_stage_key']))
                self.fake.stages.clear()
                with self.assertRaises(ToolError) as caught:
                    Engine(self.config, self.store).retime(self.job_id, overwrite=True)
                self.assertEqual(caught.exception.code, 'cache_mismatch')
                self.assertEqual(self.fake.stages, [])

    def test_corrupt_saved_draft_source_hashes_stop_before_review(self):
        self.fake.cancel_stage = 'translate'
        self.assertEqual(self.execute(), 'cancelled')
        saved = next(item for item in self.store.mt_stages(self.job_id) if item['stage'] == 'translate')
        saved['record']['source_text_hashes'].reverse()
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                (json.dumps(saved['record']), self.job_id, saved['key']))
        self.fake.cancel_stage = None
        self.fake.stages.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.execute(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')
        self.assertEqual(self.fake.stages, [])

    def test_direct_draft_survives_partial_group_relayout_and_interruption(self):
        raw = timed_raw(['A', 'cache', 'stores', 'previously', 'computed', 'results.'], word_ms=500)
        raw['transcripts'][0]['sentences'].append(
            {'begin_time': 3500, 'end_time': 4400, 'text': 'A short ending.', 'language': 'en'})
        self.save_result(raw)
        original_request = self.fake.request
        def request(method, url, **kwargs):
            reply = original_request(method, url, **kwargs)
            if url.endswith('/chat/completions') and self.fake.stages[-1][0] == 'translate':
                body = reply.json()
                value = json.loads(body['choices'][0]['message']['content'])
                value['translations']['0'] = '中' * 100
                body['choices'][0]['message']['content'] = json.dumps(value)
                return response(body)
            return reply
        self.fake.request = request
        original_save = self.store.save_layout
        interrupted = []
        def save(job_id, cues, manifest):
            original_save(job_id, cues, manifest)
            if manifest['layout_round'] == 1 and not interrupted:
                interrupted.append(True)
                raise OSError('Synthetic interruption after layout commit')
        with mock.patch.object(self.store, 'save_layout', side_effect=save):
            self.assertEqual(self.execute(), 'failed')
        self.assertTrue(interrupted)
        self.assertNotIn('mt_group_plan', self.store.job(self.job_id)['manifest'])
        cues = self.store.cues(self.job_id)
        self.assertGreater(len(cues), 2)
        self.assertFalse(cues[0]['translated'])
        self.assertTrue(cues[-1]['translated'])
        final = self.store.mt_stage_entry(self.job_id, cues[-1]['mt_stage_key'])['record']
        self.assertEqual(final['production_stage_key'], final['translation_stage_key'])
        self.assertNotIn(final['cue_ids'][0], [cue['cue_id'] for cue in cues])
        Engine(self.config, self.store).resume(self.job_id)
        self.fake.stages.clear()
        self.assertEqual(self.execute(), 'completed')
        self.assertIn('review', [name for name, _ in self.fake.stages])
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['mt_group_plan']['layout_round'], manifest['layout_round'])
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(self.execute(), 'completed')
