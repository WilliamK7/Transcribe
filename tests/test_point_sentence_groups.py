from __future__ import annotations

import copy
import json
import re
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.policy import asr_contract
from subtitle_tool.subtitles import normalize, plan_display_timing
from tests.fixtures import FakeService, PipelineFixture, TemporaryTest, point_sentence, result


def boundary_points():
    raw = result(("Earlier sentence.", "en"), ("Following sentence.", "en"))
    sentences = raw["transcripts"][0]["sentences"]
    sentences[0].update(begin_time=0, end_time=2000)
    sentences[1].update(begin_time=2000, end_time=4000)
    sentences[1:1] = [point_sentence(2000, "A synthetic sentence one."),
                      point_sentence(2000, "Another synthetic point.")]
    return raw


def nearby_point(*, direction="next", distance=80):
    raw = result(("Earlier sentence.", "en"), ("Following sentence.", "en"))
    sentences = raw["transcripts"][0]["sentences"]
    sentences[0].update(begin_time=0, end_time=640 if direction == "next" else 2000 - distance)
    begin = 2000 + distance if direction == "next" else 3600
    sentences[1].update(begin_time=begin, end_time=begin + 1280)
    point = point_sentence(2000, "Future phrase.")
    point["words"] = [{"begin_time": 2000, "end_time": 2000, "text": "Future", "punctuation": ""},
                      {"begin_time": 2000, "end_time": 2000, "text": "phrase", "punctuation": "."}]
    sentences.insert(1, point)
    return raw


