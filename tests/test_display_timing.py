from __future__ import annotations

import copy
import hashlib
import unittest
from unittest import mock

import subtitle_tool.engine as engine_module
from subtitle_tool.common import ToolError
from subtitle_tool.config import Settings
from subtitle_tool.engine import Engine
from subtitle_tool.main import summary
from subtitle_tool.subtitles import render_srt, plan_display_timing, validate_display_intervals

from tests.fixtures import FakeService, SavedResultFixture, overlapping_raw, srt_intervals, seed_job_state, timed_raw


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

    def test_reflow_upgrades_a_failed_v2_job_using_saved_asr(self):
        raw = short_first_raw()
        sources = self.save_result(raw)
        raw_path = self.cache / 'raw-0000.json'
        raw_bytes = raw_path.read_bytes()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['display_timing_version'] = 2
        self.store.update(self.job_id, manifest=manifest)
        engine = Engine(self.config, self.store)
        fake = FakeService(raw)
        engine.resume(self.job_id)
        self.assertEqual(self.run_fake(fake), 'failed')
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
        manifest = self.store.job(self.job_id)['manifest']
        manifest['display_timing_version'] = 2
        self.store.update(self.job_id, manifest=manifest)
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
        self.assertEqual(self.store.job(self.job_id)['manifest']['display_timing_version'], 2)
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


class DisplayTimingV2Tests(unittest.TestCase):
    def test_300ms_reproduction_keeps_evidence_and_only_trims_the_first_end(self):
        cues = [cue(298, 1549460, 1554980), cue(299, 1554680, 1558000)]
        saved = copy.deepcopy(cues)
        intervals, adjustments = plan_display_timing(cues, version=2)
        self.assertEqual([(r['begin_ms'], r['end_ms']) for r in intervals],
                         [(1549460, 1554680), (1554680, 1558000)])
        self.assertEqual(adjustments, [dict(cue_id=cues[0]['cue_id'], old_begin_ms=1549460,
            old_end_ms=1554980, new_begin_ms=1549460, new_end_ms=1554680, adjustment_ms=300,
            reason='bounded_sentence_overlap')])
        self.assertEqual(cues, saved)
        self.assertEqual(plan_display_timing(cues, version=2), (intervals, adjustments))
        content, saved_adjustments = Engine.render_subtitles(cues, Settings(), version=2)
        self.assertEqual(saved_adjustments, adjustments)
        self.assertEqual(srt_intervals(content), [(1549460, 1554680), (1554680, 1558000)])

    def test_absolute_overlap_boundaries(self):
        for overlap in [100, 101, 300, 500, 501]:
            cues = [cue(1, 0, 5000), cue(2, 5000 - overlap, 9000)]
            with self.subTest(overlap=overlap):
                if overlap > 500:
                    with self.assertRaises(ToolError) as caught:
                        plan_display_timing(cues, version=2)
                    self.assertIn('500ms', str(caught.exception))
                else:
                    intervals, adjustments = plan_display_timing(cues, version=2)
                    self.assertEqual(intervals[0]['end_ms'], 5000 - overlap)
                    self.assertEqual(adjustments[0]['adjustment_ms'], overlap)

    def test_ten_percent_boundary_uses_the_original_duration(self):
        for duration, accepted in [(3000, True), (2999, False), (5000, True), (4999, False)]:
            overlap = 300 if duration < 4000 else 500
            cues = [cue(1, 0, duration), cue(2, duration - overlap, 9000)]
            with self.subTest(duration=duration):
                if accepted:
                    intervals, _ = plan_display_timing(cues, version=2)
                    self.assertEqual(intervals[0]['end_ms'], duration - overlap)
                else:
                    with self.assertRaises(ToolError) as caught:
                        plan_display_timing(cues, version=2)
                    self.assertIn('10%', str(caught.exception))
                    self.assertNotIn(cues[0]['text'], str(caught.exception))

    def test_both_minimum_duration_boundaries_and_unconflicted_short_cues(self):
        for remaining, following, accepted in [(1000, 1000, True), (999, 1000, False), (1000, 999, False)]:
            cues = [cue(1, 0, remaining + 100), cue(2, remaining, remaining + following)]
            with self.subTest(remaining=remaining, following=following):
                if accepted:
                    plan_display_timing(cues, version=2)
                else:
                    with self.assertRaises(ToolError) as caught:
                        plan_display_timing(cues, version=2)
                    self.assertIn('1000ms', str(caught.exception))
        intervals, adjustments = plan_display_timing([cue(1, 0, 200), cue(2, 300, 500)], version=2)
        self.assertEqual([i['end_ms'] for i in intervals], [200, 500])
        self.assertEqual(adjustments, [])

    def test_containment_same_start_cross_segment_and_source_order_are_rejected(self):
        first, second = cue(1, 0, 5000), cue(2, 4700, 9000)
        variants = [{**second, 'begin_ms': 0}, {**second, 'end_ms': 4900},
                    {**second, 'end_ms': 5000}, {**second, 'sentence_id': '0001:00000002'},
                    {**second, 'sentence_id': first['sentence_id']}, {**second, 'sentence_id': '0000:00000000'}]
        for version in (2, 3):
            for other in variants:
                with self.subTest(version=version, other=other), self.assertRaises(ToolError) as caught:
                    plan_display_timing([first, other], version=version)
                self.assertIn('区间结构冲突', str(caught.exception))
                self.assertIn(first['sentence_id'], str(caught.exception))
                self.assertIn('[0, 5000)', str(caught.exception))
                self.assertNotIn(first['text'], str(caught.exception))

    def test_chained_conflicts_are_deterministic_and_idempotent(self):
        cues = [cue(1, 0, 5000), cue(2, 4700, 9700), cue(3, 9400, 14400)]
        before = copy.deepcopy(cues)
        intervals, adjustments = plan_display_timing(cues, version=2)
        validate_display_intervals(cues, intervals)
        self.assertEqual([i['end_ms'] for i in intervals], [4700, 9400, 14400])
        self.assertEqual([a['adjustment_ms'] for a in adjustments], [300, 300])
        self.assertEqual(plan_display_timing(cues, version=2), (intervals, adjustments))
        displayed = [{**c, **i} for c, i in zip(cues, intervals)]
        self.assertEqual(plan_display_timing(displayed, version=2), (intervals, []))
        self.assertEqual(cues, before)


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


