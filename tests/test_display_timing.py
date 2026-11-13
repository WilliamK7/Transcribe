from __future__ import annotations

import copy
import hashlib
import unittest
from unittest import mock

import subtitle_tool.engine as engine_module
from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.main import summary
from subtitle_tool.subtitles import plan_display_timing

from tests.fixtures import FakeService, SavedResultFixture, overlapping_raw, srt_intervals, timed_raw


def short_first_raw():
    raw = timed_raw(['One', 'short', 'line.'], word_ms=200, start=1000, pauses={2: 200})
    following = timed_raw([f'bravo{i}' for i in range(8)], word_ms=560, start=1500, pauses={1: 240})
    raw['transcripts'][0]['sentences'].extend(following['transcripts'][0]['sentences'])
    return raw


class DisplayTimingPipelineTests(SavedResultFixture):
    def test_short_cue_keeps_its_full_duration_when_the_next_start_can_move(self):
        raw = short_first_raw()
        sources = self.save_result(raw)
        fake = FakeService(raw)
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(srt_intervals(self.source.with_suffix('.srt').read_bytes()),
                         [(1000, 1800), (1800, 6220)])
        self.assertEqual(self.store.sources(self.job_id), sources)
        self.assertEqual([(c['begin_ms'], c['end_ms']) for c in self.store.cues(self.job_id)],
                         [(1000, 1800), (1500, 6220)])
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['display_timing_adjustments'], [dict(
            cue_id=sources[1]['cue_id'], old_begin_ms=1500, old_end_ms=6220,
            new_begin_ms=1800, new_end_ms=6220, adjustment_ms=300,
            reason='bounded_sentence_overlap_start_delay')])
        self.assertTrue(fake.named('/chat/completions'))
        self.assertEqual(fake.calls, fake.named('/chat/completions'))

    def test_reflow_rebuilds_a_retired_v2_job_using_saved_asr(self):
        raw = short_first_raw()
        sources = self.save_result(raw)
        raw_path = self.cache / 'raw-0000.json'
        raw_bytes = raw_path.read_bytes()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['display_timing_version'] = 2
        self.store.update(self.job_id, manifest=manifest)
        engine = Engine(self.config, self.store)
        fake = FakeService(raw)
        self.assertEqual(self.run_fake(fake), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_version')
        self.assertEqual(fake.calls, [])
        self.assertEqual(summary(self.store, self.job_id)['lifecycle']['next_actions'][0]['id'], 'reflow')

        self.assertEqual(engine.reflow(self.job_id), self.job_id)
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['display_timing_version'], 3)
        self.assertTrue((self.config.root / manifest['reflow']['snapshot']).is_file())
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(raw_path.read_bytes(), raw_bytes)
        self.assertEqual(self.store.sources(self.job_id), sources)
        self.assertEqual(fake.calls, fake.named('/chat/completions'))

    def test_new_task_trims_only_the_display_end_of_a_small_overlap(self):
        raw = overlapping_raw()
        self.save_result(raw)
        self.assertEqual(self.run_fake(FakeService(raw)), "completed")
        content = self.source.with_suffix(".srt").read_bytes()
        self.assertEqual(srt_intervals(content), [(1000, 4960), (4960, 8320)])
        manifest = self.store.job(self.job_id)["manifest"]
        self.assertEqual(manifest["display_timing_version"], 3)
        self.assertEqual(manifest["display_timing_adjustments"][0]["adjustment_ms"], 40)
        self.assertEqual(manifest["expected_srt_sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(manifest["publication_intent"]["expected_srt_sha256"], manifest["expected_srt_sha256"])
        self.assertEqual([(c["begin_ms"], c["end_ms"]) for c in self.store.cues(self.job_id)],
                         [(1000, 5000), (4960, 8320)])
        Engine(self.config, self.store).validate_saved_subtitles(self.job_id, manifest)


    def test_unrepairable_overlap_stops_before_translation_and_preserves_output(self):
        raw = overlapping_raw(overlap=501)
        self.save_result(raw)
        output = self.source.with_suffix(".srt")
        output.write_bytes(b"original subtitles")
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["overwrite"] = True
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(raw)
        self.assertEqual(self.run_fake(fake), "failed")
        job = self.store.job(self.job_id)
        self.assertEqual(job["error_code"], "timeline")
        self.assertFalse(job["manifest"].get("layout_complete"))
        self.assertFalse(any(c["translated"] for c in self.store.cues(self.job_id)))
        self.assertEqual(fake.calls, [])
        self.assertNotIn("publication_intent", job["manifest"])
        self.assertNotIn("display_timing_adjustments", job["manifest"])
        self.assertEqual(output.read_bytes(), b"original subtitles")
        self.assertEqual(list(self.path.glob("*.bak.*")), [])
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(fake.calls, [])

    def test_interrupted_publication_fixes_policy_and_adjustments_in_one_commit(self):
        self.save_result(overlapping_raw())
        fake = FakeService(overlapping_raw())
        with mock.patch.object(engine_module, "write_srt", side_effect=OSError()):
            self.assertEqual(self.run_fake(fake), "failed")
        manifest = self.store.job(self.job_id)["manifest"]
        intent = copy.deepcopy(manifest["publication_intent"])
        self.assertEqual(manifest["display_timing_adjustments"][0]["adjustment_ms"], 40)
        cues, sources = self.store.cues(self.job_id), self.store.sources(self.job_id)
        self.config.settings.zh_line_width = 12
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.store.job(self.job_id)["manifest"]["publication_intent"], intent)
        self.assertEqual(self.store.job(self.job_id)['manifest']['display_timing_version'], 3)
        self.assertEqual(self.store.cues(self.job_id), cues)
        self.assertEqual(self.store.sources(self.job_id), sources)
        self.assertEqual(summary(self.store, self.job_id)["display_timing_adjustments"], manifest["display_timing_adjustments"])

    def test_pending_adjustment_mismatch_is_not_silently_replaced(self):
        self.save_result(overlapping_raw())
        fake = FakeService(overlapping_raw())
        with mock.patch.object(engine_module, "write_srt", side_effect=OSError()):
            self.assertEqual(self.run_fake(fake), "failed")
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["display_timing_adjustments"][0]["adjustment_ms"] = 41
        self.store.update(self.job_id, manifest=manifest)
        with mock.patch.object(engine_module, "write_srt", side_effect=AssertionError("no publish")):
            self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "cache_mismatch")


