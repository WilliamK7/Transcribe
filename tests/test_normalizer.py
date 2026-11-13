from __future__ import annotations

import copy
import json
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.subtitles import normalize
from tests.fixtures import FakeService, PipelineFixture, TemporaryTest, point_sentence, result


class NormalizerBoundaryTests(TemporaryTest):
    @staticmethod
    def tail_result(end=1117):
        raw = result(("完整字幕。", "zh"))
        raw["transcripts"][0]["sentences"][0].update(
            begin_time=700, end_time=end,
            words=[{"begin_time": 700, "end_time": 900, "text": "完整", "punctuation": ""},
                   {"begin_time": 980, "end_time": 980, "text": "字", "punctuation": ""},
                   {"begin_time": 980, "end_time": end, "text": "幕", "punctuation": "。"}])
        return raw

    def test_final_word_can_corroborate_short_segment_end_overrun(self):
        for end in (1002, 1117, 1200):
            with self.subTest(end=end):
                raw = self.tail_result(end)
                original = copy.deepcopy(raw)
                sources, warnings = normalize(raw, 12345, "s", duration_ms=1000)
                source = sources[0]
                self.assertEqual(raw, original)
                self.assertEqual((source["begin_ms"], source["end_ms"]), (13045, 13345))
                self.assertEqual(source["text"], "完整字幕。")
                self.assertEqual(source["original_timings"], [{"asr_sentence_index": 0,
                                  "begin_ms": 13045, "end_ms": 12345 + end}])
                self.assertEqual(source["timing_adjustments"], [{"asr_sentence_index": 0,
                                  "original_begin_ms": 700, "original_end_ms": end,
                                  "new_end_ms": 1000, "kind": "clipped_segment_end"}])
                self.assertEqual((source["words"], source["word_timing_status"]), ([], "repaired_sentence"))
                self.assertTrue(warnings)
                self.assertNotIn(source["text"], " ".join(warnings))

    def test_segment_end_overrun_rejects_missing_or_contradictory_evidence(self):
        cases = {}
        cases["over_limit"] = self.tail_result(1201)
        cases["missing_words"] = self.tail_result()
        cases["missing_words"]["transcripts"][0]["sentences"][0].pop("words")
        cases["incomplete_words"] = self.tail_result()
        cases["incomplete_words"]["transcripts"][0]["sentences"][0]["words"][-1]["text"] = "别"
        cases["word_starts_at_end"] = self.tail_result()
        cases["word_starts_at_end"]["transcripts"][0]["sentences"][0]["words"][-1]["begin_time"] = 1000
        cases["last_word_ends_early"] = self.tail_result()
        cases["last_word_ends_early"]["transcripts"][0]["sentences"][0]["words"][-1]["end_time"] = 1100
        cases["invalid_word_order"] = self.tail_result()
        cases["invalid_word_order"]["transcripts"][0]["sentences"][0]["words"][1]["begin_time"] = 800
        cases["unsupported_language"] = self.tail_result()
        cases["unsupported_language"]["transcripts"][0]["sentences"][0]["language"] = "xx"
        cases["invalid_language_type"] = self.tail_result()
        cases["invalid_language_type"]["transcripts"][0]["sentences"][0]["language"] = []
        cases["non_final_sentence"] = self.tail_result()
        cases["non_final_sentence"]["transcripts"][0]["sentences"].append(
            {"begin_time": 900, "end_time": 990, "text": "后句。", "language": "zh"})
        cases["sentence_starts_at_end"] = self.tail_result()
        cases["sentence_starts_at_end"]["transcripts"][0]["sentences"][0]["begin_time"] = 1000
        for name, raw in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ToolError) as caught:
                    normalize(raw, 0, "s", duration_ms=1000)
                self.assertEqual(caught.exception.code, "timeline")

    def test_segment_end_tolerates_one_millisecond_only_before_global_offset(self):
        for begin, end, valid in [(0, 1000, True), (900, 1001, True), (900, 1002, False),
                                  (1000, 1001, False), (5000, 6000, False)]:
            with self.subTest(begin=begin, end=end):
                raw = result(("完整字幕。", "zh"))
                raw["transcripts"][0]["sentences"][0].update(begin_time=begin, end_time=end)
                if valid:
                    cues, _ = normalize(raw, 12345, "s", duration_ms=1000)
                    self.assertEqual(cues[0]["end_ms"], end + 12345)
                else:
                    with self.assertRaises(ToolError) as caught:
                        normalize(raw, 12345, "s", duration_ms=1000)
                    self.assertEqual(caught.exception.code, "timeline")

    def test_nonempty_summary_requires_nonempty_sentence_text(self):
        for sentences in [[], [{"begin_time": 0, "end_time": 100, "text": " \x00 ", "language": "zh"}]]:
            raw = {"transcripts": [{"text": "汇总有文字。", "sentences": sentences}]}
            with self.assertRaises(ToolError) as caught:
                normalize(raw, 0, "s", duration_ms=1000)
            self.assertEqual(caught.exception.code, "response_schema")
        for raw in [result(), {"transcripts": [{"text": "", "sentences": []}]}, result(("完整字幕。", "zh"))]:
            normalize(raw, 0, "s", duration_ms=1000)

    def test_negative_global_offset_and_short_point_repair(self):
        raw = result(("完整字幕。", "zh"))
        cues, warnings = normalize(raw, -100, "s", duration_ms=1000)
        self.assertEqual((cues[0]["begin_ms"], cues[0]["end_ms"]), (0, 800))
        self.assertTrue(warnings)
        raw = result()
        raw["transcripts"][0]["sentences"].append(point_sentence(900))
        cues, _ = normalize(raw, 500, "s", duration_ms=1000)
        self.assertEqual((cues[0]["begin_ms"], cues[0]["end_ms"]), (1400, 1500))


class NormalizerVersionPipelineTests(PipelineFixture):
    def test_failed_tail_overrun_resumes_from_saved_raw_without_resubmitting(self):
        raw = result(("完整字幕。", "zh"))
        raw["transcripts"][0]["sentences"][0].update(
            begin_time=8000, end_time=10117,
            words=[{"begin_time": 8000, "end_time": 9900, "text": "完整", "punctuation": ""},
                   {"begin_time": 9960, "end_time": 10117, "text": "字幕", "punctuation": "。"}])
        fake = FakeService(raw)
        with mock.patch("subtitle_tool.engine.normalize", side_effect=ToolError("时间越界", "timeline")):
            self.assertEqual(self.run_fake(fake), "failed")
        raw_path = self.config.data / "cache" / self.job_id / "raw-0000.json"
        self.assertEqual(json.loads(raw_path.read_text(encoding="utf-8")), raw)
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)
        self.assertEqual(json.loads(raw_path.read_text(encoding="utf-8")), raw)
        source = self.store.sources(self.job_id)[0]
        self.assertEqual(source["end_ms"], 10000)
        self.assertEqual(source["timing_adjustments"][0]["kind"], "clipped_segment_end")
        self.assertEqual(self.store.job(self.job_id)["manifest"]["contract"]["normalizer"], 4)

    def test_new_task_uses_v4_and_preserves_invalid_raw_without_resubmitting(self):
        fake = FakeService(result(("完整字幕。", "zh")))
        fake.raw["transcripts"][0]["sentences"][0].update(begin_time=100000, end_time=101000)
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "timeline")
        self.assertEqual(self.store.job(self.job_id)["manifest"]["contract"]["normalizer"], 4)
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)
        self.assertTrue((self.config.data / "cache" / self.job_id / "raw-0000.json").is_file())
