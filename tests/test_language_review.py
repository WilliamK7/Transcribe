"""Automatic language decisions use synthetic text and the existing gateway transport."""
from __future__ import annotations

import copy
import json
from unittest import mock

from subtitle_tool.common import Cancelled, ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.mt_quality import BatchValidationError, parse_stage_response
from subtitle_tool.review import ReviewDecision
from tests.fixtures import FakeService, SavedResultFixture, result, unlabeled_result


class LanguageService(FakeService):
    def __init__(self, decisions, *, invalid=False):
        super().__init__(result())
        self.decisions = decisions
        self.invalid = invalid
        self.language_requests = []
        self.fail_targets = set()

    def request(self, method, url, **kwargs):
        from subtitle_tool.mt_quality import PROMPTS
        from tests.fixtures import response
        payload = kwargs.get('json', {})
        messages = payload.get('messages', [])
        if messages and messages[0]['content'].startswith(PROMPTS['language']):
            self.calls.append((method, url, kwargs))
            data = json.loads(messages[1]['content'])
            self.language_requests.append(data)
            if any(item['text'] in self.fail_targets for item in data['sentences']):
                return response({'error': {'code': 'SyntheticFailure'}}, 401)
            value = {'decisions': {item['id']: self.decisions.get(item['text'], 'uncertain')
                                   for item in data['sentences']}}
            if self.invalid:
                value['decisions'].pop(next(iter(value['decisions'])), None)
            return response({'model': 'synthetic/language', 'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(value, ensure_ascii=False)}}]})
        return super().request(method, url, **kwargs)


class LanguageReviewTests(SavedResultFixture):
    def test_common_chinese_and_formula_do_not_interrupt_translation(self):
        raw = unlabeled_result('This is a complete sentence.', '哎。', 'π/4.', '随便说说。')
        self.save_result(raw)
        fake = LanguageService({'哎。': 'original', 'π/4.': 'original', '随便说说。': 'original'})
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(len(fake.language_requests), 1)
        self.assertEqual([s['decision'] for s in self.store.sources(self.job_id)],
                         ['translate', 'original', 'original', 'original'])
        self.assertEqual(json.loads((self.cache / 'raw-0000.json').read_bytes()), raw)
        self.assertTrue(all(s['language'] == '' for s in self.store.sources(self.job_id)))
        data = fake.language_requests[0]
        self.assertEqual({s['text'] for s in data['sentences']}, {'哎。', 'π/4.', '随便说说。'})
        self.assertIn('This is a complete sentence.', [c['text'] for c in data['context']])
        self.assertTrue(all(c['sentence_id'].startswith('0000:') for c in data['context']))
        self.assertTrue(all(call[1].endswith('/chat/completions') for call in fake.calls))

    def test_genuine_uncertainty_is_reviewed_only_once_and_manual_decision_wins(self):
        self.save_result(unlabeled_result('随便说说。', '東京都港区'))
        fake = LanguageService({'随便说说。': 'original', '東京都港区': 'uncertain'})
        self.assertEqual(self.run_fake(fake), 'needs_review')
        self.assertEqual(self.store.sources(self.job_id)[0]['decision'], 'original')
        self.assertEqual(len(fake.language_requests), 1)
        self.assertEqual(self.run_fake(fake), 'needs_review')
        self.assertEqual(len(fake.language_requests), 1)
        sources = self.store.sources(self.job_id)
        self.store.apply_review(self.job_id, sources[1]['cue_id'], ReviewDecision('original'))
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(len(fake.language_requests), 1)
        self.assertEqual(self.store.sources(self.job_id)[1]['decision_origin'], 'manual')

    def test_invalid_batch_keeps_all_decisions_and_never_publishes(self):
        sources = self.save_result(unlabeled_result('随便说说。', '東京都港区'))
        fake = LanguageService({'随便说说。': 'original'}, invalid=True)
        self.assertEqual(self.run_fake(fake), 'needs_review')
        self.assertEqual([s['decision'] for s in self.store.sources(self.job_id)], ['uncertain'] * 2)
        self.assertFalse(self.source.with_suffix('.srt').exists())
        self.assertEqual([s['text'] for s in self.store.sources(self.job_id)], [s['text'] for s in sources])

    def test_manual_review_after_partial_failure_does_not_reclassify_completed_sources(self):
        texts = [f'片段{i}。' for i in range(9)]
        self.save_result(unlabeled_result(*texts))
        raw = (self.cache / 'raw-0000.json').read_bytes()
        fake = LanguageService(dict.fromkeys(texts, 'original'))
        fake.decisions[texts[0]] = 'uncertain'
        fake.decisions[texts[7]] = 'uncertain'
        fake.fail_targets.add(texts[-1])
        self.assertEqual(self.run_fake(fake), 'needs_review')
        self.assertEqual([len(data['sentences']) for data in fake.language_requests], [8, 1])
        sources = self.store.sources(self.job_id)
        completed = copy.deepcopy(sources[1:8])
        saved_stage = self.store.mt_stage_entry(self.job_id, sources[0]['language_review']['stage_key'])
        self.store.apply_review(self.job_id, sources[0]['cue_id'], ReviewDecision('original'))
        manual = self.store.sources(self.job_id)[0]
        fake.fail_targets.clear()
        # A changed model answer must have no effect on already accepted decisions.
        fake.decisions.update(dict.fromkeys(texts[1:8], 'translate'))
        self.assertEqual(self.run_fake(fake), 'needs_review')
        self.assertEqual([item['text'] for data in fake.language_requests[2:] for item in data['sentences']],
                         [texts[-1]])
        after = self.store.sources(self.job_id)
        self.assertEqual(after[0], manual)
        self.assertEqual(after[1:8], completed)
        self.assertEqual(after[-1]['decision'], 'original')
        self.assertEqual(self.store.mt_stage_entry(self.job_id, sources[0]['language_review']['stage_key']), saved_stage)
        self.assertEqual(self.run_fake(fake), 'needs_review')
        self.assertEqual(len(fake.language_requests), 3)
        self.store.apply_review(self.job_id, after[7]['cue_id'], ReviewDecision('original'))
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(len(fake.language_requests), 3)
        self.assertEqual((self.cache / 'raw-0000.json').read_bytes(), raw)

    def test_long_formula_with_small_decision_response_is_automatically_reviewed(self):
        text = '(1+2)*' * 500
        raw = unlabeled_result(text)
        self.save_result(raw)
        fake = LanguageService({text: 'original'})
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(len(fake.language_requests), 1)
        self.assertEqual(fake.language_requests[0]['sentences'][0]['text'], text)
        self.assertEqual(self.store.sources(self.job_id)[0]['decision'], 'original')
        self.assertEqual(json.loads((self.cache / 'raw-0000.json').read_bytes()), raw)

    def test_language_review_still_rejects_an_oversized_input(self):
        text = '(1+2)*' * 6000
        self.save_result(unlabeled_result(text))
        fake = LanguageService({text: 'original'})
        self.assertEqual(self.run_fake(fake), 'needs_review')
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.store.sources(self.job_id)[0]['decision'], 'uncertain')
        self.assertFalse(self.source.with_suffix('.srt').exists())

    def test_interrupted_decision_commit_resumes_from_saved_stage_without_repeating_request(self):
        self.save_result(unlabeled_result('随便说说。'))
        fake = LanguageService({'随便说说。': 'original'})
        with mock.patch.object(self.store, 'save_language_review', side_effect=Cancelled):
            self.assertEqual(self.run_fake(fake), 'cancelled')
        self.assertEqual(len(fake.language_requests), 1)
        self.assertEqual(self.store.sources(self.job_id)[0]['decision'], 'uncertain')
        self.assertEqual([s['stage'] for s in self.store.mt_stages(self.job_id)], ['language'])
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(len(fake.language_requests), 1)

    def test_decision_write_rolls_back_sources_cues_and_manifest_together(self):
        from subtitle_tool.mt_language import review_languages
        self.save_result(unlabeled_result('随便说说。'))
        fake = LanguageService({'随便说说。': 'original'})
        before = (self.store.sources(self.job_id), self.store.cues(self.job_id), self.store.job(self.job_id)['manifest'])
        engine = Engine(self.config, self.store)
        manifest = copy.deepcopy(before[2])
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        with mock.patch('requests.request', side_effect=fake.request), \
                mock.patch.object(self.store, '_replace_records', side_effect=OSError('synthetic disk failure')), \
                self.assertRaises(OSError):
            review_languages(engine, self.job_id, manifest, scope)
        self.assertEqual((self.store.sources(self.job_id), self.store.cues(self.job_id),
                          self.store.job(self.job_id)['manifest']), before)
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(len(fake.language_requests), 1)

    def test_model_only_receives_unlabeled_automatic_targets(self):
        self.save_result(result(('This conflicts with Chinese.', 'zh'), ('啊。', ''), ('OK', '')))
        self.store.update(self.job_id, 'needs_review', code='language_uncertain')
        source = self.store.sources(self.job_id)[-1]
        self.store.apply_review(self.job_id, source['cue_id'], ReviewDecision('original'))
        fake = LanguageService({'啊。': 'original', 'This conflicts with Chinese.': 'translate', 'OK': 'translate'})
        self.assertEqual(self.run_fake(fake), 'needs_review')
        self.assertEqual([s['text'] for data in fake.language_requests for s in data['sentences']], ['啊。'])
        self.assertEqual([s['decision'] for s in self.store.sources(self.job_id)], ['uncertain', 'original', 'original'])

    def test_language_context_observes_segment_and_time_boundaries(self):
        from subtitle_tool.mt_language import _data
        from subtitle_tool.subtitles import normalize
        sources = normalize(unlabeled_result('English background.', '呃。', 'Chinese background.'), 0, '0000')[0]
        sources[2]['sentence_id'] = '0001:00000002'
        sources[0].update(begin_ms=0, end_ms=900)
        sources[1].update(begin_ms=61000, end_ms=61900)
        self.assertEqual(_data(sources, [1])['context'], [])
        sources[1].update(begin_ms=60900, end_ms=61800)
        context = _data(sources, [1])['context']
        self.assertEqual([c['text'] for c in context], ['English background.'])

    def test_language_decision_validator_rejects_partial_extra_and_invalid_outputs(self):
        from tests.test_mt_quality import body
        valid = {'decisions': {'0': 'original', '1': 'translate'}}
        self.assertEqual(parse_stage_response('language', body(valid), ids=['0', '1']), valid)
        for value in [{'decisions': {'0': 'original'}},
                      {'decisions': {'0': 'original', '1': 'translate', '2': 'original'}},
                      {'decisions': {'0': 'English', '1': 'original'}},
                      {'decisions': {'0': True, '1': 'original'}}]:
            with self.subTest(value=value), self.assertRaises(BatchValidationError):
                parse_stage_response('language', body(value), ids=['0', '1'])

    def test_applied_model_decision_cannot_silently_diverge_from_stage_evidence(self):
        self.save_result(unlabeled_result('随便说说。', '東京都港区'))
        fake = LanguageService({'随便说说。': 'original', '東京都港区': 'uncertain'})
        self.assertEqual(self.run_fake(fake), 'needs_review')
        source = self.store.sources(self.job_id)[0]
        source['decision'] = 'translate'
        with self.store.db:
            self.store.db.execute('UPDATE subtitle_sources SET record=? WHERE job_id=? AND sentence_id=?',
                                 (json.dumps(source), self.job_id, source['sentence_id']))
            self.store.db.execute('UPDATE cues SET decision=? WHERE job_id=? AND cue_id=?',
                                 ('translate', self.job_id, source['cue_id']))
        engine = Engine(self.config, self.store)
        with self.assertRaises(ToolError):
            engine.validate_saved_subtitles(self.job_id, self.store.job(self.job_id)['manifest'])
