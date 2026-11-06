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
from subtitle_tool.policy import load_policy
from subtitle_tool.subtitles import render_srt, make_fragment, normalize, plan_display_timing, validate_display_intervals

from tests.fixtures import FakeService, SavedResultFixture, overlapping_raw, srt_intervals


def original_cues(raw):
    sources, _ = normalize(raw, 0, "0000", preserve_sentences=True)
    return sources, [{**make_fragment(s), "decision": "original"} for s in sources]


class DisplayTimingPipelineTests(SavedResultFixture):
    def test_new_task_trims_only_the_display_end_of_a_small_overlap(self):
        raw = overlapping_raw()
        self.save_result(raw)
        self.assertEqual(self.run_fake(FakeService(raw)), "completed")
        content = self.source.with_suffix(".srt").read_bytes()
        self.assertEqual(srt_intervals(content), [(1000, 4960), (4960, 8320)])
        manifest = self.store.job(self.job_id)["manifest"]
        self.assertEqual(manifest["display_timing_version"], 2)
        self.assertEqual(manifest["display_timing_adjustments"][0]["adjustment_ms"], 40)
        self.assertEqual(manifest["expected_srt_sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(manifest["publication_intent"]["expected_srt_sha256"], manifest["expected_srt_sha256"])
        self.assertEqual([(c["begin_ms"], c["end_ms"]) for c in self.store.cues(self.job_id)],
                         [(1000, 5000), (4960, 8320)])
        Engine(self.config, self.store).validate_saved_subtitles(self.job_id, manifest)


    def test_unknown_version_stops_before_cache_requests_and_publication(self):
        self.save_result(overlapping_raw())
        initial = self.store.job(self.job_id)["manifest"]
        for version in [-1, 3, True, "1", None]:
            with self.subTest(version=version):
                manifest = {**initial, "display_timing_version": version}
                self.store.update(self.job_id, "queued", manifest)
                fake = FakeService(overlapping_raw())
                self.assertEqual(self.run_fake(fake), "failed")
                self.assertEqual(self.store.job(self.job_id)["error_code"], "cache_version")
                self.assertEqual(fake.calls, [])
                self.assertFalse(self.source.with_suffix(".srt").exists())

    def test_unrepairable_overlap_preserves_complete_translations_and_output(self):
        raw = overlapping_raw(overlap=501)
        self.save_result(raw)
        output = self.source.with_suffix(".srt")
        output.write_bytes(b"original subtitles")
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["overwrite"] = True
        manifest["display_timing_version"] = 2
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(raw)
        self.assertEqual(self.run_fake(fake), "failed")
        job = self.store.job(self.job_id)
        self.assertEqual(job["error_code"], "timeline")
        self.assertFalse(job["manifest"].get("layout_complete"))
        self.assertFalse(any(c["translated"] for c in self.store.cues(self.job_id)))
        self.assertNotIn("publication_intent", job["manifest"])
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
        with self.assertRaises(ToolError) as caught:
            plan_display_timing(cues, version=1)
        self.assertEqual(caught.exception.code, 'cache_version')
        intervals, adjustments = plan_display_timing(cues, version=2)
        self.assertEqual([(r['begin_ms'], r['end_ms']) for r in intervals],
                         [(1549460, 1554680), (1554680, 1558000)])
        self.assertEqual(adjustments, [dict(cue_id=cues[0]['cue_id'], old_begin_ms=1549460,
            old_end_ms=1554980, new_begin_ms=1549460, new_end_ms=1554680, adjustment_ms=300,
            reason='bounded_sentence_overlap')])
        self.assertEqual(cues, saved)
        self.assertEqual(plan_display_timing(cues, version=2), (intervals, adjustments))
        content, saved_adjustments = Engine.render_subtitles(cues, Settings(), {'display_timing_version': 2})
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
        for other in variants:
            with self.subTest(other=other), self.assertRaises(ToolError) as caught:
                plan_display_timing([first, other], version=2)
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


    def test_unknown_display_versions_are_rejected_by_policy_and_renderer(self):
        for version in [-1, 3, True, '2', None]:
            with self.subTest(version=version):
                for operation in [lambda: load_policy({'display_timing_version': version}),
                                  lambda: Engine.render_subtitles([], Settings(), {'display_timing_version': version})]:
                    with self.assertRaises(ToolError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, 'cache_version')


class DisplayTimingV2PipelineTests(SavedResultFixture):
    def completed_with_invalid_timing(self, overlap=300):
        self.source.with_suffix('.srt').unlink(missing_ok=True)
        initial = self.store.job(self.job_id)['manifest']
        for field in ['layout_planned', 'layout_complete', 'layout_round',
                      'publication_intent', 'expected_srt_sha256', 'display_timing_adjustments', 'retime']:
            initial.pop(field, None)
        self.store.update(self.job_id, 'queued', initial)
        raw = overlapping_raw(overlap=overlap)
        self.save_result(raw)
        manifest = self.store.job(self.job_id)['manifest']
        manifest['display_timing_version'] = 2
        self.store.update(self.job_id, manifest=manifest)
        self.fake = FakeService(raw)
        with mock.patch.object(Engine, 'preflight_display_timing'), mock.patch.object(Engine, 'render_subtitles',
                side_effect=lambda cues, settings, manifest: (render_srt(cues, settings), [])):
            self.assertEqual(self.run_fake(self.fake), 'completed')
        self.fake.calls.clear()
        return self.store.job(self.job_id)['manifest']

    def test_impossible_new_layout_stops_before_subtitle_translation(self):
        manifest = self.store.job(self.job_id)['manifest']
        manifest['subtitle_settings']['other_line_width'] = 24
        self.store.update(self.job_id, manifest=manifest)
        raw = overlapping_raw(overlap=501)
        self.save_result(raw)
        fake = FakeService(raw)
        self.assertEqual(self.run_fake(fake), 'failed')
        job = self.store.job(self.job_id)
        self.assertEqual(job['error_code'], 'timeline')
        self.assertEqual(self.store.counts(self.job_id).get('mt', 0), 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_subtitle_request', 0), 0)
        self.assertNotIn('publication_intent', job['manifest'])
        self.assertNotIn('display_timing_adjustments', job['manifest'])

    def test_preflight_keeps_existing_successes_and_does_not_request_missing_translation(self):
        manifest = self.completed_with_invalid_timing(overlap=501)
        manifest['layout_complete'] = False
        self.source.with_suffix('.srt').unlink()
        manifest['display_timing_version'] = 2
        with self.store.db:
            self.store.db.execute("UPDATE cues SET translated='', metadata=json_remove(metadata,'$.mt_origin','$.mt_stage_key') WHERE job_id=? AND cue_id=?",
                                  (self.job_id, self.store.cues(self.job_id)[-1]['cue_id']))
        for field in ['publication_intent', 'expected_srt_sha256']:
            manifest.pop(field, None)
        self.store.update(self.job_id, 'failed', manifest)
        before = self.store.cues(self.job_id)
        self.assertEqual(self.run_fake(self.fake), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'timeline')
        self.assertEqual(self.fake.calls, [])
        self.assertEqual((self.store.cues(self.job_id)), before)
