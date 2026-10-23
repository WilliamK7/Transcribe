from __future__ import annotations

import copy
import hashlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import subtitle_tool.engine as engine_module
from subtitle_tool.common import ToolError, atomic_bytes
from subtitle_tool.config import Settings
from subtitle_tool.engine import Engine
from subtitle_tool.main import hint, main, summary
from subtitle_tool.mt_backend import MTBackend, OPENROUTER_BASE_URL, key_fingerprint
from subtitle_tool.policy import load_policy
from subtitle_tool.subtitles import (make_fragment, normalize, plan_display_timing, plan_fragments,
                                     render_srt, validate_display_intervals, validate_fragment)

from tests.fixtures import FakeService, SavedResultFixture, overlapping_raw, srt_intervals, timed_raw


def original_cues(raw):
    sources, _ = normalize(raw, 0, "0000", preserve_sentences=True)
    return sources, [{**make_fragment(s), "decision": "original"} for s in sources]


class DisplayTimingTests(unittest.TestCase):
    def test_display_timing_preserves_evidence_and_is_idempotent(self):
        raw = overlapping_raw()
        sources, cues = original_cues(raw)
        before = copy.deepcopy((raw, sources))
        for cue in cues:
            cue.update(decision="translate", translated="已保存的合成译文")
        saved_cues = copy.deepcopy(cues)
        intervals, adjustments = plan_display_timing(cues, version=1)
        self.assertEqual([(p["begin_ms"], p["end_ms"]) for p in intervals],
                         [(1000, 4960), (4960, 8320)])
        self.assertEqual(adjustments, [{"cue_id": cues[0]["cue_id"], "old_begin_ms": 1000,
            "old_end_ms": 5000, "new_begin_ms": 1000, "new_end_ms": 4960,
            "adjustment_ms": 40, "reason": "small_sentence_overlap"}])
        self.assertEqual((intervals, adjustments), plan_display_timing(cues, version=1))
        self.assertEqual(cues, saved_cues)
        self.assertEqual((raw, sources), before)
        content = render_srt(cues, Settings(), display_intervals=intervals)
        self.assertEqual(srt_intervals(content), [(1000, 4960), (4960, 8320)])
        self.assertEqual(content.count("已保存的合成译文".encode()), 2)

    def test_gap_touching_and_1_40_100ms_overlaps(self):
        for overlap in [-100, 0, 1, 40, 100]:
            with self.subTest(overlap=overlap):
                _, cues = original_cues(overlapping_raw(overlap=overlap))
                intervals, adjustments = plan_display_timing(cues, version=1)
                self.assertEqual(intervals[0]["end_ms"], min(5000, 5000 - overlap))
                self.assertEqual(intervals[1]["begin_ms"], 5000 - overlap)
                self.assertEqual(len(adjustments), int(overlap > 0))
                self.assertLessEqual(intervals[0]["end_ms"], intervals[1]["begin_ms"])

    def test_unsupported_conflicts_fail_with_only_ids_and_ranges(self):
        _, base = original_cues(overlapping_raw())
        cases = {
            "101ms": [{**base[0], "end_ms": 5061}, base[1]],
            "short_previous": [{**base[0], "begin_ms": 4000}, base[1]],
            "short_following": [base[0], {**base[1], "end_ms": 5500}],
            "same_start": [base[0], {**base[1], "begin_ms": 1000}],
            "contained": [base[0], {**base[1], "end_ms": 4980}],
            "same_end": [base[0], {**base[1], "end_ms": 5000}],
            "cross_segment": [base[0], {**base[1], "sentence_id": "0001:00000000"}],
            "same_source": [base[0], {**base[1], "sentence_id": base[0]["sentence_id"]}],
            "source_order": [{**base[0], "sentence_id": "0000:00000002"}, base[1]],
            "reverse": list(reversed(base)),
        }
        for name, cues in cases.items():
            with self.subTest(name=name), self.assertRaises(ToolError) as caught:
                plan_display_timing(cues, version=1)
            self.assertEqual(caught.exception.code, "timeline")
            self.assertIn(base[0]["cue_id"], str(caught.exception))
            self.assertIn(base[1]["cue_id"], str(caught.exception))
            self.assertNotIn(base[0]["text"], str(caught.exception))

    def test_short_unconflicted_cues_and_exact_minimum_after_trimming(self):
        _, cues = original_cues(overlapping_raw())
        cues[0].update(begin_ms=0, end_ms=1040)
        cues[1].update(begin_ms=1000, end_ms=2000)
        intervals, _ = plan_display_timing(cues, version=1)
        self.assertEqual(intervals[0]["end_ms"], 1000)
        cues[0]["end_ms"] = 300
        intervals, adjustments = plan_display_timing(cues, version=1)
        self.assertEqual(intervals[0]["end_ms"], 300)
        self.assertEqual(adjustments, [])

    def test_chain_trims_each_previous_end_without_drift(self):
        raw = overlapping_raw()
        raw["transcripts"][0]["sentences"].extend(
            timed_raw(["Third", "sentence"], word_ms=1000, start=8280)["transcripts"][0]["sentences"])
        _, cues = original_cues(raw)
        intervals, adjustments = plan_display_timing(cues, version=1)
        self.assertEqual([(p["begin_ms"], p["end_ms"]) for p in intervals],
                         [(1000, 4960), (4960, 8280), (8280, 10280)])
        self.assertEqual([a["adjustment_ms"] for a in adjustments], [40, 40])

    def test_final_fragments_are_planned_after_splitting(self):
        raw = timed_raw([f"word{i}" for i in range(20)], word_ms=500, start=1000)
        raw["transcripts"][0]["sentences"].extend(
            timed_raw(["Last", "sentence"], word_ms=1000, start=10960)["transcripts"][0]["sentences"])
        sources, _ = normalize(raw, 0, "0000", preserve_sentences=True)
        cues = [c for source in sources for c in plan_fragments(source, Settings(other_line_width=24))]
        self.assertGreater(len(cues), 2)
        original = copy.deepcopy(cues)
        intervals, adjustments = plan_display_timing(cues, version=1)
        self.assertEqual(cues, original)
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(adjustments[0]["cue_id"], cues[-2]["cue_id"])
        self.assertEqual(intervals[-2]["end_ms"], 10960)
        for source in sources:
            for cue in cues:
                if source["sentence_id"] == cue["sentence_id"]:
                    validate_fragment(source, cue)

    def test_all_final_intervals_are_checked_including_nested_conflicts(self):
        _, cues = original_cues(overlapping_raw())
        valid, _ = plan_display_timing(cues, version=1)
        variants = [valid[:-1], [valid[1], valid[0]],
                    [{**valid[0], "begin_ms": -1}, valid[1]],
                    [{**valid[0], "end_ms": 1000}, valid[1]],
                    [{**valid[0], "end_ms": True}, valid[1]],
                    [{**valid[0], "end_ms": 10000}, valid[1]],
                    [valid[0], {**valid[1], "cue_id": "wrong"}]]
        for intervals in variants:
            with self.subTest(intervals=intervals), self.assertRaises(ToolError) as caught:
                render_srt(cues, Settings(), display_intervals=intervals)
            self.assertEqual(caught.exception.code, "timeline")
        third = {**cues[1], "cue_id": "third", "begin_ms": 9000, "end_ms": 10000}
        nested = [{**valid[0], "end_ms": 12000}, valid[1],
                  {k: third[k] for k in ["cue_id", "begin_ms", "end_ms"]}]
        with self.assertRaises(ToolError):
            validate_display_intervals([*cues, third], nested)

    def test_empty_and_invalid_times(self):
        self.assertEqual(plan_display_timing([], version=1), ([], []))
        _, cues = original_cues(overlapping_raw())
        for begin, end in [(-1, 10), (0, 0), (1, 0), (False, 10), (0, 1.1)]:
            with self.subTest(begin=begin, end=end), self.assertRaises(ToolError) as caught:
                plan_display_timing([{**cues[0], "begin_ms": begin, "end_ms": end}], version=1)
            self.assertEqual(caught.exception.code, "timeline")


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
        Engine(self.config, self.store).validate_saved_subtitles(self.job_id, manifest, self.cache)

    def test_missing_and_zero_version_keep_legacy_bytes_and_shape(self):
        sources = self.save_result(overlapping_raw())
        initial = self.store.job(self.job_id)["manifest"]
        for version in [None, 0]:
            with self.subTest(version=version):
                self.source.with_suffix(".srt").unlink(missing_ok=True)
                manifest = copy.deepcopy(initial)
                manifest.pop("display_timing_version")
                if version is not None:
                    manifest["display_timing_version"] = version
                self.store.initialize_subtitles(self.job_id, sources, [make_fragment(s) for s in sources], manifest)
                self.store.update(self.job_id, "queued", manifest)
                self.assertEqual(self.run_fake(FakeService(overlapping_raw())), "completed")
                saved = self.store.job(self.job_id)["manifest"]
                cues = self.store.cues(self.job_id)
                expected = render_srt(cues, load_policy(saved).settings(self.config.settings))
                self.assertEqual(self.source.with_suffix(".srt").read_bytes(), expected)
                self.assertEqual(srt_intervals(expected), [(1000, 5000), (4960, 8320)])
                self.assertEqual("display_timing_version" in saved, version is not None)
                self.assertNotIn("display_timing_adjustments", saved)

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
        raw = overlapping_raw(overlap=101)
        self.save_result(raw)
        output = self.source.with_suffix(".srt")
        output.write_bytes(b"original subtitles")
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["overwrite"] = True
        manifest["display_timing_version"] = 1
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(raw)
        self.assertEqual(self.run_fake(fake), "failed")
        job = self.store.job(self.job_id)
        self.assertEqual(job["error_code"], "timeline")
        self.assertTrue(job["manifest"]["layout_complete"])
        self.assertTrue(all(c["translated"] for c in self.store.cues(self.job_id)))
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

    def test_reflow_explicitly_upgrades_without_new_asr_or_mt(self):
        self.save_result(overlapping_raw())
        manifest = self.store.job(self.job_id)["manifest"]
        manifest.pop("display_timing_version")
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(overlapping_raw())
        self.assertEqual(self.run_fake(fake), "completed")
        before = self.source.with_suffix(".srt").read_bytes()
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.store.job(self.job_id)["manifest"]["display_timing_version"], 2)
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(fake.calls, [])
        saved = self.store.job(self.job_id)["manifest"]
        self.assertEqual(Path(saved["backup"]).read_bytes(), before)


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
        self.assertEqual(caught.exception.code, 'timeline')
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

    def test_zero_and_one_keep_their_rendered_bytes(self):
        cues = [cue(1, 1000, 5000), cue(2, 4960, 8320)]
        original = render_srt(cues, Settings())
        for version in [0, 1]:
            content, adjustments = Engine.render_subtitles(cues, Settings(), {'display_timing_version': version})
            self.assertEqual(content, original if version == 0 else original.replace(b'00:00:05,000', b'00:00:04,960', 1))
            self.assertEqual(adjustments[0]['reason'] if adjustments else None,
                             'small_sentence_overlap' if version else None)
        self.assertEqual(Engine.render_subtitles(cues, Settings(), {})[0], original)

    def test_unknown_display_versions_are_rejected_by_policy_and_renderer(self):
        for version in [-1, 3, True, '2', None]:
            with self.subTest(version=version):
                for operation in [lambda: load_policy({'display_timing_version': version}),
                                  lambda: Engine.render_subtitles([], Settings(), {'display_timing_version': version})]:
                    with self.assertRaises(ToolError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, 'cache_version')