class PointSentenceGroupTests(TemporaryTest):
    def test_fourteen_character_point_merges_with_next_original_sentence_eighty_ms_away(self):
        raw = nearby_point()
        before = copy.deepcopy(raw)
        sources, warnings = normalize(raw, 1250, "s", duration_ms=5000, preserve_sentences=True)
        self.assertEqual(raw, before)
        self.assertEqual(len(sources), 2)
        source = sources[-1]
        self.assertEqual(source["text"], "Future phrase. Following sentence.")
        self.assertEqual((source["begin_ms"], source["end_ms"]), (3250, 4610))
        self.assertEqual(source["asr_sentence_indices"], [1, 2])
        self.assertEqual(source["original_timings"], [
            {"asr_sentence_index": 1, "begin_ms": 3250, "end_ms": 3250},
            {"asr_sentence_index": 2, "begin_ms": 3330, "end_ms": 4610}])
        self.assertEqual(source["words"], [])
        self.assertEqual(source["timing_adjustments"], [{
            "asr_sentence_index": 1, "anchor_ms": 2000, "original_begin_ms": 2000, "original_end_ms": 2000,
            "kind": "merged_asr_nearby_point", "direction": "next", "distance_ms": 80,
            "original_neighbor_begin_ms": 2080, "original_neighbor_end_ms": 3360}])
        self.assertTrue(warnings)
        self.assertEqual(plan_display_timing(sources, version=2)[1], [])
        with self.assertRaises(ToolError) as caught:
            normalize(raw, 1250, "s", duration_ms=5000, normalizer_version=3)
        self.assertEqual(caught.exception.code, "timeline")

    def test_nearby_original_neighbors_allow_four_hundred_ms_but_reject_four_hundred_and_one(self):
        for direction in ["previous", "next"]:
            for distance in [80, 400, 401]:
                with self.subTest(direction=direction, distance=distance):
                    raw = nearby_point(direction=direction, distance=distance)
                    before = copy.deepcopy(raw)
                    if distance == 401:
                        with self.assertRaises(ToolError) as caught:
                            normalize(raw, 0, "s", duration_ms=5000)
                        self.assertEqual(caught.exception.code, "timeline")
                    else:
                        sources, _ = normalize(raw, 0, "s", duration_ms=5000, preserve_sentences=True)
                        source = sources[0] if direction == "previous" else sources[-1]
                        self.assertEqual(source["timing_adjustments"][0]["distance_ms"], distance)
                        self.assertEqual(source["timing_adjustments"][0]["direction"], direction)
                        self.assertEqual(source["asr_sentence_indices"], [0, 1] if direction == "previous" else [1, 2])
                        self.assertEqual(source["words"], [])
                        self.assertEqual(plan_display_timing(sources, version=2)[1], [])
                    self.assertEqual(raw, before)

    def test_nearby_previous_neighbor_keeps_short_point_direction_priority(self):
        raw = nearby_point()
        raw["transcripts"][0]["sentences"][0]["end_time"] = 1600
        sources, _ = normalize(raw, 0, "s", duration_ms=5000, preserve_sentences=True)
        self.assertEqual(sources[0]["asr_sentence_indices"], [0, 1])
        self.assertEqual((sources[0]["begin_ms"], sources[0]["end_ms"]), (0, 2000))
        self.assertEqual(sources[0]["timing_adjustments"][0]["distance_ms"], 400)

    def test_nearby_mixed_group_requires_every_points_evidence_before_display_windows(self):
        for short_first in [True, False]:
            with self.subTest(short_first=short_first):
                raw = nearby_point()
                sentences = raw["transcripts"][0]["sentences"]
                short = point_sentence(2000)
                sentences.insert(1 if short_first else 2, short)
                sources, _ = normalize(raw, 0, "s", duration_ms=5000, preserve_sentences=True)
                self.assertEqual(sources[-1]["asr_sentence_indices"], [1, 2, 3])
                self.assertEqual(sources[-1]["text"], " ".join(s["text"] for s in sentences[1:]))
                self.assertEqual(sources[-1]["words"], [])
                short["words"][0]["text"] = "different"
                before = copy.deepcopy(raw)
                with self.assertRaises(ToolError) as caught:
                    normalize(raw, 0, "s", duration_ms=5000)
                self.assertEqual(caught.exception.code, "timeline")
                self.assertEqual(raw, before)

    def test_nearby_candidates_still_require_language_text_and_order(self):
        for broken in ["language", "empty", "previous_order", "following_order"]:
            with self.subTest(broken=broken):
                raw = nearby_point()
                sentences = raw["transcripts"][0]["sentences"]
                if broken == "language":
                    sentences[-1]["language"] = "de"
                elif broken == "empty":
                    sentences[-1]["text"] = " "
                elif broken == "previous_order":
                    sentences[0]["end_time"] = 2001
                else:
                    sentences[-1]["begin_time"] = 1999
                with self.assertRaises(ToolError) as caught:
                    normalize(raw, 0, "s", duration_ms=5000)
                self.assertEqual(caught.exception.code, "timeline")

    def test_chained_groups_measure_distance_from_original_positive_interval(self):
        raw = result()
        sentences = raw["transcripts"][0]["sentences"]
        sentences[:] = [point_sentence(1000, "A synthetic sentence one."),
            {"begin_time": 1400, "end_time": 2400, "text": "The timed sentence.", "language": "en"},
            point_sentence(2800, "Another synthetic point.")]
        sources, _ = normalize(raw, 0, "s", duration_ms=5000, preserve_sentences=True)
        self.assertEqual(len(sources), 1)
        self.assertEqual((sources[0]["begin_ms"], sources[0]["end_ms"]), (1000, 2800))
        self.assertEqual(sources[0]["asr_sentence_indices"], [0, 1, 2])
        self.assertEqual([a["distance_ms"] for a in sources[0]["timing_adjustments"]], [400, 400])
        self.assertTrue(all((a["original_neighbor_begin_ms"], a["original_neighbor_end_ms"]) == (1400, 2400)
                            for a in sources[0]["timing_adjustments"]))
        sentences.append(point_sentence(3200, "A distant synthetic point."))
        with self.assertRaises(ToolError) as caught:
            normalize(raw, 0, "s", duration_ms=5000)
        self.assertEqual(caught.exception.code, "timeline")
        sentences.append({"begin_time": 3200, "end_time": 4500, "text": "The final sentence.", "language": "en"})
        sources, _ = normalize(raw, 0, "s", duration_ms=5000, preserve_sentences=True)
        self.assertEqual([s["asr_sentence_indices"] for s in sources], [[0, 1, 2], [3, 4]])
        self.assertEqual(sources[-1]["timing_adjustments"][0]["kind"], "merged_asr_boundary_point")
        self.assertEqual(sources[-1]["timing_adjustments"][0]["direction"], "next")

    def test_long_same_point_group_reuses_previous_interval_and_keeps_all_evidence(self):
        raw = boundary_points()
        before = copy.deepcopy(raw)
        sources, warnings = normalize(raw, 1250, "s", duration_ms=5000, preserve_sentences=True)
        self.assertEqual(raw, before)
        self.assertEqual(len(sources), 2)
        source = sources[0]
        sentences = raw["transcripts"][0]["sentences"]
        self.assertEqual(source["text"], " ".join(s["text"] for s in sentences[:3]))
        self.assertEqual((source["begin_ms"], source["end_ms"]), (1250, 3250))
        self.assertEqual(source["asr_sentence_indices"], [0, 1, 2])
        self.assertEqual(source["original_timings"], [
            {"asr_sentence_index": i, "begin_ms": s["begin_time"] + 1250,
             "end_ms": s["end_time"] + 1250} for i, s in enumerate(sentences[:3])])
        self.assertEqual(source["words"], [])
        self.assertEqual(source["timing_adjustments"], [
            {"asr_sentence_index": i, "anchor_ms": 2000, "original_begin_ms": 2000, "original_end_ms": 2000,
             "kind": "merged_asr_boundary_point", "direction": "previous"} for i in [1, 2]])
        self.assertTrue(warnings)
        intervals, _ = plan_display_timing(sources, version=2)
        self.assertEqual([(i["begin_ms"], i["end_ms"]) for i in intervals], [(1250, 3250), (3250, 5250)])
        for version in [1, 2, 3]:
            with self.subTest(saved_version=version), self.assertRaises(ToolError) as caught:
                normalize(raw, 1250, "s", duration_ms=5000, normalizer_version=version)
            self.assertEqual(caught.exception.code, "timeline")

    def test_group_uses_next_original_interval_when_previous_cannot_anchor_it(self):
        for previous in [None, "other_language", "gap"]:
            with self.subTest(previous=previous):
                raw = boundary_points()
                sentences = raw["transcripts"][0]["sentences"]
                if previous is None:
                    sentences.pop(0)
                elif previous == "other_language":
                    sentences[0]["language"] = "zh"
                    sentences[0]["text"] = "前句内容。"
                else:
                    sentences[0]["end_time"] = 1999
                first = 0 if previous is None else 1
                sources, _ = normalize(raw, 0, "s", duration_ms=5000, preserve_sentences=True)
                source = sources[-1]
                self.assertEqual((source["begin_ms"], source["end_ms"]), (2000, 4000))
                self.assertEqual(source["asr_sentence_indices"], list(range(first, first + 3)))
                self.assertEqual(source["text"], " ".join(s["text"] for s in sentences[first:]))
                self.assertEqual(source["words"], [])
                self.assertTrue(all(a["direction"] == "next" and a["kind"] == "merged_asr_boundary_point" and
                                    "distance_ms" not in a for a in source["timing_adjustments"]))

    def test_long_point_uses_exact_previous_boundary_even_with_space_for_a_window(self):
        raw = boundary_points()
        sentences = raw["transcripts"][0]["sentences"]
        sentences.pop(2)
        sentences[-1]["begin_time"] = 3500
        sources, _ = normalize(raw, 0, "s", duration_ms=5000, preserve_sentences=True)
        self.assertEqual((sources[0]["begin_ms"], sources[0]["end_ms"]), (0, 2000))
        self.assertEqual(sources[0]["asr_sentence_indices"], [0, 1])

    def test_mixed_short_and_long_group_is_checked_before_any_display_window(self):
        for short_first in [True, False]:
            with self.subTest(short_first=short_first):
                raw = result()
                points = [point_sentence(2000), point_sentence(2000, "A synthetic sentence one.")]
                raw["transcripts"][0]["sentences"] = points if short_first else list(reversed(points))
                with self.assertRaises(ToolError) as caught:
                    normalize(raw, 0, "s", duration_ms=5000)
                self.assertEqual(caught.exception.code, "timeline")
                raw["transcripts"][0]["sentences"].append({
                    "begin_time": 2401, "end_time": 4000, "text": "Following sentence.", "language": "en"})
                with self.assertRaises(ToolError) as caught:
                    normalize(raw, 0, "s", duration_ms=5000)
                self.assertEqual(caught.exception.code, "timeline")

    def test_group_accepts_complete_multiword_evidence_and_keeps_punctuation(self):
        raw = boundary_points()
        sentences = raw["transcripts"][0]["sentences"]
        sentences[1] = point_sentence(2000)
        point = sentences[2]
        point["words"] = [{"begin_time": 2000, "end_time": 2000, "text": "Another", "punctuation": ""},
                          {"begin_time": 2000, "end_time": 2000, "text": "synthetic point", "punctuation": "."}]
        sources, _ = normalize(raw, 0, "s", duration_ms=5000, preserve_sentences=True)
        self.assertEqual(sources[0]["text"], "Earlier sentence. Yes. Another synthetic point.")
        self.assertEqual(sources[0]["asr_sentence_indices"], [0, 1, 2])
        self.assertEqual(sources[0]["words"], [])

    def test_long_point_cannot_borrow_a_short_points_display_window(self):
        raw = result()
        raw["transcripts"][0]["sentences"] = [point_sentence(0), point_sentence(600, "A synthetic sentence one."),
            {"begin_time": 600, "end_time": 2000, "text": "下一句内容。", "language": "zh"}]
        with self.assertRaises(ToolError) as caught:
            normalize(raw, 0, "s", duration_ms=5000)
        self.assertEqual(caught.exception.code, "timeline")

    def test_boundary_groups_share_an_original_positive_interval_without_reordering(self):
        raw = result()
        raw["transcripts"][0]["sentences"] = [point_sentence(1000, "A synthetic sentence one."),
            {"begin_time": 1000, "end_time": 2000, "text": "The timed sentence.", "language": "en"},
            point_sentence(2000, "Another synthetic point.")]
        sources, _ = normalize(raw, 0, "s", duration_ms=5000, preserve_sentences=True)
        self.assertEqual(len(sources), 1)
        self.assertEqual((sources[0]["begin_ms"], sources[0]["end_ms"]), (1000, 2000))
        self.assertEqual(sources[0]["asr_sentence_indices"], [0, 1, 2])
        self.assertEqual(sources[0]["text"], "A synthetic sentence one. The timed sentence. Another synthetic point.")
        self.assertEqual(sources[0]["words"], [])

    def test_incomplete_evidence_or_nonboundary_neighbors_are_rejected_without_mutating_raw(self):
        for broken in ["no_words", "wrong_word", "punctuation", "word_point", "word_float", "sentence_float",
                       "unsupported", "mixed_language", "empty_member", "no_neighbor", "too_distant_neighbor",
                       "reverse_neighbor", "reverse_previous", "reverse_following", "empty_neighbor"]:
            with self.subTest(broken=broken):
                raw = boundary_points()
                sentences = raw["transcripts"][0]["sentences"]
                if broken == "no_words":
                    sentences[1].pop("words")
                elif broken == "wrong_word":
                    sentences[1]["words"][0]["text"] = "different text"
                elif broken == "punctuation":
                    sentences[1]["words"][0]["punctuation"] = "!"
                elif broken == "word_point":
                    sentences[1]["words"][0]["begin_time"] = 1999
                elif broken == "word_float":
                    sentences[1]["words"][0]["end_time"] = 2000.0
                elif broken == "sentence_float":
                    sentences[1]["begin_time"] = 2000.0
                elif broken == "unsupported":
                    for sentence in sentences:
                        sentence["language"] = "und"
                elif broken == "mixed_language":
                    sentences[2]["language"] = "de"
                elif broken == "empty_member":
                    sentences[2]["text"] = " "
                elif broken == "no_neighbor":
                    sentences[:] = sentences[1:3]
                elif broken == "too_distant_neighbor":
                    sentences[0]["end_time"] = 1599
                    sentences[-1]["begin_time"] = 2401
                elif broken == "reverse_neighbor":
                    sentences[-1].update(begin_time=1999, end_time=1998)
                elif broken == "reverse_previous":
                    sentences[0]["end_time"] = 2001
                elif broken == "reverse_following":
                    sentences[-1]["begin_time"] = 1999
                else:
                    sentences[0]["text"] = " "
                    sentences[-1]["text"] = " "
                before = copy.deepcopy(raw)
                with self.assertRaises(ToolError) as caught:
                    normalize(raw, 0, "s", duration_ms=5000)
                self.assertEqual(caught.exception.code, "timeline")
                self.assertEqual(raw, before)