def cue(number, begin, end):
    sentence_id = f'0000:{number:08d}'
    return dict(cue_id=sentence_id + ':0000', sentence_id=sentence_id, begin_ms=begin, end_ms=end,
                text=f'Synthetic sentence {number}.', translated='已保存的合成译文',
                language='en', source_lang='en', decision='translate', mt_origin='fallback')




class DisplayTimingV3Tests(unittest.TestCase):
    def test_delayed_start_respects_absolute_ratio_and_remaining_duration_limits(self):
        for begin, end, error in [(300, 5300, None), (299, 5300, '500ms'), (300, 5299, '10%'),
                                  (700, 1800, None), (700, 1799, '1000ms')]:
            cues = [cue(1, 0, 800), cue(2, begin, end)]
            with self.subTest(begin=begin, end=end):
                if error:
                    with self.assertRaises(ToolError) as caught:
                        plan_display_timing(cues)
                    self.assertEqual(caught.exception.code, 'timeline')
                    self.assertIn(error, str(caught.exception))
                else:
                    intervals, adjustments = plan_display_timing(cues)
                    self.assertEqual([(i['begin_ms'], i['end_ms']) for i in intervals], [(0, 800), (800, end)])
                    self.assertEqual(adjustments[0]['adjustment_ms'], 800 - begin)

    def test_trimming_the_first_end_preserves_a_naturally_short_second_cue(self):
        intervals, adjustments = plan_display_timing([cue(1, 0, 4000), cue(2, 3800, 4500)])
        self.assertEqual([(i['begin_ms'], i['end_ms']) for i in intervals], [(0, 3800), (3800, 4500)])
        self.assertEqual(adjustments[0]['reason'], 'bounded_sentence_overlap')

    def test_chained_trims_share_each_cues_budget(self):
        cues = [cue(1, 0, 800), cue(2, 500, 4500), cue(3, 4300, 8300), cue(4, 8200, 12200)]
        before = copy.deepcopy(cues)
        intervals, adjustments = plan_display_timing(cues)
        self.assertEqual([(i['begin_ms'], i['end_ms']) for i in intervals],
                         [(0, 800), (800, 4500), (4500, 8200), (8200, 12200)])
        self.assertEqual([(a['cue_id'], a['adjustment_ms']) for a in adjustments],
                         [(cues[1]['cue_id'], 300), (cues[2]['cue_id'], 200), (cues[2]['cue_id'], 100)])
        self.assertEqual(adjustments[-1]['old_begin_ms'], adjustments[-2]['new_begin_ms'])
        self.assertEqual(plan_display_timing(cues), (intervals, adjustments))
        self.assertEqual(cues, before)
