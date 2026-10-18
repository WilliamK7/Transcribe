from __future__ import annotations

import copy
import hashlib
import sqlite3
import threading
from unittest import mock

from subtitle_tool.engine import Engine
from subtitle_tool.main import summary
from subtitle_tool.subtitles import mt_cache_key, translation_context
from tests.fixtures import FakeService, PipelineFixture, response, result, timed_raw


class ReferenceReuseTests(PipelineFixture):
    fixture_seconds = 60

    def test_new_jobs_snapshot_wider_defaults_and_keep_short_sentences_whole(self):
        fake = FakeService(timed_raw(['word'] * 24, word_ms=150))
        self.assertEqual(self.run_fake(fake), 'completed')
        manifest = self.store.job(self.job_id)['manifest']
        for name in ['subtitle_settings', 'output_settings']:
            self.assertEqual((manifest[name]['zh_line_width'], manifest[name]['other_line_width']), (44, 84))
        self.assertEqual(len(self.store.cues(self.job_id)), 1)
        self.assertEqual(manifest['warnings'], [])
        self.assertEqual(len(fake.named('/chat/completions')), 1)

    def prepare_subtitle_stage(self, raw=None):
        fake = FakeService(raw or result(('First sentence.', 'en'), ('Second sentence.', 'en')))

        def interrupt(job, stage, done=None, total=None):
            if stage == 'subtitle_translating':
                raise KeyboardInterrupt()

        with mock.patch('requests.request', side_effect=fake.request), \
                mock.patch('requests.post', side_effect=fake.upload):
            self.assertEqual(Engine(self.config, self.store, progress=interrupt).run(self.job_id), 'cancelled')
        self.store.update(self.job_id, 'queued')
        return fake

    def offline_resume(self, cancel=None):
        with mock.patch('requests.request', side_effect=AssertionError('Unexpected network request')), \
                mock.patch('requests.post', side_effect=AssertionError('Unexpected upload')):
            return Engine(self.config, self.store, cancel).run(self.job_id)

    def test_fifty_distinct_whole_sentences_only_request_references(self):
        sentences = [(f'Sentence number {i}.', 'en') for i in range(49)] + [('今日は晴れです。', 'ja')]
        fake = FakeService(result(*sentences))
        self.assertEqual(self.run_fake(fake), 'completed')
        calls = fake.named('/chat/completions')
        self.assertEqual(len(calls), 50)
        self.assertTrue(all(not call[2]['json']['translation_options'].get('tm_list') for call in calls))
        counts = self.store.counts(self.job_id)
        self.assertEqual(counts['mt_reference_request'], 50)
        self.assertEqual(counts.get('mt_subtitle_request', 0), 0)
        self.assertEqual(counts['mt_reference_reused'], 50)
        self.assertEqual(counts.get('mt_subtitle_cache_hit', 0), 0)
        cues = self.store.cues(self.job_id)
        self.assertTrue(all(c['attempts'] == 0 and c['translated'] for c in cues))
        self.assertEqual([c['text'] for c in cues], [text for text, _ in sentences])
        self.assertEqual([c['translated'] for c in cues], [r['translated'] for r in self.store.references(self.job_id)])
        self.assertEqual(summary(self.store, self.job_id)['translation_strategy_version'], 2)

    def test_reuse_and_counter_roll_back_together_and_resume_counts_once(self):
        self.prepare_subtitle_stage(result(('Isolated sentence.', 'en')))
        self.store.db.execute("CREATE TEMP TRIGGER reject_reuse BEFORE INSERT ON counters "
                              "WHEN NEW.name='mt_reference_reused' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.offline_resume()
        self.assertFalse(self.store.cues(self.job_id)[0]['translated'])
        self.assertEqual(self.store.counts(self.job_id).get('mt_reference_reused', 0), 0)
        self.store.db.execute('DROP TRIGGER reject_reuse')
        self.assertEqual(self.offline_resume(), 'completed')
        counts = self.store.counts(self.job_id)
        self.assertEqual(counts['mt_reference_reused'], 1)
        self.assertEqual(self.offline_resume(), 'completed')
        self.assertEqual(self.store.counts(self.job_id), counts)

    def test_cancel_between_reused_records_and_resume_without_duplicate_counts(self):
        self.prepare_subtitle_stage()
        cancel = threading.Event()
        save = self.store.translation_update

        def interrupt(*args, **kwargs):
            save(*args, **kwargs)
            if kwargs.get('counts', {}).get('mt_reference_reused'):
                cancel.set()

        with mock.patch.object(self.store, 'translation_update', side_effect=interrupt):
            self.assertEqual(self.offline_resume(cancel), 'cancelled')
        cues = self.store.cues(self.job_id)
        self.assertTrue(cues[0]['translated'])
        self.assertFalse(cues[1]['translated'])
        self.assertEqual(self.store.counts(self.job_id)['mt_reference_reused'], 1)
        self.assertFalse(self.source.with_suffix('.srt').exists())
        self.store.update(self.job_id, 'queued')
        self.assertEqual(self.offline_resume(), 'completed')
        self.assertEqual(self.store.counts(self.job_id)['mt_reference_reused'], 2)
        self.assertTrue(all(c['attempts'] == 0 for c in self.store.cues(self.job_id)))

    def test_strategy_one_upgrade_preserves_saved_work_and_only_reuses_missing(self):
        self.prepare_subtitle_stage()
        sources = self.store.sources(self.job_id)
        for source in sources:
            self.store.cue_update(self.job_id, next(c['cue_id'] for c in self.store.cues(self.job_id)
                                                  if c['sentence_id'] == source['sentence_id']),
                                  reason='用户在终端确认', source_lang='English')
        # Keep reference metadata consistent with the saved manual source decision.
        references = self.store.references(self.job_id)
        for reference in references:
            reference.update(reason='用户在终端确认', source_lang='English')
        cues = self.store.cues(self.job_id)
        cues[0].update(translated='保留旧策略成功译文', attempts=3)
        manifest = self.store.job(self.job_id)['manifest']
        manifest.update(translation_strategy_version=1, layout_round=2, layout_complete=False)
        self.store.save_layout(self.job_id, cues, manifest, references=references)
        before_sources = self.store.sources(self.job_id)
        before_cues = self.store.cues(self.job_id)
        self.assertEqual(self.offline_resume(), 'completed')
        after = self.store.job(self.job_id)['manifest']
        self.assertEqual(after['translation_strategy_version'], 2)
        self.assertEqual(after['layout_round'], 2)
        self.assertEqual(after['subtitle_settings'], manifest['subtitle_settings'])
        self.assertEqual(self.store.cues(self.job_id)[0], before_cues[0])
        self.assertEqual(self.store.sources(self.job_id), before_sources)
        self.assertEqual(self.store.references(self.job_id), references)
        self.assertEqual(self.store.counts(self.job_id)['mt_reference_reused'], 1)

    def test_upgrade_is_durable_when_interrupted_and_repeat_resume_is_safe(self):
        self.prepare_subtitle_stage()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['translation_strategy_version'] = 1
        self.store.update(self.job_id, manifest=manifest)
        save = self.store.update
        interrupted = False

        def interrupt(job_id, state=None, manifest=None, *args, **kwargs):
            nonlocal interrupted
            save(job_id, state, manifest, *args, **kwargs)
            if not interrupted and manifest and manifest.get('translation_strategy_version') == 2:
                interrupted = True
                raise KeyboardInterrupt()

        with mock.patch.object(self.store, 'update', side_effect=interrupt):
            self.assertEqual(self.offline_resume(), 'cancelled')
        self.assertEqual(self.store.job(self.job_id)['manifest']['translation_strategy_version'], 2)
        self.assertFalse(any(c['translated'] for c in self.store.cues(self.job_id)))
        self.store.update(self.job_id, 'queued')
        self.assertEqual(self.offline_resume(), 'completed')
        self.assertEqual(self.store.counts(self.job_id)['mt_reference_reused'], 2)

    def test_identity_and_unknown_versions_are_checked_before_upgrade_or_reuse(self):
        self.prepare_subtitle_stage()
        original = self.store.job(self.job_id)['manifest']
        for changes, error in [({'account': hashlib.sha256(b'other-account').hexdigest()}, 'account_changed'),
                               ({'subtitle_version': 99}, 'cache_version'),
                               ({'translation_strategy_version': 99}, 'cache_version')]:
            with self.subTest(changes=changes):
                manifest = {**copy.deepcopy(original), 'translation_strategy_version': 1, **changes}
                self.store.update(self.job_id, 'queued', manifest)
                self.assertEqual(self.offline_resume(), 'failed')
                saved = self.store.job(self.job_id)
                self.assertEqual(saved['error_code'], error)
                self.assertEqual(saved['manifest']['translation_strategy_version'], manifest['translation_strategy_version'])
                self.assertFalse(any(c['translated'] for c in self.store.cues(self.job_id)))

    def test_whole_sentence_reuse_does_not_poison_contextual_subtitle_cache(self):
        self.prepare_subtitle_stage()
        manifest = self.store.job(self.job_id)['manifest']
        references = self.store.references(self.job_id)
        sources = [{**s, 'reference': r['translated']} for s, r in zip(self.store.sources(self.job_id), references)]
        keys = [mt_cache_key(c['text'], c['source_lang'], **manifest['cache_identity'],
                             stage='subtitle', strategy_version=2, asr_language=c['language'],
                             tm_list=translation_context(c, sources)) for c in self.store.cues(self.job_id)]
        self.assertEqual(self.offline_resume(), 'completed')
        self.assertTrue(all(self.store.cache_get(key) is None for key in keys))

    def test_strategy_two_uses_existing_strategy_one_fragment_cache(self):
        manifest = self.store.job(self.job_id)['manifest']
        manifest['subtitle_settings'].update(zh_line_width=22, other_line_width=42)
        self.store.update(self.job_id, manifest=manifest)
        self.prepare_subtitle_stage(timed_raw([f'word{i}' for i in range(24)], word_ms=200))
        manifest = self.store.job(self.job_id)['manifest']
        manifest['translation_strategy_version'] = 1
        self.store.update(self.job_id, manifest=manifest)
        reference = self.store.references(self.job_id)[0]
        source = {**self.store.sources(self.job_id)[0], 'reference': reference['translated']}
        cues = self.store.cues(self.job_id)
        self.assertGreater(len(cues), 1)
        with self.store.db:
            for i, cue in enumerate(cues):
                key = mt_cache_key(cue['text'], cue['source_lang'], **manifest['cache_identity'], stage='subtitle',
                                   strategy_version=1, asr_language=cue['language'], tm_list=translation_context(cue, [source]))
                self.store.db.execute('INSERT INTO mt_cache VALUES(?,?)', (key, f'旧片段缓存{i}'))
        self.assertEqual(self.offline_resume(), 'completed')
        self.assertEqual(self.store.job(self.job_id)['manifest']['translation_strategy_version'], 2)
        self.assertEqual(self.store.counts(self.job_id)['mt_subtitle_cache_hit'], len(cues))
        self.assertEqual([c['translated'] for c in self.store.cues(self.job_id)], [f'旧片段缓存{i}' for i in range(len(cues))])

    def test_reused_translation_is_checked_against_saved_layout_and_split_if_needed(self):
        manifest = self.store.job(self.job_id)['manifest']
        manifest['subtitle_settings'].update(zh_line_width=22, other_line_width=42)
        self.store.update(self.job_id, manifest=manifest)
        self.prepare_subtitle_stage(timed_raw(['alpha', 'bravo', 'charlie', 'delta'], word_ms=1200))
        manifest = self.store.job(self.job_id)['manifest']
        references = self.store.references(self.job_id)
        references[0]['translated'] = '中' * 50
        self.store.save_layout(self.job_id, self.store.cues(self.job_id), manifest, references=references)

        def translated(method, url, **kwargs):
            self.assertTrue(url.endswith('/chat/completions'))
            self.assertEqual(kwargs['json']['translation_options']['tm_list'][0]['target'], '中' * 50)
            return response({'choices': [{'finish_reason': 'stop', 'message': {'content': '短片段译文'}}]})

        with mock.patch('requests.request', side_effect=translated) as request:
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'completed')
        cues = self.store.cues(self.job_id)
        self.assertEqual((request.call_count, len(cues)), (2, 2))
        self.assertTrue(all(c['translated'] == '短片段译文' for c in cues))
        self.assertEqual(self.store.job(self.job_id)['manifest']['layout_round'], 1)
        self.assertEqual(self.store.counts(self.job_id)['mt_reference_reused'], 1)

    def test_completed_strategy_one_job_does_not_rewrite_subtitles(self):
        self.assertEqual(self.run_fake(FakeService(result(('Saved sentence.', 'en')))), 'completed')
        manifest = self.store.job(self.job_id)['manifest']
        manifest['translation_strategy_version'] = 1
        self.store.update(self.job_id, manifest=manifest)
        before = self.store.job(self.job_id)
        output = self.source.with_suffix('.srt')
        old, modified = output.read_bytes(), output.stat().st_mtime_ns
        self.assertEqual(self.offline_resume(), 'completed')
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), (old, modified))
