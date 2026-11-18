"""Ordered terminology with overlapping synthetic requests and real recovery storage."""
import threading
from concurrent.futures import wait
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.mt_pipeline import Pipeline
from tests.fixtures import result
from tests.mt_fixtures import PoolPipelineFixture


class GlossarySchedulingTests(PoolPipelineFixture):
    def install_windows(self, *, legacy=False, count=32):
        self.config.settings.mt_workers = 4
        raw = result(*[(f'Cache stores item {i}.', 'en') for i in range(count)])
        step = min(100, 10000 // count)
        for i, sentence in enumerate(raw['transcripts'][0]['sentences']):
            sentence.update(begin_time=i * step, end_time=(i + 1) * step - 10)
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id)
        if legacy:
            manifest = self.store.job(self.job_id)['manifest']
            manifest['mt_glossary_schedule'] = 'window-prefix-v3'
            self.store.update(self.job_id, manifest=manifest)
        fit = Pipeline._fit

        def one_group(pipeline, stage, data, **kwargs):
            if stage == 'glossary' and len(data['sentences']) > 8:
                raise ToolError('Synthetic window budget', 'mt_input_budget')
            return fit(pipeline, stage, data, **kwargs)

        patch = mock.patch.object(Pipeline, '_fit', one_group)
        patch.start()
        self.addCleanup(patch.stop)
        self.glossary_terms = lambda data: [{'source': 'Cache', 'translation': '缓存',
                                            'sentence': data['sentences'][0]['id']}]

    def test_small_windows_can_feed_all_configured_workers(self):
        self.install_windows(count=20 * 8)
        self.config.settings.mt_workers = 20
        gate = threading.Barrier(20, timeout=10)
        def wait_for_drafts(stage, data):
            if stage == 'translate':
                gate.wait()
        self.stage_hook = wait_for_drafts
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(sum(stage == 'translate' for stage, _, _ in self.calls), 20)

    def test_slow_first_window_does_not_block_next_extraction_or_reorder_evidence(self):
        self.install_windows()
        extract = Pipeline.glossary
        second_saved = threading.Event()
        overlap, completion = [], []

        def reordered(pipeline, group, context, known=()):
            first = group[0]['text'] == 'Cache stores item 0.'
            second = group[0]['text'] == 'Cache stores item 8.'
            if first:
                overlap.append(second_saved.wait(2))
            value = extract(pipeline, group, context, known)
            completion.append(group[0]['text'])
            if second:
                second_saved.set()
            return value

        with mock.patch.object(Pipeline, 'glossary', reordered):
            self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(overlap, [True], 'A slow glossary request serialized the next independent window')
        self.assertEqual(completion[0], 'Cache stores item 8.')
        windows = [data for stage, _, data in self.calls if stage == 'glossary']
        self.assertTrue(all(not data['glossary'] for data in windows))
        drafts = [data for stage, _, data in self.calls if stage == 'translate']
        drafts.sort(key=lambda data: int(data['sentences'][0]['text'].split()[-1][:-1]))
        self.assertEqual([data['sentences'][0]['text'] for data in drafts],
                         [f'Cache stores item {i}.' for i in (0, 8, 16, 24)])
        for data in drafts:
            self.assertEqual([term['sentence'] for term in data['glossary']],
                             [data['sentences'][0]['source_id']])

    def test_cancelled_prefetch_is_reused_with_changed_worker_count(self):
        self.install_windows()
        extract = Pipeline.glossary
        second_saved = threading.Event()
        engine = Engine(self.config, self.store)
        overlap = []

        def cancelled(pipeline, group, context, known=()):
            if group[0]['text'] == 'Cache stores item 0.':
                overlap.append(second_saved.wait(2))
                engine.request_cancel()
            value = extract(pipeline, group, context, known)
            if group[0]['text'] == 'Cache stores item 8.':
                second_saved.set()
            return value

        with mock.patch.object(Pipeline, 'glossary', cancelled):
            self.assertEqual(self.run_pool(engine=engine), 'cancelled')
        self.assertEqual(overlap, [True])
        saved = [row for row in self.store.mt_stages(self.job_id) if row['stage'] == 'glossary']
        self.assertEqual(len(saved), 1)
        self.assertFalse(any(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.calls.clear()
        self.config.settings.mt_workers = 1
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        windows = [data for stage, _, data in self.calls if stage == 'glossary']
        self.assertEqual(len(windows), 3)
        self.assertNotIn('Cache stores item 8.', [data['sentences'][0]['text'] for data in windows])

    def test_completed_future_window_still_counts_against_prefetch_limit(self):
        self.install_windows()
        extract = Pipeline.glossary
        second_saved, release_first = threading.Event(), threading.Event()
        seen_completion, outstanding = False, []

        def blocked(pipeline, group, context, known=()):
            if group[0]['text'] == 'Cache stores item 0.':
                release_first.wait(5)
            value = extract(pipeline, group, context, known)
            if group[0]['text'] == 'Cache stores item 8.':
                second_saved.set()
            return value

        def observe(futures, **kwargs):
            nonlocal seen_completion
            if seen_completion and not release_first.is_set():
                # The second window has been processed by the scheduler, but
                # cannot be consumed yet. Only the blocked first may remain.
                outstanding.append(len(futures))
                release_first.set()
            completed, pending = wait(futures, **kwargs)
            if completed and second_saved.is_set():
                seen_completion = True
            return completed, pending

        try:
            with mock.patch.object(Pipeline, 'glossary', blocked), \
                    mock.patch('subtitle_tool.mt_pipeline.wait', observe):
                self.assertEqual(self.run_pool(), 'completed')
        finally:
            release_first.set()
        self.assertEqual(outstanding, [1])

    def test_legacy_resume_keeps_the_saved_serial_prefix_contract(self):
        self.install_windows(legacy=True)
        self.config.settings.mt_workers = 1
        self.cancel_after = 'translate'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        before = next(data for stage, _, data in self.calls if stage == 'translate')
        self.calls.clear()
        self.cancel_after = None
        self.config.settings.mt_workers = 4
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        windows = [data for stage, _, data in self.calls if stage == 'glossary']
        self.assertEqual(len(windows), 3)
        self.assertTrue(all(data['glossary'] for data in windows))
        self.assertEqual(windows[0]['glossary'], before['glossary'])
        self.assertEqual(self.store.job(self.job_id)['manifest']['mt_glossary_schedule'], 'window-prefix-v3')