class PointSentenceGroupPipelineTests(PipelineFixture):
    def test_v3_failure_reflows_to_v4_and_resumes_saved_raw_without_resubmitting_asr(self):
        raw = boundary_points()
        fake = FakeService(raw)
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["contract"] = asr_contract(3)
        self.store.update(self.job_id, manifest=manifest)
        for _ in range(2):
            self.assertEqual(self.run_fake(fake), "failed")
            self.assertEqual(self.store.job(self.job_id)["error_code"], "timeline")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)
        cache = self.config.data / "cache" / self.job_id
        raw_bytes = (cache / "raw-0000.json").read_bytes()
        before = copy.deepcopy(self.store.job(self.job_id))
        with mock.patch("requests.request") as request, mock.patch("requests.post") as upload:
            Engine(self.config, self.store).reflow(self.job_id)
        request.assert_not_called()
        upload.assert_not_called()
        prepared = self.store.job(self.job_id)
        manifest = prepared["manifest"]
        self.assertEqual(manifest["contract"]["normalizer"], 4)
        self.assertEqual(manifest["normalized_version"], 4)
        snapshot = json.loads((self.config.root / manifest["reflow"]["snapshot"]).read_text(encoding="utf-8"))
        self.assertEqual(snapshot["job"], before)
        sources = self.store.sources(self.job_id)
        self.assertEqual(len(sources), 2)
        self.assertEqual([i for source in sources for i in source["asr_sentence_indices"]], [0, 1, 2, 3])
        self.assertEqual(re.sub(r"\s", "", "".join(s["text"] for s in sources)),
                         re.sub(r"\s", "", "".join(s["text"] for s in raw["transcripts"][0]["sentences"])))
        # Prove recovery can validate v4 against raw when the count/file proof is unavailable.
        manifest.pop("normalized_count")
        (cache / manifest.pop("normalized_file")).unlink()
        self.store.update(self.job_id, manifest=manifest)
        self.source.unlink()
        fake.calls.clear()
        fake.mt_failure.add(sources[0]["text"])
        self.assertEqual(self.run_fake(fake), "failed")
        fake.mt_failure.clear()
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(fake.named("/asr/transcription"), [])
        self.assertEqual(len(fake.uploads), 1)
        self.assertEqual((cache / "raw-0000.json").read_bytes(), raw_bytes)
        self.assertEqual(self.store.sources(self.job_id), sources)