class DisplayTimingV2PipelineTests(SavedResultFixture):
    def completed_with_invalid_timing(self, raw):
        self.source.with_suffix('.srt').unlink(missing_ok=True)
        initial = self.store.job(self.job_id)['manifest']
        for field in ['layout_planned', 'layout_complete', 'layout_round',
                      'publication_intent', 'expected_srt_sha256', 'display_timing_adjustments', 'retime']:
            initial.pop(field, None)
        seed_job_state(self.store, self.job_id, 'queued', initial)
        self.save_result(raw)
        manifest = self.store.job(self.job_id)['manifest']
        manifest['display_timing_version'] = 2
        self.store.update(self.job_id, manifest=manifest)
        self.fake = FakeService(raw)
        with mock.patch('subtitle_tool.mt_pipeline.plan_display_timing'), mock.patch.object(Engine, 'render_subtitles',
                side_effect=lambda cues, settings, **kwargs: (render_srt(cues, settings), [])):
            self.assertEqual(self.run_fake(self.fake), 'completed')
        self.fake.calls.clear()
        return self.store.job(self.job_id)['manifest']

    def test_retime_upgrades_a_finished_v2_layout_without_retranslating(self):
        manifest = self.completed_with_invalid_timing(short_first_raw())
        seed_job_state(self.store, self.job_id, 'failed', manifest, code='timeline')
        self.assertEqual(summary(self.store, self.job_id)['lifecycle']['next_actions'][0]['id'], 'retime')
        saved = self.store.cues(self.job_id)
        output = self.source.with_suffix('.srt')
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(self.run_fake(self.fake), 'completed')
        self.assertEqual(srt_intervals(output.read_bytes()), [(1000, 1800), (1800, 6220)])
        self.assertEqual(self.store.cues(self.job_id), saved)
        self.assertEqual(self.fake.calls, [])

    def test_preflight_keeps_existing_successes_and_does_not_request_missing_translation(self):
        manifest = self.completed_with_invalid_timing(overlapping_raw(overlap=501))
        manifest['layout_complete'] = False
        self.source.with_suffix('.srt').unlink()
        manifest['display_timing_version'] = 2
        with self.store.db:
            self.store.db.execute("UPDATE cues SET translated='', metadata=json_remove(metadata,'$.mt_origin','$.mt_stage_key') WHERE job_id=? AND cue_id=?",
                                  (self.job_id, self.store.cues(self.job_id)[-1]['cue_id']))
        for field in ['publication_intent', 'expected_srt_sha256']:
            manifest.pop(field, None)
        seed_job_state(self.store, self.job_id, 'failed', manifest)
        before = self.store.cues(self.job_id)
        self.assertEqual(self.run_fake(self.fake), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'timeline')
        self.assertEqual(self.fake.calls, [])
        self.assertEqual((self.store.cues(self.job_id)), before)
