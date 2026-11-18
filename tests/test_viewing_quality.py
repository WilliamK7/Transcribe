import json
import threading
import unittest
from types import SimpleNamespace

from subtitle_tool.config import Settings
from subtitle_tool.engine import Engine
from subtitle_tool.mt_quality import make_stage_payload
from subtitle_tool.subtitles import plan_fragments
from tests.fixtures import overlapping_raw, result, timed_source
from tests.mt_fixtures import PoolPipelineFixture


class ViewingQualityTests(unittest.TestCase):
    def test_a_whole_future_subtitle_inside_an_earlier_translation_is_checked(self):
        from subtitle_tool.mt_quality import repetition_checks
        short = '稳压器输出电压取决于参考电压。'
        values = {'0': '这就是系统的工作方式。' + short, '1': '而另一种电路不同。', '2': short}
        self.assertEqual([issue['id'] for issue in repetition_checks(values, ['one', 'two', 'three'])], ['0', '2'])
        self.assertEqual(repetition_checks({'0': '电压增加。', '1': '电压增加。'}, ['one', 'two']), [])

    def test_review_has_distinct_sentence_labels_and_each_display_pair(self):
        data = {'sentences': [{'id': '0', 'text': 'A sensor.'}, {'id': '1', 'text': 'It measures force.'}],
                'fragments': [{'id': '0', 'sentence': '0', 'text': 'A sensor.'},
                              {'id': '1', 'sentence': '1', 'text': 'It measures'},
                              {'id': '2', 'sentence': '1', 'text': 'force.'}],
                'translations': {'0': '这是一个传感器。', '1': '它测量的', '2': '是力。'},
                'context': [], 'glossary': []}
        payload = make_stage_payload('review', data, SimpleNamespace(model='offline'),
            capabilities={'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096})
        new = json.loads(payload['messages'][1]['content'])
        self.assertTrue({sentence['id'] for sentence in new['sentences']}.isdisjoint(data['translations']))
        self.assertEqual([fragment['translation'] for fragment in new['fragments']], list(data['translations'].values()))
        self.assertEqual(new['fragments'][1]['sentence'], new['sentences'][1]['id'])
        self.assertEqual(new['sentences'][1]['fragment_ids'], ['1', '2'])

    def test_balanced_english_break_keeps_article_with_its_noun(self):
        source = timed_source(['Calculate', 'the', 'total', 'charge'])
        cuts = plan_fragments(source, Settings(), force=True)
        self.assertEqual([cut['text'] for cut in cuts], ['Calculate', 'the total charge'])
        self.assertEqual(cuts[0]['end_ms'], source['words'][0]['end_ms'])

    def test_reviewed_language_takes_precedence_for_english_breaks(self):
        from subtitle_tool.review import ReviewDecision
        source = timed_source(['Calculate', 'the', 'total', 'charge'])
        for asr, manual, expected in [('zh', 'English', 'Calculate'), ('en', 'French', 'Calculate the'),
                                      ('en', 'auto', 'Calculate'), ('zh', 'auto', 'Calculate the')]:
            with self.subTest(asr=asr, manual=manual):
                reviewed = {**source, 'language': asr, **ReviewDecision('translate', manual).values()}
                self.assertEqual(plan_fragments(reviewed, Settings(), force=True)[0]['text'], expected)

    def test_equal_pauses_do_not_force_a_third_screen_for_a_preposition(self):
        source = timed_source(['The', 'voltage', 'depends', 'on', 'the', 'reference', 'signal', 'at', 'this', 'terminal.'],
                                durations=[1820, 1600, 1900, 450, 500, 600, 500, 500, 900, 1400],
                                pauses={3: 420, 4: 420, 5: 420})
        parts = plan_fragments(source, Settings())
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]['text'], 'The voltage depends')
        self.assertTrue(all(part['end_ms'] - part['begin_ms'] <= 7000 for part in parts))


class ViewingPipelineTests(PoolPipelineFixture):
    duplicated = '整个测量系统会在温度达到上限时关闭电源。'

    def setUp(self):
        super().setUp()
        self.config.settings.mt_workers = 1

    def prepare(self, same_source=False):
        first = 'The circuit supplies a constant voltage.'
        self.save_result(result((first, 'en'), (first if same_source else 'The meter measures the current.', 'en')))
        Engine(self.config, self.store).reflow(self.job_id)
        self.draft_text = lambda fragment: self.duplicated

    def test_resume_completes_with_a_warning_if_repetition_evidence_exceeds_judge_budget(self):
        self.prepare()
        manifest = self.store.job(self.job_id)['manifest']
        for entry in manifest['mt_route_snapshot']['entries']:
            if entry['id'] in ('magpie_judge', 'magpie_judge_fallback'):
                entry['capabilities']['input_bytes'] = 3200
        self.store.update(self.job_id, manifest=manifest)
        self.cancel_after = 'review'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        self.assertEqual([stage for stage, _, _ in self.calls], ['glossary', 'translate', 'review'])
        self.cancel_after = None
        self.calls.clear()
        engine = Engine(self.config, self.store)
        engine.resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertFalse(self.calls)
        self.assertEqual([cue['translated'] for cue in self.store.cues(self.job_id)], [self.duplicated] * 2)
        final = next(row['record'] for row in self.store.mt_stages(self.job_id) if row['stage'] == 'final')
        self.assertTrue(final['warning'])
        self.assertEqual(final['production_stage_key'], final['translation_stage_key'])
        self.assertEqual(final['correction_stage_keys'], [])
        engine.retime(self.job_id, overwrite=True)

    def test_each_stage_receives_the_published_display_duration_after_overlap_adjustment(self):
        self.save_result(overlapping_raw())
        Engine(self.config, self.store).reflow(self.job_id)
        self.review_issue = self.dismiss_review = self.no_change_correction = True
        self.assertEqual(self.run_pool(), 'completed')
        for stage in ('translate', 'review', 'judge'):
            data = next(request for name, _, request in self.calls if name == stage)
            self.assertEqual([part.get('display_ms') for part in data['fragments']], [3960, 3360])

    def test_clean_reviewer_does_not_silence_distinct_sources_with_one_long_translation(self):
        self.prepare()
        corrected = {'0': '电路提供恒定电压。', '1': '仪表测量电流。'}
        self.corrected_translations = lambda data: corrected
        self.cancel_after = 'review'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        self.cancel_after = None
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([stage for stage, _, _ in self.calls], ['judge', 'review'])
        judge = self.calls[0][2]
        self.assertEqual({issue['id'] for issue in judge['issues']}, {'0', '1'})
        self.assertEqual([cue['translated'] for cue in self.store.cues(self.job_id)], list(corrected.values()))
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_repeated_spoken_sentence_does_not_trigger_a_judgment(self):
        self.prepare(same_source=True)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertFalse(any(stage == 'judge' for stage, _, _ in self.calls))

    def test_a_judge_may_dismiss_repetition_without_rephrasing_valid_text(self):
        self.prepare()
        self.dismiss_review = self.no_change_correction = True
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(sum(stage == 'judge' for stage, _, _ in self.calls), 1)
        self.assertEqual([cue['translated'] for cue in self.store.cues(self.job_id)], [self.duplicated] * 2)
        self.assertFalse(self.store.counts(self.job_id).get('mt_quality_warning'))
