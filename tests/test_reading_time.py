"""Reading time is a display decision, independent of saved speech evidence."""
import copy
from pathlib import Path
from unittest import mock
import unittest

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.subtitles import plan_display_timing
from tests.fixtures import timed_raw, srt_intervals
from tests.mt_fixtures import PoolPipelineFixture


def cue(number, begin, end, *, segment='0000'):
    identity = f'{segment}:{number:08d}'
    return {'cue_id': identity + ':0000', 'sentence_id': identity,
            'begin_ms': begin, 'end_ms': end}


def short_raw():
    raw = timed_raw(['Why?'], word_ms=200, start=1000)
    raw['transcripts'][0]['sentences'].extend(
        timed_raw(['The', 'voltage', 'increases.'], word_ms=500, start=2500)['transcripts'][0]['sentences'])
    return raw


class ReadingTimeTests(unittest.TestCase):
    def test_short_speech_uses_following_silence_without_moving_its_start(self):
        cues = [cue(0, 1000, 1080), cue(1, 2500, 4500)]
        before = copy.deepcopy(cues)
        intervals, changes = plan_display_timing(cues)
        self.assertEqual([(i['begin_ms'], i['end_ms']) for i in intervals], [(1000, 2000), (2500, 4500)])
        self.assertEqual(changes, [dict(cue_id=cues[0]['cue_id'], old_begin_ms=1000, old_end_ms=1080,
                                      new_begin_ms=1000, new_end_ms=2000, adjustment_ms=-920,
                                      reason='minimum_reading_time')])
        self.assertEqual(cues, before)

    def test_only_available_silence_is_used_and_normal_cues_keep_their_times(self):
        cues = [cue(0, 1000, 1200), cue(1, 1600, 1800), cue(2, 2600, 4600)]
        intervals, _ = plan_display_timing(cues)
        self.assertEqual([(i['begin_ms'], i['end_ms']) for i in intervals],
                         [(1000, 1600), (1600, 2600), (2600, 4600)])

    def test_never_crosses_a_segment_or_guesses_the_last_media_boundary(self):
        cues = [cue(0, 1000, 1200), cue(0, 2500, 2700, segment='0001')]
        intervals, changes = plan_display_timing(cues)
        self.assertEqual([(i['begin_ms'], i['end_ms']) for i in intervals], [(1000, 1200), (2500, 2700)])
        self.assertEqual(changes, [])

    def test_unknown_or_backwards_source_identity_does_not_authorize_a_hold(self):
        for following in [cue(0, 2500, 4500), {'cue_id': 'unknown', 'begin_ms': 2500, 'end_ms': 4500}]:
            with self.subTest(following=following['cue_id']):
                intervals, changes = plan_display_timing([cue(1, 1000, 1200), following])
                self.assertEqual(intervals[0]['end_ms'], 1200)
                self.assertEqual(changes, [])

    def test_overlap_repairs_cannot_borrow_time_back_from_the_next_speech(self):
        cues = [cue(0, 1000, 1800), cue(1, 1500, 5500), cue(2, 6000, 8000)]
        intervals, changes = plan_display_timing(cues)
        self.assertEqual([(i['begin_ms'], i['end_ms']) for i in intervals],
                         [(1000, 1800), (1800, 5500), (6000, 8000)])
        self.assertEqual([c['reason'] for c in changes], ['bounded_sentence_overlap_start_delay'])

    def test_v3_keeps_exact_previous_display_times(self):
        cues = [cue(0, 1000, 1080), cue(1, 2500, 4500)]
        intervals, changes = plan_display_timing(cues, version=3)
        self.assertEqual([(i['begin_ms'], i['end_ms']) for i in intervals], [(1000, 1080), (2500, 4500)])
        self.assertEqual(changes, [])

    def test_unsupported_display_version_is_not_silently_reinterpreted(self):
        for version in (None, True, 4.0, 2, 5):
            with self.subTest(version=version):
                with self.assertRaises(ToolError) as caught:
                    plan_display_timing([cue(0, 1000, 1200)], version=version)
                self.assertEqual(caught.exception.code, 'cache_version')


class ReadingTimePipelineTests(PoolPipelineFixture):
    def prepare(self, version=None):
        self.save_result(short_raw())
        Engine(self.config, self.store).reflow(self.job_id)
        if version is not None:
            manifest = self.store.job(self.job_id)['manifest']
            manifest['display_timing_version'] = version
            self.store.update(self.job_id, manifest=manifest)
        self.output = self.source.with_suffix('.srt')
        self.saved_sources = self.store.sources(self.job_id)

    def test_translation_review_and_publication_share_the_readable_duration(self):
        self.prepare()
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(srt_intervals(self.output.read_bytes()), [(1000, 2000), (2500, 4000)])
        for stage in ('translate', 'review'):
            data = next(data for name, _, data in self.calls if name == stage)
            self.assertEqual(data['fragments'][0]['display_ms'], 1000)
        self.assertEqual(self.store.sources(self.job_id), self.saved_sources)
        self.assertEqual(self.store.cues(self.job_id)[0]['end_ms'], 1200)

    def test_old_resume_uses_v3_but_explicit_retime_upgrades_without_cloud(self):
        self.prepare(version=3)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(srt_intervals(self.output.read_bytes()), [(1000, 1200), (2500, 4000)])
        data = next(data for name, _, data in self.calls if name == 'translate')
        self.assertEqual(data['fragments'][0]['display_ms'], 200)
        before = self.output.read_bytes()
        cues, stages, counts = (self.store.cues(self.job_id), self.store.mt_stages(self.job_id),
                               self.store.counts(self.job_id))
        self.calls.clear()
        engine = Engine(self.config, self.store)
        engine.retime(self.job_id, overwrite=True)
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['display_timing_version'], 4)
        self.assertEqual(manifest['retime']['from_display_timing_version'], 3)
        self.assertEqual(self.output.read_bytes(), before)
        with mock.patch('subtitle_tool.engine.write_srt', side_effect=OSError('synthetic publication interruption')):
            self.assertEqual(self.run_pool(), 'failed')
        intent = copy.deepcopy(self.store.job(self.job_id)['manifest']['publication_intent'])
        engine.resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(srt_intervals(self.output.read_bytes()), [(1000, 2000), (2500, 4000)])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.cues(self.job_id), cues)
        self.assertEqual(self.store.mt_stages(self.job_id), stages)
        self.assertEqual(self.store.sources(self.job_id), self.saved_sources)
        self.assertEqual(self.store.counts(self.job_id)['mt'], counts['mt'])
        self.assertEqual(self.store.job(self.job_id)['manifest']['publication_intent'], intent)
        self.assertEqual(Path(intent['backup']).read_bytes(), before)

    def test_old_pending_publication_resumes_identical_bytes(self):
        self.prepare(version=3)
        with mock.patch('subtitle_tool.engine.write_srt', side_effect=OSError('synthetic publication interruption')):
            self.assertEqual(self.run_pool(), 'failed')
        before = copy.deepcopy(self.store.job(self.job_id)['manifest'])
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(self.calls, [])
        self.assertEqual(srt_intervals(self.output.read_bytes()), [(1000, 1200), (2500, 4000)])
        after = self.store.job(self.job_id)['manifest']
        self.assertEqual(after['publication_intent'], before['publication_intent'])
        self.assertEqual(after['display_timing_adjustments'], before['display_timing_adjustments'])