class DisplayTimingV2PipelineTests(SavedResultFixture):
    def legacy_complete(self, overlap=300):
        self.source.with_suffix('.srt').unlink(missing_ok=True)
        initial = self.store.job(self.job_id)['manifest']
        for field in ['references_planned', 'layout_planned', 'layout_complete', 'layout_round',
                      'publication_intent', 'expected_srt_sha256', 'display_timing_adjustments', 'retime']:
            initial.pop(field, None)
        self.store.update(self.job_id, 'queued', initial)
        raw = overlapping_raw(overlap=overlap)
        self.save_result(raw)
        manifest = self.store.job(self.job_id)['manifest']
        manifest['display_timing_version'] = 0
        self.store.update(self.job_id, manifest=manifest)
        self.fake = FakeService(raw)
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
        self.assertTrue(all(r['translated'] for r in self.store.references(self.job_id)))
        self.assertGreater(self.store.counts(self.job_id).get('mt_reference_request', 0), 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_subtitle_request', 0), 0)
        self.assertNotIn('publication_intent', job['manifest'])
        self.assertNotIn('display_timing_adjustments', job['manifest'])

    def test_preflight_keeps_existing_successes_and_does_not_request_missing_translation(self):
        manifest = self.legacy_complete(overlap=501)
        self.source.with_suffix('.srt').unlink()
        manifest['display_timing_version'] = 2
        self.store.translation_update(self.job_id, [self.store.cues(self.job_id)[-1]['cue_id']], translated='')
        for field in ['publication_intent', 'expected_srt_sha256']:
            manifest.pop(field, None)
        self.store.update(self.job_id, 'failed', manifest)
        before = self.store.cues(self.job_id), self.store.references(self.job_id)
        self.assertEqual(self.run_fake(self.fake), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'timeline')
        self.assertEqual(self.fake.calls, [])
        self.assertEqual((self.store.cues(self.job_id), self.store.references(self.job_id)), before)

    def test_prepared_v1_retime_resumes_with_original_adjustment_and_intent(self):
        self.legacy_complete(overlap=40)
        engine = Engine(self.config, self.store)
        with mock.patch.object(engine_module, 'DISPLAY_TIMING_VERSION', 1):
            engine.retime(self.job_id, overwrite=True)
        prepared = self.store.job(self.job_id)['manifest']
        self.assertEqual(prepared['display_timing_version'], 1)
        self.assertEqual(prepared['display_timing_adjustments'][0]['reason'], 'small_sentence_overlap')
        with mock.patch.object(engine_module, 'write_srt', side_effect=OSError()):
            self.assertEqual(self.run_fake(self.fake), 'failed')
        engine.resume(self.job_id)
        self.assertEqual(self.run_fake(self.fake), 'completed')
        saved = self.store.job(self.job_id)['manifest']
        self.assertEqual(saved['publication_intent'], prepared['publication_intent'])
        self.assertEqual(saved['display_timing_adjustments'], prepared['display_timing_adjustments'])
        self.assertEqual(self.fake.calls, [])

    def test_completed_old_timeline_failure_gets_retime_hint_only_when_v2_succeeds(self):
        for overlap, eligible in [(300, True), (501, False)]:
            with self.subTest(overlap=overlap):
                manifest = self.legacy_complete(overlap=overlap)
                manifest['display_timing_version'] = 1
                self.store.update(self.job_id, 'failed', manifest, error='安全时间冲突提示', code='timeline')
                with mock.patch('sys.stderr', new_callable=io.StringIO) as output:
                    hint(self.store, self.job_id)
                self.assertIn('--retime' if eligible else '--resume', output.getvalue())
                if eligible:
                    self.assertIn('译文已完成', output.getvalue())
                    self.assertNotIn('--resume', output.getvalue())

    def test_retime_preserves_mixed_origins_and_cli_explains_local_only_work(self):
        manifest = self.legacy_complete()
        atomic_bytes(self.config.data / 'mt-key.txt', b'offline-display-v2-key')
        manifest['mt_snapshot'] = MTBackend('openai_compatible', OPENROUTER_BASE_URL,
            'openrouter/free', key_fingerprint('offline-display-v2-key'), 2).snapshot()
        self.store.update(self.job_id, manifest=manifest)
        Engine(self.config, self.store).configure_fallback(self.job_id)
        last = self.store.cues(self.job_id)[-1]
        self.store.translation_update(self.job_id, [last['cue_id']], translated=last['translated'], origin='fallback')
        before = self.store.cues(self.job_id), self.store.references(self.job_id), self.store.sources(self.job_id)
        policy = self.store.job(self.job_id)['manifest']['mt_fallback_policy']
        with mock.patch('subtitle_tool.main.Config', return_value=self.config), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as stdout, \
                mock.patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                mock.patch('requests.request', side_effect=AssertionError('Local only')), \
                mock.patch('requests.post', side_effect=AssertionError('Local only')), \
                mock.patch('subtitle_tool.mt_runtime.ProcessTransport', side_effect=AssertionError('Local only')):
            self.assertEqual(main(['--retime', self.job_id, '--overwrite', '--non-interactive']), 0)
        self.assertIn('本地已保存', stderr.getvalue())
        self.assertNotIn('可能产生费用', stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())['display_timing_version'], 2)
        self.assertEqual((self.store.cues(self.job_id), self.store.references(self.job_id), self.store.sources(self.job_id)), before)
        self.assertEqual(self.store.job(self.job_id)['manifest']['mt_fallback_policy'], policy)
