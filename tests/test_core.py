from __future__ import annotations

import json
import os
import shutil
import signal
import io
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

import requests

from subtitle_tool.api import Http
from subtitle_tool.common import ASR_MODEL, ToolError, redact
from subtitle_tool.config import Config, Settings
from subtitle_tool.engine import Engine
from subtitle_tool.media import Media, run_process
from subtitle_tool.main import main
from subtitle_tool.subtitles import normalize, render_srt, route
from subtitle_tool.publication import write_srt
from subtitle_tool.review import ReviewDecision

from tests.fixtures import set_asr_account, enqueue_current, ROOT, FakeService, PipelineFixture, TemporaryTest, point_sentence, result, response


class ConfigTests(TemporaryTest):
    def test_asr_key_uses_environment_then_local_file(self):
        config = Config(self.path)
        config.set_key('local-asr')
        with mock.patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''}):
            self.assertEqual(config.get_key(), 'local-asr')
        with mock.patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'environment-asr'}):
            self.assertEqual(config.get_key(), 'environment-asr')

    def test_mt_workers_requires_a_positive_integer(self):
        for workers in (1, 4, 8, 16, 32):
            with self.subTest(workers=workers):
                Settings(mt_workers=workers).validate()
        for workers in (0, -1, True, 1.5, '8', None):
            with self.subTest(workers=workers), self.assertRaises(ToolError):
                Settings(mt_workers=workers).validate()


