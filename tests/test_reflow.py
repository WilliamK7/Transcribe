from __future__ import annotations

import copy
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.main import main, summary
from subtitle_tool.mt import batch_cache_key, batch_cache_value
from subtitle_tool.subtitles import normalize, plan_fragments, wrap
from subtitle_tool.config import Settings
from tests.fixtures import FakeService, PipelineFixture, result, timed_raw


class ZeroDurationWordTests(unittest.TestCase):
    def test_leading_middle_trailing_and_consecutive_points_keep_text_and_endpoints(self):
        for zeros in [(0,), (9,), (19,), (0, 1, 2), (8, 9, 10), (17, 18, 19)]:
            with self.subTest(zeros=zeros):
                raw = timed_raw([f'word{i},' for i in range(20)], word_ms=1000)
                sentence = raw['transcripts'][0]['sentences'][0]
                for index in zeros:
                    sentence['words'][index]['end_time'] = sentence['words'][index]['begin_time']
                saved = copy.deepcopy(raw)
                source = normalize(raw, 0, '0000', preserve_sentences=True, subtitle_version=2)[0][0]
                self.assertEqual(raw, saved)
                self.assertEqual(source['word_timing_status'], 'reliable_grouped_points')
                self.assertEqual(len(source['words']), 20 - len(zeros))
                points = {w[k] for w in sentence['words'] for k in ['begin_time', 'end_time']}
                self.assertTrue(all(w['begin_ms'] in points and w['end_ms'] in points for w in source['words']))
                cues = plan_fragments(source, Settings(zh_line_width=22, other_line_width=42))
                self.assertGreater(len(cues), 1)
                self.assertEqual(re.sub(r'\s', '', ''.join(c['text'] for c in cues)), re.sub(r'\s', '', sentence['text']))
                self.assertTrue(all(c['end_ms'] - c['begin_ms'] >= 1000 for c in cues))
                self.assertTrue(all(len(wrap(c['text'], 42)) <= 2 for c in cues))
                self.assertTrue(all(a['end_ms'] <= b['begin_ms'] for a, b in zip(cues, cues[1:])))

    def test_invalid_timing_or_text_retains_whole_sentence(self):
        for mode in ['all_zero', 'backwards', 'outside', 'overlap', 'text', 'punctuation']:
            with self.subTest(mode=mode):
                raw = timed_raw([f'word{i}' for i in range(20)], word_ms=300)
                sentence = raw['transcripts'][0]['sentences'][0]
                if mode == 'all_zero':
                    for word in sentence['words']:
                        word['end_time'] = word['begin_time']
                elif mode == 'backwards':
                    sentence['words'][1]['end_time'] = 299
                elif mode == 'outside':
                    sentence['words'][-1]['end_time'] = 6001
                elif mode == 'overlap':
                    sentence['words'][1]['begin_time'] = 299
                elif mode == 'text':
                    sentence['words'][1]['text'] = 'wrong'
                else:
                    sentence['words'][1]['punctuation'] = '!'
                source = normalize(raw, 0, '0000', preserve_sentences=True, subtitle_version=2)[0][0]
                self.assertEqual(source['words'], [])
                cue = plan_fragments(source, Settings())[0]
                self.assertEqual((cue['begin_ms'], cue['end_ms'], cue['text']), (0, 6000, sentence['text']))

    def test_version_one_still_rejects_point_words(self):
        raw = timed_raw(['word'] * 20)
        raw['transcripts'][0]['sentences'][0]['words'][0]['end_time'] = 0
        old = normalize(raw, 0, '0000', preserve_sentences=True, subtitle_version=1)[0][0]
        new = normalize(raw, 0, '0000', preserve_sentences=True, subtitle_version=2)[0][0]
        self.assertEqual(old['words'], [])
        self.assertTrue(new['words'])


