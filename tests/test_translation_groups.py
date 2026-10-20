from __future__ import annotations

import sqlite3
import threading
from unittest import mock

from subtitle_tool.api import Bailian, RequestPacer
from subtitle_tool.common import Cancelled, ToolError
from subtitle_tool.config import Config
from subtitle_tool.engine import Engine
from subtitle_tool.store import Store
from subtitle_tool.subtitles import mt_cache_key
from tests.fixtures import FakeService, PipelineFixture, TemporaryTest, response, result, timed_raw


PHASES = [('legacy', {}), ('reference', {'reference_stage': True, 'strategy_version': 2}),
          ('subtitle', {'strategy_version': 2})]


class TranslationGroupTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        self.store = Store(self.config.data / 'jobs.sqlite')
        self.scope = dict(region='beijing', account='offline-account', workspace_id='offline-space',
                          access_mode='workspace', processing_version=2)
        self.cancel = threading.Event()
        self.progress = []
        self.engine = Engine(self.config, self.store, self.cancel,
                             lambda job, stage, done, total: self.progress.append((done, total)))
        self.sequence = 0

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def prepare(self, texts, *, reference_stage=False):
        self.sequence += 1
        source = self.path / f'video-{self.sequence}.mkv'
        source.write_bytes(b'fixture')
        job_id = self.store.enqueue(source, workspace_id='offline-space')
        records = [dict(cue_id=str(i), begin_ms=i * 1000, end_ms=i * 1000 + 900, text=text,
                        language='en', decision='translate', reason='offline', translated='',
                        source_lang='auto', attempts=i, error='previous error') for i, text in enumerate(texts)]
        self.store.save_layout(job_id, records, self.store.job(job_id)['manifest'],
                               references=records if reference_stage else None)
        return job_id

    def records(self, job_id, options):
        return (self.store.references if options.get('reference_stage') else self.store.cues)(job_id)

    def client(self, job_id):
        return Bailian('offline-group-key', 'beijing', self.cancel, workspace_id='offline-space',
                       access_mode='workspace', count=lambda name: self.store.count(job_id, name))

    def translate(self, job_id, options, handler):
        with mock.patch('requests.request', side_effect=handler):
            self.engine.translate(job_id, self.records(job_id, options), self.scope,
                                  lambda: self.client(job_id), **options)

    def key(self, record, options, context=()):
        return mt_cache_key(record['text'], record['source_lang'], **self.scope,
                            stage='reference' if options.get('reference_stage') else 'subtitle',
                            strategy_version=options.get('strategy_version', 0), tm_list=context,
                            asr_language=record['language'].lower().strip())

    @staticmethod
    def success(*args, **kwargs):
        return response({'id': 'offline-mt', 'choices': [{'finish_reason': 'stop', 'message': {'content': '重复译文'}}]})

    def test_four_identical_records_make_one_request_at_each_phase_and_concurrency(self):
        for phase, options in PHASES:
            for workers in [1, 4]:
                with self.subTest(phase=phase, workers=workers):
                    self.config.settings.mt_workers = workers
                    job_id = self.prepare([f'Repeated {phase} {workers}.'] * 4,
                                          reference_stage=options.get('reference_stage', False))
                    handler = mock.Mock(side_effect=self.success)
                    self.translate(job_id, options, handler)
                    records = self.records(job_id, options)
                    self.assertEqual(handler.call_count, 1)
                    self.assertEqual([r['translated'] for r in records], ['重复译文'] * 4)
                    self.assertEqual([r['attempts'] for r in records], [1, 2, 3, 4])
                    self.assertTrue(all(not r['error'] for r in records))
                    counts = self.store.counts(job_id)
                    stage = 'reference' if options.get('reference_stage') else 'subtitle'
                    self.assertEqual(counts['mt'], 1)
                    self.assertEqual(counts['mt_' + stage + '_completed'], 4)
                    self.assertEqual(counts.get('mt_' + stage + '_cache_hit', 0), 0)
                    if options.get('strategy_version'):
                        self.assertEqual(counts['mt_' + stage + '_request'], 1)
                    self.assertEqual(self.progress[-1], (4, 4))

    def test_full_key_keeps_context_source_language_and_asr_language_isolated(self):
        options = {'strategy_version': 1}
        job_id = self.prepare(['Same input.'] * 5)
        with self.store.db:
            self.store.db.execute("UPDATE cues SET source_lang='English' WHERE job_id=? AND cue_id='2'", (job_id,))
            self.store.db.execute("UPDATE cues SET language='fr' WHERE job_id=? AND cue_id='3'", (job_id,))
        a = [{'source': 'Context', 'target': '甲'}]
        b = [{'source': 'Context', 'target': '乙'}]
        options['contexts'] = {'0': a, '1': b, '2': a, '3': a, '4': a}
        handler = mock.Mock(side_effect=self.success)
        self.translate(job_id, options, handler)
        self.assertEqual(handler.call_count, 4)
        self.assertTrue(all(r['translated'] for r in self.store.cues(job_id)))
        self.assertEqual(self.store.counts(job_id)['mt_subtitle_completed'], 5)

    def test_persistent_cache_updates_all_members_without_request_or_attempt(self):
        for phase, options in PHASES:
            with self.subTest(phase=phase):
                job_id = self.prepare([f'Cached {phase}.'] * 4, reference_stage=options.get('reference_stage', False))
                with self.store.db:
                    self.store.db.execute('INSERT INTO mt_cache VALUES(?,?)',
                                          (self.key(self.records(job_id, options)[0], options), '缓存译文'))
                handler = mock.Mock(side_effect=AssertionError('must not request'))
                self.translate(job_id, options, handler)
                handler.assert_not_called()
                records = self.records(job_id, options)
                self.assertEqual([r['translated'] for r in records], ['缓存译文'] * 4)
                self.assertEqual([r['attempts'] for r in records], [0, 1, 2, 3])
                counts = self.store.counts(job_id)
                stage = 'reference' if options.get('reference_stage') else 'subtitle'
                self.assertEqual(counts.get('mt_' + stage + '_completed', 0), 0)
                if options.get('strategy_version'):
                    self.assertEqual(counts['mt_' + stage + '_cache_hit'], 4)
                self.assertEqual(self.progress[-1], (4, 4))

    def test_failure_updates_group_stops_new_groups_and_saves_inflight_successes(self):
        for phase, options in PHASES:
            for workers in [1, 4]:
                with self.subTest(phase=phase, workers=workers):
                    self.config.settings.mt_workers = workers
                    prefix = f'{phase} {workers}'
                    texts = [prefix + ' fail'] * 4 + [prefix + ' success'] * 4 + [prefix + ' third', prefix + ' fourth', prefix + ' fifth']
                    job_id = self.prepare(texts, reference_stage=options.get('reference_stage', False))
                    barrier, failure_saved = threading.Barrier(workers), threading.Event()
                    def progress(job, stage, done, total):
                        if all(r['error'] != 'previous error' for r in self.records(job, options)[:4]):
                            failure_saved.set()
                    self.engine.progress = progress
                    def handler(method, url, **kwargs):
                        barrier.wait(timeout=5)
                        if kwargs['json']['messages'][0]['content'].endswith(' fail'):
                            return response({'code': 'Unauthorized'}, 403)
                        if not failure_saved.wait(5):
                            raise AssertionError('failed group was not saved')
                        return self.success()
                    request = mock.Mock(side_effect=handler)
                    with self.assertRaises(ToolError):
                        self.translate(job_id, options, request)
                    self.assertEqual(request.call_count, workers)
                    records = self.records(job_id, options)
                    self.assertTrue(all(r['error'] and r['error'] != 'previous error' for r in records[:4]))
                    self.assertTrue(all(not r['translated'] for r in records[:4]))
                    if workers == 4:
                        self.assertTrue(all(r['translated'] for r in records[4:10]))
                    self.assertFalse(records[-1]['translated'])
                    self.assertEqual(records[-1]['attempts'], 10)
                    self.engine.progress = lambda *args: None
                    retry = mock.Mock(side_effect=self.success)
                    self.translate(job_id, options, retry)
                    self.assertEqual(retry.call_count, 2 if workers == 4 else 5)
                    self.assertTrue(all(r['translated'] and not r['error'] for r in self.records(job_id, options)))
                    self.assertEqual([r['attempts'] for r in self.records(job_id, options)[:4]], [2, 3, 4, 5])

    def test_cancel_saves_whole_group_and_resume_uses_cache_and_missing_groups(self):
        for phase, options in PHASES:
            with self.subTest(phase=phase):
                self.cancel.clear()
                self.config.settings.mt_workers = 1
                job_id = self.prepare([phase + ' first'] * 4 + [phase + ' second'] * 4,
                                      reference_stage=options.get('reference_stage', False))
                def cancel_after_success(*args, **kwargs):
                    self.cancel.set()
                    return self.success()
                request = mock.Mock(side_effect=cancel_after_success)
                with self.assertRaises(Cancelled):
                    self.translate(job_id, options, request)
                self.assertEqual(request.call_count, 1)
                records = self.records(job_id, options)
                self.assertTrue(all(r['translated'] for r in records[:4]))
                self.assertTrue(all(not r['translated'] for r in records[4:]))
                self.assertEqual([r['attempts'] for r in records[4:]], [4, 5, 6, 7])
                self.cancel.clear()
                self.store.translation_update(job_id, ['0'], reference_stage=options.get('reference_stage', False),
                                              translated='')
                retry = mock.Mock(side_effect=self.success)
                self.translate(job_id, options, retry)
                self.assertEqual(retry.call_count, 1)
                self.assertTrue(all(r['translated'] for r in self.records(job_id, options)))
                counts = self.store.counts(job_id)
                stage = 'reference' if options.get('reference_stage') else 'subtitle'
                self.assertEqual(counts['mt_' + stage + '_completed'], 8)
                if options.get('strategy_version'):
                    self.assertEqual(counts['mt_' + stage + '_cache_hit'], 1)
                self.assertEqual(self.progress[-1], (8, 8))

    def test_group_result_and_cache_roll_back_together_on_database_failure(self):
        job_id = self.prepare(['Atomic input.'] * 4)
        records = self.store.cues(job_id)
        self.store.db.execute("CREATE TEMP TRIGGER reject_translation BEFORE UPDATE OF translated ON cues "
                              "WHEN NEW.cue_id='3' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.translate(job_id, {}, self.success)
        self.assertTrue(all(not r['translated'] for r in self.store.cues(job_id)))
        self.assertIsNone(self.store.cache_get(self.key(records[0], {})))
        self.assertEqual(self.store.counts(job_id).get('mt_subtitle_completed', 0), 0)


class TranslationGroupPipelineTests(PipelineFixture):
    translation_strategy_version = 2
    fixture_seconds = 20

    def test_throttle_cancel_and_resume_keep_successes_at_each_translation_phase(self):
        for phase, target_stage in [('legacy', 'translating'), ('reference', 'reference_translating'),
                                    ('subtitle', 'subtitle_translating')]:
            with self.subTest(phase=phase):
                self.config.settings.mt_workers = 1
                source = self.path / f'throttle-{phase}.mkv'
                source.write_bytes(self.source.read_bytes())
                job_id = self.store.enqueue(source, workspace_id='offline-space')
                manifest = self.store.job(job_id)['manifest']
                manifest['translation_strategy_version'] = 2
                self.store.update(job_id, manifest=manifest)
                if phase == 'legacy':
                    manifest = self.store.job(job_id)['manifest']
                    for name in ['subtitle_version', 'translation_strategy_version', 'subtitle_settings']:
                        manifest.pop(name)
                    self.store.update(job_id, manifest=manifest)
                raw = result(*[(f'{phase} line {i}.', 'en') for i in range(8)])
                for index, sentence in enumerate(raw['transcripts'][0]['sentences']):
                    sentence.update(begin_time=index * 500, end_time=index * 500 + 400)
                if phase == 'subtitle':
                    raw = timed_raw([f'word{i}' for i in range(32)], word_ms=500)
                    # Eight real fragments, each requiring its fixed parent reference.
                    manifest = self.store.job(job_id)['manifest']
                    manifest['subtitle_settings']['other_line_width'] = 24
                    self.store.update(job_id, manifest=manifest)
                fake = FakeService(raw)
                cancel = threading.Event()
                now, events, mt_calls = [0.0], [], []
                pacer = RequestPacer(clock=lambda: now[0])
                current_stage, interrupted = None, False

                def advance(seconds):
                    now[0] += seconds
                    return cancel.is_set()

                def progress(job, stage, done, total):
                    nonlocal current_stage, interrupted
                    events.append((stage, done, total))
                    if stage in {'translating', 'reference_translating', 'subtitle_translating'}:
                        current_stage = stage
                    if stage == 'mt_cooldown' and not interrupted:
                        interrupted = True
                        cancel.set()

                def request(method, url, **kwargs):
                    if url.endswith('/chat/completions'):
                        mt_calls.append((current_stage, kwargs['json']['messages'][0]['content']))
                        if current_stage == target_stage and not interrupted:
                            calls = sum(stage == target_stage for stage, _ in mt_calls)
                            if calls == 2:
                                fake.calls.append((method, url, kwargs))
                                return response({}, 429, {'Retry-After': '15'})
                    return fake.request(method, url, **kwargs)

                engine = Engine(self.config, self.store, cancel, progress)
                records = self.store.references if phase == 'reference' else self.store.cues
                with mock.patch('subtitle_tool.api.mt_pacer', return_value=pacer), \
                        mock.patch.object(cancel, 'wait', side_effect=advance), \
                        mock.patch('requests.request', side_effect=request), \
                        mock.patch('requests.post', side_effect=fake.upload):
                    self.assertEqual(engine.run(job_id), 'cancelled')
                    saved = records(job_id)
                    self.assertTrue(saved[0]['translated'])
                    self.assertTrue(all(not record['translated'] for record in saved[1:]))
                    self.assertFalse(source.with_suffix('.srt').exists())
                    before = self.store.counts(job_id)
                    self.assertEqual(before['mt_429'], 1)
                    self.assertEqual(before['mt_cooldown_ms'], 15000)
                    cancel.clear()
                    self.store.update(job_id, 'queued')
                    self.assertEqual(engine.run(job_id), 'completed')
                restored = records(job_id)
                self.assertTrue(all(record['translated'] for record in restored))
                self.assertEqual(restored[0]['translated'], saved[0]['translated'])
                self.assertEqual(restored[0]['attempts'], saved[0]['attempts'])
                self.assertEqual(sum(stage == target_stage and text == saved[0]['text']
                                     for stage, text in mt_calls), 1)
                self.assertEqual(sum(stage == target_stage for stage, _ in mt_calls), 9)
                self.assertTrue(source.with_suffix('.srt').exists())
                after = self.store.counts(job_id)
                for name in ['upload_policy', 'upload', 'asr_submit', 'asr_query', 'result_download']:
                    self.assertEqual(after.get(name, 0), before.get(name, 0))
                self.assertEqual(after['mt_429'], 1)
                self.assertEqual(after['mt_cooldown_ms'], 15000)
                recoveries = [event for event in events if event[0] == 'mt_recovery']
                self.assertTrue(recoveries)
                self.assertEqual(recoveries[0][1], 1.05)
                self.assertAlmostEqual(recoveries[0][2], 0.735)

    def test_legacy_and_two_stage_flows_deduplicate_without_changing_record_counts(self):
        for legacy in [True, False]:
            for workers in [1, 4]:
                with self.subTest(legacy=legacy, workers=workers):
                    source = self.path / f'video-{legacy}-{workers}.mkv'
                    source.write_bytes(self.source.read_bytes())
                    self.job_id = self.store.enqueue(source, workspace_id='offline-space')
                    manifest = self.store.job(self.job_id)['manifest']
                    manifest['translation_strategy_version'] = 2
                    self.store.update(self.job_id, manifest=manifest)
                    self.config.settings.mt_workers = workers
                    if legacy:
                        manifest = self.store.job(self.job_id)['manifest']
                        for name in ['subtitle_version', 'translation_strategy_version', 'subtitle_settings']:
                            manifest.pop(name)
                        self.store.update(self.job_id, manifest=manifest)
                    raw = result(*[(f'Repeated {legacy} {workers}.', 'en')] * 4)
                    fake = FakeService(raw)
                    self.assertEqual(self.run_fake(fake), 'completed')
                    self.assertEqual(len(fake.named('/chat/completions')), 1)
                    self.assertEqual(len(self.store.cues(self.job_id)), 4)
                    self.assertTrue(all(c['translated'] for c in self.store.cues(self.job_id)))
                    self.assertEqual(source.with_suffix('.srt').read_text(encoding='utf-8').count('-->'), 4)
                    if not legacy:
                        self.assertEqual(len(self.store.references(self.job_id)), 4)
                        counts = self.store.counts(self.job_id)
                        self.assertEqual((counts['mt_reference_request'], counts.get('mt_subtitle_request', 0)), (1, 0))
                        self.assertEqual(counts['mt_reference_completed'], 4)
                        self.assertEqual(counts.get('mt_subtitle_completed', 0), 0)
                        self.assertEqual(counts['mt_reference_reused'], 4)

    def test_four_identical_fragments_share_one_parent_context_and_one_request(self):
        for workers in [1, 4]:
            with self.subTest(workers=workers):
                source = self.path / f'fragments-{workers}.mkv'
                source.write_bytes(self.source.read_bytes())
                self.config.settings.other_line_width = 24
                self.config.settings.mt_workers = workers
                self.job_id = self.store.enqueue(source, workspace_id='offline-space', settings=self.config.settings)
                manifest = self.store.job(self.job_id)['manifest']
                manifest['translation_strategy_version'] = 2
                self.store.update(self.job_id, manifest=manifest)
                fake = FakeService(timed_raw([f'word{workers}'] * 24, word_ms=200))
                self.assertEqual(self.run_fake(fake), 'completed')
                cues = self.store.cues(self.job_id)
                self.assertEqual(len(cues), 4)
                self.assertEqual(len({c['text'] for c in cues}), 1)
                self.assertTrue(all(c['translated'] for c in cues))
                self.assertEqual(len(fake.named('/chat/completions')), 2)
                counts = self.store.counts(self.job_id)
                self.assertEqual((counts['mt_reference_request'], counts['mt_subtitle_request']), (1, 1))
                self.assertEqual((counts['mt_reference_completed'], counts['mt_subtitle_completed']), (1, 4))