class SubtitleTests(TemporaryTest):
    def test_zero_duration_short_sentence_gets_bounded_display_window_without_mutating_raw(self):
        raw = result(("First sentence.", "en"), ("Last sentence.", "en"))
        raw["transcripts"][0]["sentences"][1].update(begin_time=2400, end_time=4000)
        raw["transcripts"][0]["sentences"].insert(1, point_sentence(1200))
        before = json.dumps(raw, sort_keys=True)
        cues, warnings = normalize(raw, 1250, "0", duration_ms=4500)
        self.assertEqual(json.dumps(raw, sort_keys=True), before)
        self.assertEqual((cues[1]["begin_ms"], cues[1]["end_ms"]), (2450, 3450))
        self.assertEqual(cues[1]["text"], "Yes.")
        self.assertEqual(cues[1]["timing_adjustments"][0]["kind"], "point_display_window")
        self.assertTrue(any("零时长" in message for message in warnings))

    def test_zero_duration_at_shared_boundary_merges_same_language_and_preserves_text(self):
        raw = result(("First sentence.", "en"), ("Next sentence.", "en"))
        raw["transcripts"][0]["sentences"][0]["end_time"] = 1000
        raw["transcripts"][0]["sentences"].insert(1, point_sentence(1000))
        cues, warnings = normalize(raw, 0, "0", duration_ms=3000)
        self.assertEqual(len(cues), 2)
        self.assertEqual((cues[0]["begin_ms"], cues[0]["end_ms"]), (0, 1000))
        self.assertEqual(cues[0]["text"], "First sentence. Yes.")
        self.assertEqual(cues[0]["asr_sentence_indices"], [0, 1])
        self.assertTrue(any("合并" in message for message in warnings))

    def test_zero_duration_before_close_sentence_merges_into_next(self):
        raw = result(("First sentence.", "en"), ("Next sentence.", "en"))
        raw["transcripts"][0]["sentences"][1].update(begin_time=4080, end_time=4900)
        raw["transcripts"][0]["sentences"].insert(1, point_sentence(4000))
        cues, _ = normalize(raw, 0, "0", duration_ms=5500)
        self.assertEqual(cues[1]["text"], "Yes. Next sentence.")
        self.assertEqual((cues[1]["begin_ms"], cues[1]["end_ms"]), (4000, 4900))

    def test_zero_duration_without_corroboration_or_cross_language_boundary_still_stops(self):
        for raw in [result(("Invalid.", "en")), result(("第一句。", "zh"), ("第二句。", "zh"))]:
            sentences = raw["transcripts"][0]["sentences"]
            if len(sentences) == 1:
                sentences[0]["end_time"] = sentences[0]["begin_time"]
            else:
                sentences[0]["end_time"] = 1000
                sentences.insert(1, point_sentence(1000))
            with self.assertRaises(ToolError) as error:
                normalize(raw, 0, "0", duration_ms=3000)
            self.assertEqual(error.exception.code, "timeline")

    def test_zero_duration_display_window_does_not_exceed_audio(self):
        raw = result(("First sentence.", "en"))
        raw["transcripts"][0]["sentences"].append(point_sentence(4900))
        cues, _ = normalize(raw, 0, "0", duration_ms=5100)
        self.assertEqual((cues[1]["begin_ms"], cues[1]["end_ms"]), (4900, 5100))

    def test_chinese_and_brands_skip_translation(self):
        for text, lang in [("欢迎使用百炼。", "zh"), ("我们使用 Python 和 API。", "zh"), ("今天天气好。", "yue")]:
            self.assertEqual(route(text, lang)[0], "original")

    def test_japanese_han_is_not_chinese(self):
        self.assertEqual(route("東京都", "ja", ["ja"])[0], "translate")
        self.assertEqual(route("今日は良い天気です。", "")[0], "translate")

    def test_missing_language_is_not_default_english(self):
        self.assertEqual(route("OK", "")[0], "uncertain")
        self.assertEqual(route("Hello world", "")[0], "uncertain")
        self.assertEqual(route("東京都港区", "")[0], "uncertain")
        self.assertEqual(route("欢迎来到新课程。", "")[0], "original")

    def test_substantive_mixed_sentence_is_translated(self):
        self.assertEqual(route("然后他说 please turn on the light now。", "zh")[0], "translate")
        self.assertEqual(route("然后他说 PLEASE TURN ON THE LIGHT NOW。", "zh")[0], "translate")

    def test_wrong_language_label_requires_review(self):
        self.assertEqual(route("This is a complete English sentence", "zh")[0], "uncertain")
        self.assertEqual(route("欢迎来到这里。", "en")[0], "uncertain")

    def test_offset_sentence_only_and_unicode_srt(self):
        cues, _ = normalize(result(("مرحبا بالعالم", "ar")), 1511, "01")
        cues[0]["translated"] = "你好，世界"
        content = render_srt(cues, Settings())
        self.assertIn(b"00:00:01,511 --> 00:00:02,411", content)
        self.assertTrue(content.decode().index("مرحبا") < content.decode().index("你好"))
        chinese_first = render_srt(cues, Settings(chinese_first=True))
        self.assertTrue(chinese_first.decode().index("你好") < chinese_first.decode().index("مرحبا"))
        self.assertFalse(content.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))

    def test_invalid_time_stops_and_overlap_warns(self):
        invalid = result(("你好", "zh"))
        invalid["transcripts"][0]["sentences"][0]["end_time"] = 0
        with self.assertRaises(ToolError):
            normalize(invalid, 0, "0")
        overlap = result(("你好", "zh"), ("你好", "zh"))
        overlap["transcripts"][0]["sentences"][1]["begin_time"] = 800
        cues, warnings = normalize(overlap, 0, "0")
        self.assertEqual(len(cues), 2)
        self.assertTrue(any("重叠" in w for w in warnings))

    def test_word_boundaries_only_when_complete_and_reliable(self):
        raw = result(("First part. Second part.", "en"))
        sentence = raw["transcripts"][0]["sentences"][0]
        sentence.update(end_time=10000, words=[
            {"begin_time": 0, "end_time": 3200, "text": "First part", "punctuation": "."},
            {"begin_time": 3700, "end_time": 10000, "text": "Second part", "punctuation": "."}])
        sources, _ = normalize(raw, 1234, "s")
        from subtitle_tool.subtitles import plan_fragments
        from subtitle_tool.config import Settings
        cues = plan_fragments(sources[0], Settings())
        self.assertEqual([(c["begin_ms"], c["end_ms"]) for c in cues], [(1234, 4434), (4934, 11234)])
        sentence["language"] = "ar"
        self.assertEqual(len(normalize(raw, 0, "s")[0]), 1)
        sentence["language"] = "en"
        sentence["words"][0]["text"] = "omitted"
        self.assertEqual(len(normalize(raw, 0, "s")[0]), 1)

    def test_atomic_overwrite_backup_and_refusal(self):
        path = self.path / "多点.课程 & (1).srt"
        path.write_bytes(b"original")
        with self.assertRaises(ToolError):
            write_srt(path, b"new")
        self.assertEqual(path.read_bytes(), b"original")
        with mock.patch("subtitle_tool.publication.shutil.copyfileobj", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_srt(path, b"new", True)
        self.assertEqual(path.read_bytes(), b"original")
        backup = write_srt(path, b"new", True)
        self.assertEqual(Path(backup).read_bytes(), b"original")
        self.assertEqual(path.read_bytes(), b"new")
        self.assertFalse(list(self.path.glob("*.tmp")))

    def test_incomplete_translation_never_writes(self):
        cues, _ = normalize(result(("Hello world", "en")), 0, "0")
        with self.assertRaises(ToolError):
            render_srt(cues, Settings())


class HttpTests(unittest.TestCase):
    def test_bounded_429_and_5xx_retry(self):
        http = Http(threading.Event(), attempts=3)
        with mock.patch("requests.request", side_effect=[response({}, 429), response({}, 503), response({"ok": True})]) as request, mock.patch.object(http, "pause"):
            self.assertEqual(http.json("GET", "https://example.test")["ok"], True)
            self.assertEqual(request.call_count, 3)

    def test_redaction(self):
        sanitized = redact("Bearer sk-secret-value https://oss.example/a?Signature=secret oss://private/file")
        self.assertNotIn("sk-secret", sanitized)
        self.assertNotIn("Signature", sanitized)
        self.assertNotIn("private/file", sanitized)


class MediaTests(TemporaryTest):
    def test_exact_pcm_splitting_retains_every_sample(self):
        source = self.path / "audio.wav"
        payload = b"\x20\x03" * (16000 * 35 + 9)
        with wave.open(str(source), "wb") as writer:
            writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            writer.writeframes(payload)
        segments = Media(ROOT, threading.Event()).split(source, self.path, 500000)
        joined = b""
        previous = 0
        for segment in segments:
            self.assertEqual(segment["offset_ms"], previous * 1000 // 16000)
            with wave.open(str(self.path / segment["audio"]), "rb") as reader:
                joined += reader.readframes(reader.getnframes())
                previous += reader.getnframes()
        self.assertEqual(joined, payload)

    def test_multitrack_and_channels_require_choice(self):
        media = Media(ROOT, threading.Event())
        info = {"origin_ms": 500, "tracks": [{"index": 1, "start_time": "1.25", "channels": 2}, {"index": 2, "channels": 1}]}
        with self.assertRaises(ToolError):
            media.select(info)
        self.assertEqual(media.select(info, 1, "left"), (1, 750))
        with self.assertRaises(ToolError):
            media.select(info, 1, "ask")


class PipelineTests(PipelineFixture):
    def test_chinese_end_to_end_zero_mt_and_contract(self):
        fake = FakeService(result(("欢迎使用百炼。", "zh"), ("我们使用 Python 和 API。", "zh")))
        self.assertEqual(self.run_fake(fake), "completed")
        output = self.source.with_suffix(".srt")
        self.assertTrue(output.is_file())
        self.assertEqual(self.store.counts(self.job_id).get("mt", 0), 0)
        request = fake.named("/asr/transcription")[0][2]
        self.assertEqual(request["json"]["model"], ASR_MODEL)
        self.assertEqual(set(request["json"]["input"]), {"file_url"})
        self.assertNotIn("language", request["json"]["parameters"])
        self.assertEqual(request["headers"]["X-DashScope-OssResourceResolve"], "enable")
        self.assertEqual(fake.named("/uploads")[0][2]["params"]["model"], ASR_MODEL)
        self.assertNotIn("Authorization", fake.uploads[0][1])
        self.assertEqual(fake.named("/asr/transcription")[0][1], "https://offline-space.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription")
        self.assertEqual(fake.named("/uploads")[0][1], "https://dashscope.aliyuncs.com/api/v1/uploads")
        self.assertEqual(request['headers']['Authorization'], 'Bearer ' + self.config.get_key())
        self.assertIn(b"Content-Type: audio/flac", fake.uploads[0][2])
        segment = self.store.job(self.job_id)["manifest"]["segments"][0]
        self.assertTrue(segment["oss_url"].startswith("oss://"))
        self.assertTrue(fake.uploads[0][2].rfind(b'name="file"') > fake.uploads[0][2].rfind(b'name="key"'))
        self.assertEqual(output.name, "中文 多点.片段 & (1).srt")
        self.assertEqual(output.read_text(encoding="utf-8").count("欢迎使用百炼。"), 1)
        self.assertFalse(list((self.config.data / "cache" / self.job_id).glob("*.wav")))

    def test_foreign_concurrency_matches_ids_and_simplifies(self):
        fake = FakeService(result(("First sentence.", "en"), ("Second sentence.", "en"), ("今日は晴れです。", "ja")))
        self.assertEqual(self.run_fake(fake), "completed")
        output = self.source.with_suffix(".srt").read_text(encoding="utf-8")
        self.assertTrue(output.index("First sentence") < output.index("Second sentence"))
        self.assertIn("繁体译文", output)
        self.assertTrue(output.index("First sentence.") < output.index("繁体译文"))
        for call in fake.named("/chat/completions"):
            body = call[2]["json"]
            self.assertEqual(body["model"], 'group/transcribe')
            self.assertEqual(len(body['messages']), 2)
            self.assertEqual(body['messages'][0]['role'], 'system')

    def test_resume_reuses_asr_and_saved_glossary_failure(self):
        fake = FakeService(result(("First sentence.", "en"), ("Second sentence.", "en")))
        fake.mt_failure.add("Second sentence.")
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertFalse(self.source.with_suffix(".srt").exists())
        self.assertFalse(any(c["translated"] for c in self.store.cues(self.job_id)))
        before_resume = len(fake.named("/chat/completions"))
        fake.mt_failure.clear()
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)
        self.assertEqual(len(fake.named("/chat/completions")) - before_resume, 2)

    def test_mt_failure_stops_scheduling_beyond_concurrency(self):
        fake = FakeService(result(*[(f"Sentence number {i}.", "en") for i in range(12)]))
        fake.mt_failure.add("Sentence number 0.")
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertLessEqual(len(fake.named("/chat/completions")), self.config.settings.mt_workers)

    def test_normalized_cache_recovers_after_raw_links_are_unavailable(self):
        fake = FakeService(result(("First sentence.", "en"), ("Second sentence.", "en")))
        fake.mt_failure.add("Second sentence.")
        self.assertEqual(self.run_fake(fake), "failed")
        raw = self.config.data / "cache" / self.job_id / "raw-0000.json"
        raw.unlink()
        fake.query_failure = True
        before_resume = len(fake.named("/chat/completions"))
        fake.mt_failure.clear()
        before = self.store.counts(self.job_id).get("asr_query", 0)
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(self.store.counts(self.job_id).get("asr_query", 0), before)


    def test_interrupted_durable_submit_marker_becomes_unknown(self):
        job = self.store.job(self.job_id)
        manifest = job["manifest"]
        manifest["segments"] = [{"id": "0000", "audio": "audio.wav", "offset_ms": 0,
                                  "duration_ms": 1000, "submitting": True}]
        self.store.update(self.job_id, "submitting", manifest)
        Engine(self.config, self.store).recover_jobs()
        self.assertEqual(self.store.job(self.job_id)["state"], "submission_unknown")
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        self.assertEqual(self.run_fake(fake), "submission_unknown")
        self.assertEqual(fake.calls, [])

    def test_success_in_other_asr_result_layout_is_not_accepted(self):
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        original = fake.request
        def wrong_layout(method, url, **kwargs):
            if "/tasks/" in url:
                fake.calls.append((method, url, kwargs))
                return response({"output": {"task_status": "SUCCEEDED", "results": [{
                    "transcription_url": "https://example.test/other.json"}]}})
            return original(method, url, **kwargs)
        fake.request = wrong_layout
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "response_schema")
        self.assertFalse(self.source.with_suffix(".srt").exists())

    def test_network_query_resume_does_not_resubmit_asr(self):
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        fake.query_failure = True
        self.assertEqual(self.run_fake(fake), "failed")
        fake.query_failure = False
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)

    def test_download_resume_uses_known_task(self):
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        fake.download_failure = True
        self.assertEqual(self.run_fake(fake), "failed")
        fake.download_failure = False
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)

    def test_unknown_submit_persists_and_blocks_automatic_retry(self):
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        fake.submit_error = requests.Timeout()
        self.assertEqual(self.run_fake(fake), "submission_unknown")
        Engine(self.config, self.store).recover_jobs()
        self.assertEqual(self.run_fake(fake), "submission_unknown")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)
        Engine(self.config, self.store).resolve_unknown(self.job_id, task_id="offline-task")
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)

    def test_uncertain_language_requires_manual_review(self):
        fake = FakeService(result(("OK", "")))
        self.assertEqual(self.run_fake(fake), "needs_review")
        self.assertFalse(self.source.with_suffix(".srt").exists())
        cue = self.store.cues(self.job_id)[0]
        self.store.apply_review(self.job_id, cue["cue_id"], ReviewDecision("original"))
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.named("/chat/completions")), 0)

    def test_existing_srt_skips_before_any_cloud_request(self):
        self.source.with_suffix(".srt").write_bytes(b"existing")
        fake = FakeService(result(("你好", "zh")))
        self.assertEqual(self.run_fake(fake), "skipped")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.source.with_suffix(".srt").read_bytes(), b"existing")

    def test_cancelled_queue_job_does_not_start_cloud_calls(self):
        self.store.update(self.job_id, "cancelled")
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        self.assertEqual(self.run_fake(fake), "cancelled")
        self.assertEqual(fake.calls, [])

    def test_empty_recognition_does_not_invent_or_write_subtitles(self):
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertTrue(self.store.job(self.job_id)["manifest"]["no_speech"])
        self.assertFalse(self.source.with_suffix(".srt").exists())
        self.assertEqual(len(fake.named("/chat/completions")), 0)

    def test_output_conflict_protected(self):
        other = self.source.with_suffix(".mp4")
        shutil.copyfile(self.source, other)
        with self.assertRaises(ToolError) as error:
            self.store.enqueue(other, "beijing", workspace_id="offline-space")
        self.assertEqual(error.exception.code, "output_conflict")

    def test_modified_source_invalidates_cache(self):
        fake = FakeService(result(("First sentence.", "en")))
        fake.mt_failure.add("First sentence.")
        self.assertEqual(self.run_fake(fake), "failed")
        with self.source.open("ab") as source:
            source.write(b"changed")
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "source_changed")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)

    def test_account_checked_before_normalized_or_translation_cache_hit(self):
        fake = FakeService(result(("OK", "")))
        self.assertEqual(self.run_fake(fake), "needs_review")
        cue = self.store.cues(self.job_id)[0]
        self.store.apply_review(self.job_id, cue["cue_id"], ReviewDecision("original"))
        before = len(fake.calls)
        set_asr_account(self.config, "offline-other-account")
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "account_changed")
        self.assertEqual(len(fake.calls), before)
        self.assertFalse(self.source.with_suffix(".srt").exists())

    def test_translation_cache_cannot_cross_workspace_or_account(self):
        fake = FakeService(result(("First sentence.", "en")))
        self.assertEqual(self.run_fake(fake), "completed")
        for index, (workspace, key) in enumerate([("other-space", "offline-test-key-no-cloud-authorization"),
                                                ("offline-space", "offline-other-key")]):
            source = self.path / f"other-{index}.mkv"
            shutil.copyfile(self.fixture, source)
            self.config.settings.workspace_id = workspace
            set_asr_account(self.config, key)
            self.job_id = enqueue_current(self.store, self.config, source, workspace_id=workspace)
            self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.named("/chat/completions")), 9)


    def test_resume_cli_channel_option_changes_unsubmitted_input(self):
        run_process([ROOT / "tools/ffmpeg.exe", "-v", "error", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=440:duration=3:sample_rate=48000", "-ac", "2", "-c:a", "aac", self.source], threading.Event())
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        self.assertEqual(self.run_fake(fake), "needs_review")
        self.assertEqual(fake.calls, [])
        with mock.patch("subtitle_tool.main.Config", return_value=self.config), mock.patch("requests.request", side_effect=fake.request), mock.patch("requests.post", side_effect=fake.upload), mock.patch("sys.stdout", new_callable=io.StringIO) as output, mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["--resume", self.job_id, "--channel", "right", "--non-interactive"]), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "completed")
        self.assertEqual(self.store.job(self.job_id)["manifest"]["channel"], "right")
        self.assertEqual(self.store.job(self.job_id)["manifest"]["media_plan"]["method"], "flac")

    def test_input_change_clears_unsubmitted_audio_and_blocks_after_task_id(self):
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        with mock.patch("subtitle_tool.api.Bailian.submit", side_effect=ToolError("已拒绝", "denied")):
            self.assertEqual(self.run_fake(fake), "failed")
        cache = self.config.data / "cache" / self.job_id
        self.assertTrue(list(cache.glob("*.flac")))
        engine = Engine(self.config, self.store)
        engine.select_input(self.job_id, channel="left")
        self.assertEqual(self.store.job(self.job_id)["manifest"]["segments"], [])
        self.assertFalse(list(cache.glob("*.flac")))
        fake.query_failure = True
        self.assertEqual(self.run_fake(fake), "failed")
        with self.assertRaises(ToolError) as error:
            engine.select_input(self.job_id, channel="mix")
        self.assertEqual(error.exception.code, "input_committed")

    def test_cli_ctrl_c_saves_known_task_and_returns_130(self):
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        original = fake.request
        def interrupted(method, url, **kwargs):
            if url.endswith("/asr/transcription"):
                signal.raise_signal(signal.SIGINT)
            return original(method, url, **kwargs)
        with mock.patch("subtitle_tool.main.Config", return_value=self.config), mock.patch("requests.request", side_effect=interrupted), mock.patch("requests.post", side_effect=fake.upload), mock.patch("sys.stdout", new_callable=io.StringIO) as output, mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["--resume", self.job_id, "--non-interactive"]), 130)
            self.assertEqual(json.loads(output.getvalue())["state"], "cancelled")
        manifest = self.store.job(self.job_id)["manifest"]
        self.assertEqual(manifest["segments"][0]["task_id"], "offline-task")
        Engine(self.config, self.store).recover_jobs()
        self.assertEqual(self.store.job(self.job_id)["state"], "cancelled")

    def test_keyboard_interrupt_during_post_keeps_unknown_submission(self):
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        fake.submit_error = KeyboardInterrupt()
        self.assertEqual(self.run_fake(fake), "submission_unknown")
        self.assertTrue(self.store.job(self.job_id)["manifest"]["segments"][0]["submitting"])
        self.assertEqual(self.run_fake(fake), "submission_unknown")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)

    def test_output_conflict_after_other_extension_already_completed(self):
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        self.assertEqual(self.run_fake(fake), "completed")
        other = self.source.with_suffix(".mp4")
        shutil.copyfile(self.source, other)
        original = self.source.with_suffix(".srt").read_bytes()
        with self.assertRaises(ToolError) as error:
            self.store.enqueue(other, "beijing", overwrite=True, workspace_id="offline-space")
        self.assertEqual(error.exception.code, "output_conflict")
        self.assertEqual(self.source.with_suffix(".srt").read_bytes(), original)

    def test_missing_fallback_audio_rebuild_retains_scheme_and_samples(self):
        run_process([ROOT / "tools/ffmpeg.exe", "-v", "error", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=440:duration=3:sample_rate=48000", "-c:a", "aac", self.source], threading.Event())
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        with mock.patch.object(Media, "copy_audio", side_effect=ToolError("不相容", "media")), mock.patch("subtitle_tool.api.Bailian.submit", side_effect=ToolError("已拒绝", "denied")):
            self.assertEqual(self.run_fake(fake), "failed")
        manifest = self.store.job(self.job_id)["manifest"]
        original_plan = manifest["media_plan"]
        self.assertEqual(original_plan["method"], "flac")
        manifest["segments"][0]["upload_expires"] = 0
        Engine.cleanup_audio(self.config.data / "cache" / self.job_id)
        self.store.update(self.job_id, manifest=manifest)
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(self.store.job(self.job_id)["manifest"]["media_plan"], original_plan)
        self.assertEqual(len(fake.uploads), 2)

    def test_lower_upload_policy_replans_before_any_asr_submission(self):
        run_process([ROOT / "tools/ffmpeg.exe", "-v", "error", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=440:duration=20:sample_rate=48000", "-c:a", "aac", self.source], threading.Event())
        fake = FakeService(result(("欢迎使用百炼。", "zh")))
        original = fake.request
        policies = [0]
        def limited(method, url, **kwargs):
            reply = original(method, url, **kwargs)
            if url.endswith("/uploads"):
                policies[0] += 1
                body = reply.json()
                body["data"]["max_file_size_mb"] = 1000 if policies[0] == 1 else 0.05
                return response(body)
            return reply
        fake.request = limited
        self.assertEqual(self.run_fake(fake), "completed")
        manifest = self.store.job(self.job_id)["manifest"]
        self.assertEqual(manifest["media_plan"]["method"], "flac")
        self.assertGreater(len(manifest["segments"]), 1)
        self.assertTrue(all(s["bytes"] <= 50000 for s in manifest["segments"]))
        self.assertEqual(len(fake.named("/asr/transcription")), len(manifest["segments"]))


if __name__ == "__main__":
    unittest.main()