class ReflowTests(PipelineFixture):
    def setUp(self):
        super().setUp()
        self.config.settings.zh_line_width = 22
        self.config.settings.other_line_width = 42
        manifest = self.store.job(self.job_id)['manifest']
        manifest['subtitle_version'] = 1
        manifest['subtitle_settings'].update(zh_line_width=22, other_line_width=42)
        self.store.update(self.job_id, manifest=manifest)

    def complete_old_job(self, raw=None):
        fake = FakeService(raw or result(('First sentence.', 'en'), ('Second sentence.', 'en')))
        self.assertEqual(self.run_fake(fake), 'completed')
        return fake

    def test_resume_keeps_saved_widths_and_reflow_uses_current_settings(self):
        self.config.settings.zh_line_width = 44
        self.config.settings.other_line_width = 84
        raw = timed_raw(['字'] * 60, language='zh', word_ms=50)
        english = timed_raw(['word'] * 24, word_ms=150, start=3000)
        raw['transcripts'][0]['sentences'].extend(english['transcripts'][0]['sentences'])
        fake = FakeService(raw)

        def interrupt(job_id, stage, done=None, total=None):
            if stage == 'segmenting':
                raise KeyboardInterrupt()

        with mock.patch('requests.request', side_effect=fake.request), mock.patch('requests.post', side_effect=fake.upload):
            self.assertEqual(Engine(self.config, self.store, progress=interrupt).run(self.job_id), 'cancelled')
        self.assertFalse(self.store.job(self.job_id)['manifest'].get('normalized'))
        self.store.update(self.job_id, 'queued')
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(fake.named('/asr/transcription'), [])
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual((manifest['output_settings']['zh_line_width'], manifest['output_settings']['other_line_width']), (22, 42))
        self.assertGreater(len(self.store.cues(self.job_id)), 2)
        output = self.source.with_suffix('.srt')
        previous = output.read_bytes()

        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(fake.calls, [])
        manifest = self.store.job(self.job_id)['manifest']
        for name in ['subtitle_settings', 'output_settings']:
            self.assertEqual((manifest[name]['zh_line_width'], manifest[name]['other_line_width']), (44, 84))
        sources, cues = self.store.sources(self.job_id), self.store.cues(self.job_id)
        self.assertEqual([c['text'] for c in cues], [s['text'] for s in sources])
        self.assertEqual([(c['begin_ms'], c['end_ms']) for c in cues], [(0, 3000), (3000, 6600)])
        self.assertNotEqual(output.read_bytes(), previous)

    def test_reflow_requires_explicit_overwrite_even_if_old_job_allowed_it(self):
        self.complete_old_job()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['overwrite'] = True
        self.store.update(self.job_id, manifest=manifest)
        before = self.store.job(self.job_id)
        with self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).reflow(self.job_id)
        self.assertEqual(error.exception.code, 'overwrite_required')
        self.assertEqual(self.store.job(self.job_id), before)

    def test_reflow_uses_only_local_asr_and_exact_translation_cache(self):
        fake = self.complete_old_job()
        output = self.source.with_suffix('.srt')
        old = output.read_bytes()
        counts = self.store.counts(self.job_id)
        self.source.unlink()
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        prepared = self.store.job(self.job_id)
        self.assertEqual(prepared['state'], 'queued')
        self.assertEqual(prepared['manifest']['subtitle_version'], 2)
        snapshot = self.config.root / prepared['manifest']['reflow']['snapshot']
        saved = json.loads(snapshot.read_text(encoding='utf-8'))
        self.assertEqual(saved['job']['manifest']['subtitle_version'], 1)
        self.assertTrue(saved['sources'])
        self.assertTrue(saved['references'])
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.store.counts(self.job_id).get('mt'), counts.get('mt'))
        self.assertEqual(output.read_bytes(), old)
        backup = Path(self.store.job(self.job_id)['manifest']['backup'])
        self.assertEqual(backup.read_bytes(), old)
        report = summary(self.store, self.job_id)
        self.assertIn('timings', report)
        self.assertGreater(report['timings']['run_seconds'], 0)
        self.assertIn('reference_translating', report['timings']['stages_seconds'])

    def test_missing_raw_fails_without_requests_or_changing_task_or_srt(self):
        self.complete_old_job()
        output = self.source.with_suffix('.srt')
        old, before = output.read_bytes(), self.store.job(self.job_id)
        (self.config.data / 'cache' / self.job_id / 'raw-0000.json').unlink()
        with mock.patch('requests.request') as request, self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(error.exception.code, 'reflow_cache_missing')
        request.assert_not_called()
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual(output.read_bytes(), old)

    def test_new_grouping_translation_failure_keeps_original_and_resume_uses_saved_work(self):
        raw = timed_raw([f'word{i}' for i in range(30)], word_ms=150)
        raw['transcripts'][0]['sentences'][0]['words'][0]['end_time'] = 0
        fake = self.complete_old_job(raw)
        output = self.source.with_suffix('.srt')
        old = output.read_bytes()
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        source = self.store.sources(self.job_id)[0]
        planned = plan_fragments(source, self.config.settings)
        fake.mt_failure.add(planned[-1]['text'])
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'failed')
        self.assertEqual(output.read_bytes(), old)
        self.assertFalse(list(output.parent.glob(output.name + '.bak.*')))
        self.assertEqual(fake.named('/asr/transcription'), [])
        self.assertEqual(len(fake.uploads), 1)
        self.assertFalse(any(c['translated'] for c in self.store.cues(self.job_id)))
        references = self.store.references(self.job_id)
        self.assertTrue(all(r['translated'] for r in references))
        fake.mt_failure.clear()
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(self.store.references(self.job_id), references)
        self.assertTrue(all(c['translated'] for c in self.store.cues(self.job_id)))
        self.assertEqual(fake.named('/asr/transcription'), [])
        self.assertEqual(Path(self.store.job(self.job_id)['manifest']['backup']).read_bytes(), old)

    def test_manual_language_decision_survives_reflow(self):
        fake = FakeService(result(('OK', '')))
        self.assertEqual(self.run_fake(fake), 'needs_review')
        cue = self.store.cues(self.job_id)[0]
        self.store.cue_update(self.job_id, cue['cue_id'], decision='translate', source_lang='English', reason='用户在终端确认')
        self.store.update(self.job_id, 'queued')
        self.assertEqual(self.run_fake(fake), 'completed')
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        source = self.store.sources(self.job_id)[0]
        self.assertEqual((source['decision'], source['source_lang'], source['reason']), ('translate', 'English', '用户在终端确认'))
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(fake.calls, [])

    def test_account_change_blocks_reflow_before_mutation(self):
        self.complete_old_job()
        before = self.store.job(self.job_id)
        self.config.set_key('offline-other-account')
        with self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(error.exception.code, 'account_changed')
        self.assertEqual(self.store.job(self.job_id), before)

    def test_reference_translation_change_is_not_fragment_context_in_strategy_three(self):
        fake = self.complete_old_job(timed_raw([f'word{i}' for i in range(24)], word_ms=200))
        previous_cues = self.store.cues(self.job_id)
        manifest = self.store.job(self.job_id)['manifest']
        reference = self.store.references(self.job_id)[0]
        key = batch_cache_key([reference['text']], reference['source_lang'], **manifest['cache_identity'],
                              stage='reference', asr_language=reference['language'])
        updated_reference = reference['translated'].replace('繁体译文：', '更新参考：')
        with self.store.db:
            changed = self.store.db.execute('UPDATE mt_cache SET text=? WHERE key=?',
                                           (batch_cache_value({'0': updated_reference}), key))
            self.assertEqual(changed.rowcount, 1)
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertGreater(len(previous_cues), 1)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.store.references(self.job_id)[0]['translated'], updated_reference)
        self.assertEqual([(c['word_start'], c['word_end']) for c in self.store.cues(self.job_id)],
                         [(c['word_start'], c['word_end']) for c in previous_cues])
        self.assertEqual([c['translated'] for c in self.store.cues(self.job_id)],
                         [c['translated'] for c in previous_cues])

    def test_reflow_to_new_directory_keeps_original_output_without_overwrite(self):
        fake = self.complete_old_job()
        output = self.source.with_suffix('.srt')
        previous = output.read_bytes()
        directory = self.path / 'new-output'
        Engine(self.config, self.store).reflow(self.job_id, output_dir=directory)
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(fake.calls, [])
        self.assertEqual(output.read_bytes(), previous)
        self.assertEqual((directory / output.name).read_bytes(), previous)

    def test_legacy_manual_decision_is_preserved_when_complete_sentence_matches(self):
        manifest = self.store.job(self.job_id)['manifest']
        for name in ['subtitle_version', 'translation_strategy_version', 'subtitle_settings']:
            manifest.pop(name)
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(result(('OK', '')))
        self.assertEqual(self.run_fake(fake), 'needs_review')
        cue = self.store.cues(self.job_id)[0]
        self.store.cue_update(self.job_id, cue['cue_id'], decision='original', source_lang='auto', reason='用户在终端确认')
        self.store.update(self.job_id, 'queued')
        self.assertEqual(self.run_fake(fake), 'completed')
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        source = self.store.sources(self.job_id)[0]
        self.assertEqual((source['decision'], source['source_lang'], source['reason']), ('original', 'auto', '用户在终端确认'))
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(fake.calls, [])

    def test_transaction_failure_keeps_old_sources_cues_and_resume_marker(self):
        self.complete_old_job()
        before = self.store.job(self.job_id)
        sources, cues = self.store.sources(self.job_id), self.store.cues(self.job_id)
        references = self.store.references(self.job_id)
        with mock.patch.object(self.store, '_replace_records', side_effect=OSError()), self.assertRaises(OSError):
            Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual(self.store.sources(self.job_id), sources)
        self.assertEqual(self.store.cues(self.job_id), cues)
        self.assertEqual(self.store.references(self.job_id), references)

    def test_cli_reflow_and_resume_after_prepared_transaction(self):
        fake = self.complete_old_job()
        with mock.patch('subtitle_tool.main.Config', return_value=self.config), mock.patch('sys.stdout', new_callable=io.StringIO), mock.patch('sys.stderr', new_callable=io.StringIO), mock.patch('requests.request', side_effect=fake.request), mock.patch('requests.post', side_effect=fake.upload):
            self.assertEqual(main(['--reflow', self.job_id, '--overwrite', '--non-interactive']), 0)
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.store.update(self.job_id, 'subtitle_translating')
        self.store.recover()
        self.assertEqual(self.store.job(self.job_id)['state'], 'queued')
        with mock.patch('subtitle_tool.main.Config', return_value=self.config), mock.patch('sys.stdout', new_callable=io.StringIO), mock.patch('sys.stderr', new_callable=io.StringIO), mock.patch('requests.request', side_effect=fake.request), mock.patch('requests.post', side_effect=fake.upload):
            self.assertEqual(main(['--resume', self.job_id, '--non-interactive']), 0)
        self.assertEqual(self.store.job(self.job_id)['state'], 'completed')
