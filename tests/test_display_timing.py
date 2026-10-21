from __future__ import annotations

import copy
import hashlib
import unittest
from pathlib import Path
from unittest import mock

import subtitle_tool.engine as engine_module
from subtitle_tool.common import ToolError
from subtitle_tool.config import Settings
from subtitle_tool.engine import Engine
from subtitle_tool.main import summary
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
        intervals, adjustments = plan_display_timing(cues)
        self.assertEqual([(p["begin_ms"], p["end_ms"]) for p in intervals],
                         [(1000, 4960), (4960, 8320)])
        self.assertEqual(adjustments, [{"cue_id": cues[0]["cue_id"], "old_begin_ms": 1000,
            "old_end_ms": 5000, "new_begin_ms": 1000, "new_end_ms": 4960,
            "adjustment_ms": 40, "reason": "small_sentence_overlap"}])
        self.assertEqual((intervals, adjustments), plan_display_timing(cues))
        self.assertEqual(cues, saved_cues)
        self.assertEqual((raw, sources), before)
        content = render_srt(cues, Settings(), display_intervals=intervals)
        self.assertEqual(srt_intervals(content), [(1000, 4960), (4960, 8320)])
        self.assertEqual(content.count("已保存的合成译文".encode()), 2)

    def test_gap_touching_and_1_40_100ms_overlaps(self):
        for overlap in [-100, 0, 1, 40, 100]:
            with self.subTest(overlap=overlap):
                _, cues = original_cues(overlapping_raw(overlap=overlap))
                intervals, adjustments = plan_display_timing(cues)
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
                plan_display_timing(cues)
            self.assertEqual(caught.exception.code, "timeline")
            self.assertIn(base[0]["cue_id"], str(caught.exception))
            self.assertIn(base[1]["cue_id"], str(caught.exception))
            self.assertNotIn(base[0]["text"], str(caught.exception))

    def test_short_unconflicted_cues_and_exact_minimum_after_trimming(self):
        _, cues = original_cues(overlapping_raw())
        cues[0].update(begin_ms=0, end_ms=1040)
        cues[1].update(begin_ms=1000, end_ms=2000)
        intervals, _ = plan_display_timing(cues)
        self.assertEqual(intervals[0]["end_ms"], 1000)
        cues[0]["end_ms"] = 300
        intervals, adjustments = plan_display_timing(cues)
        self.assertEqual(intervals[0]["end_ms"], 300)
        self.assertEqual(adjustments, [])

    def test_chain_trims_each_previous_end_without_drift(self):
        raw = overlapping_raw()
        raw["transcripts"][0]["sentences"].extend(
            timed_raw(["Third", "sentence"], word_ms=1000, start=8280)["transcripts"][0]["sentences"])
        _, cues = original_cues(raw)
        intervals, adjustments = plan_display_timing(cues)
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
        intervals, adjustments = plan_display_timing(cues)
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
        valid, _ = plan_display_timing(cues)
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
        self.assertEqual(plan_display_timing([]), ([], []))
        _, cues = original_cues(overlapping_raw())
        for begin, end in [(-1, 10), (0, 0), (1, 0), (False, 10), (0, 1.1)]:
            with self.subTest(begin=begin, end=end), self.assertRaises(ToolError) as caught:
                plan_display_timing([{**cues[0], "begin_ms": begin, "end_ms": end}])
            self.assertEqual(caught.exception.code, "timeline")


class DisplayTimingPipelineTests(SavedResultFixture):
    def test_new_task_trims_only_the_display_end_of_a_small_overlap(self):
        raw = overlapping_raw()
        self.save_result(raw)
        self.assertEqual(self.run_fake(FakeService(raw)), "completed")
        content = self.source.with_suffix(".srt").read_bytes()
        self.assertEqual(srt_intervals(content), [(1000, 4960), (4960, 8320)])
        manifest = self.store.job(self.job_id)["manifest"]
        self.assertEqual(manifest["display_timing_version"], 1)
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
        for version in [-1, 2, True, "1", None]:
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
        self.assertEqual(self.store.job(self.job_id)["manifest"]["display_timing_version"], 1)
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(fake.calls, [])
        saved = self.store.job(self.job_id)["manifest"]
        self.assertEqual(Path(saved["backup"]).read_bytes(), before)
