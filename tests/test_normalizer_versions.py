from __future__ import annotations

import copy
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.subtitles import normalize
from tests.fixtures import FakeService, PipelineFixture, TemporaryTest, point_sentence, result


class NormalizerBoundaryTests(TemporaryTest):
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

    def test_old_versions_keep_their_validation_and_v3_rejects_unknown_versions(self):
        raw = result(("完整字幕。", "zh"))
        raw["transcripts"][0]["sentences"][0].update(begin_time=5000, end_time=6000)
        for version in [1, 2]:
            self.assertEqual(len(normalize(raw, 0, "s", duration_ms=1000, normalizer_version=version)[0]), 1)
            empty = {"transcripts": [{"text": "汇总。", "sentences": []}]}
            self.assertEqual(normalize(empty, 0, "s", normalizer_version=version)[0], [])
        with self.assertRaises(ToolError) as caught:
            normalize(raw, 0, "s", normalizer_version=99)
        self.assertEqual(caught.exception.code, "cache_version")

    def test_v3_keeps_negative_global_offset_and_point_repair(self):
        raw = result(("完整字幕。", "zh"))
        cues, warnings = normalize(raw, -100, "s", duration_ms=1000)
        self.assertEqual((cues[0]["begin_ms"], cues[0]["end_ms"]), (0, 800))
        self.assertTrue(warnings)
        raw = result()
        raw["transcripts"][0]["sentences"].append(point_sentence(900))
        cues, _ = normalize(raw, 500, "s", duration_ms=1000)
        self.assertEqual((cues[0]["begin_ms"], cues[0]["end_ms"]), (1400, 1500))


class NormalizerVersionPipelineTests(PipelineFixture):
    def test_new_task_uses_v3_and_preserves_invalid_raw_without_resubmitting(self):
        fake = FakeService(result(("完整字幕。", "zh")))
        fake.raw["transcripts"][0]["sentences"][0].update(begin_time=100000, end_time=101000)
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "timeline")
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)
        self.assertTrue((self.config.data / "cache" / self.job_id / "raw-0000.json").is_file())

    def test_old_v2_raw_uses_saved_version_but_explicit_reflow_uses_v3(self):
        fake = FakeService(result(("完整字幕。", "zh")))
        fake.raw["transcripts"][0]["sentences"][0].update(begin_time=100000, end_time=101000)
        def interrupt(job_id, stage, *args):
            if stage == "segmenting":
                raise KeyboardInterrupt()
        with mock.patch("requests.request", side_effect=fake.request), mock.patch("requests.post", side_effect=fake.upload):
            self.assertEqual(Engine(self.config, self.store, progress=interrupt).run(self.job_id), "cancelled")
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["contract"]["normalizer"] = 2
        self.store.update(self.job_id, "queued", manifest)
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(self.store.job(self.job_id)["manifest"]["normalized_version"], 2)
        before = copy.deepcopy(self.store.job(self.job_id))
        with self.assertRaises(ToolError) as caught:
            Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(caught.exception.code, "timeline")
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual(len(fake.named("/asr/transcription")), 1)
