from __future__ import annotations

import copy
from unittest import mock

from subtitle_tool.engine import Engine
from subtitle_tool.media import Media
from subtitle_tool.common import atomic_json
from tests.fixtures import FakeService, SavedResultFixture, result, timed_raw


class SavedRecoveryTests(SavedResultFixture):
    def assert_mismatch_without_requests(self, fake=None):
        fake = fake or FakeService(result())
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "cache_mismatch")
        self.assertEqual(fake.calls, [])
        self.assertFalse(self.source.with_suffix(".srt").exists())

    def test_normalized_result_needs_neither_source_nor_raw_nor_media_tools(self):
        self.save_result(result(("保存的字幕。", "zh")))
        before_sources, before_cues = self.store.sources(self.job_id), self.store.cues(self.job_id)
        self.source.unlink()
        (self.cache / "raw-0000.json").unlink()
        (self.cache / "subtitle-sources-v2.json").unlink()
        fake = FakeService(result())
        with mock.patch.object(Media, "probe", side_effect=AssertionError("no probe")), \
                mock.patch("subtitle_tool.engine.fingerprint", side_effect=AssertionError("no source read")):
            self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(self.store.sources(self.job_id), before_sources)
        self.assertEqual(self.store.cues(self.job_id), before_cues)
        self.assertEqual(fake.calls, [])

    def test_raw_and_known_task_resume_without_media_or_asr_resubmit(self):
        for known_id in [False, True]:
            with self.subTest(known_id=known_id):
                self.source.write_bytes(b"saved source identity only")
                self.store.replace_cues(self.job_id, [])
                self.save_result(result(("本地转写。", "zh")), normalized=False)
                self.store.update(self.job_id, "queued")
                if known_id:
                    (self.cache / "raw-0000.json").unlink()
                else:
                    manifest = self.store.job(self.job_id)["manifest"]
                    manifest["segments"][0].pop("task_id")
                    self.store.update(self.job_id, manifest=manifest)
                self.source.unlink()
                fake = FakeService(result(("本地转写。", "zh")))
                with mock.patch.object(Media, "probe", side_effect=AssertionError("no probe")):
                    self.assertEqual(self.run_fake(fake), "completed")
                self.assertEqual(fake.named("/asr/transcription"), [])
                self.assertEqual(bool(fake.named("/tasks/offline-task")), known_id)
                self.source.with_suffix(".srt").unlink()

    def test_count_missing_source_and_fragment_ranges_fail_before_mt(self):
        sources = self.save_result(timed_raw(["first", "second"]))
        manifest = self.store.job(self.job_id)["manifest"]
        original_cues = self.store.cues(self.job_id)
        bad_cue = {**original_cues[0], "char_end": 1}
        for kind in ["count", "missing_source", "range", "word_text", "duplicate_source", "reference"]:
            with self.subTest(kind=kind):
                candidate = copy.deepcopy(manifest)
                records = copy.deepcopy(sources)
                cues = copy.deepcopy(original_cues)
                if kind == "count":
                    candidate["normalized_count"] = 2
                elif kind == "missing_source":
                    records = []
                elif kind == "range":
                    cues = [bad_cue]
                elif kind == "word_text":
                    records[0]["words"][0]["char_end"] = 1
                elif kind == "duplicate_source":
                    candidate["segments"].append(copy.deepcopy(candidate["segments"][0]))
                elif kind == "reference":
                    candidate["references_planned"] = True
                self.store.initialize_subtitles(self.job_id, records, cues, candidate)
                self.store.update(self.job_id, "queued")
                self.assert_mismatch_without_requests()

    def test_legacy_count_proof_uses_saved_file_or_raw_but_never_rebuilds_committed_cues(self):
        self.save_result(result(("保留原文。", "zh")))
        manifest = self.store.job(self.job_id)["manifest"]
        manifest.pop("normalized_count")
        self.store.update(self.job_id, manifest=manifest)
        self.source.unlink()
        self.assertEqual(self.run_fake(FakeService(result())), "completed")
        self.source.with_suffix(".srt").unlink()
        self.store.update(self.job_id, "queued")
        (self.cache / manifest["normalized_file"]).unlink()
        self.assertEqual(self.run_fake(FakeService(result())), "completed")
        self.source.with_suffix(".srt").unlink()
        self.store.update(self.job_id, "queued")
        (self.cache / "raw-0000.json").unlink()
        self.assert_mismatch_without_requests()

    def test_empty_result_needs_independent_proof_and_contradictions_fail(self):
        self.save_result(result())
        manifest = self.store.job(self.job_id)["manifest"]
        manifest.pop("normalized_count")
        self.store.update(self.job_id, manifest=manifest)
        atomic_json(self.cache / manifest["normalized_file"], [{"unexpected": "record"}])
        self.assert_mismatch_without_requests()

    def test_changed_original_requires_explicit_output_directory(self):
        self.save_result(result(("原媒体字幕。", "zh")))
        self.source.write_bytes(b"different media")
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), "failed")
        job = self.store.job(self.job_id)
        self.assertEqual(job["error_code"], "source_changed")
        self.assertIn("--output-dir", job["error"])
        Engine(self.config, self.store).resume(self.job_id, output_dir=self.path / "elsewhere")
        with mock.patch("subtitle_tool.engine.fingerprint", side_effect=AssertionError("no source read")):
            self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(fake.calls, [])

    def test_key_change_still_blocks_pure_local_recovery(self):
        self.save_result(result(("保留原文。", "zh")))
        Engine(self.config, self.store).identity(self.job_id, self.store.job(self.job_id)["manifest"])
        self.source.unlink()
        self.config.set_key("offline-different-key")
        self.assertEqual(self.run_fake(FakeService(result())), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "account_changed")

    def test_source_is_rechecked_immediately_before_publish(self):
        self.save_result(result(("保留原文。", "zh")))
        def progress(job_id, stage, *args):
            if stage == "writing":
                self.source.write_bytes(b"changed while processing")
        with mock.patch("requests.request", side_effect=AssertionError("local only")):
            self.assertEqual(Engine(self.config, self.store, progress=progress).run(self.job_id), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "source_changed")
        self.assertFalse(self.source.with_suffix(".srt").exists())

    def test_missing_committed_raw_without_task_id_never_restarts_asr(self):
        self.save_result(normalized=False)
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["segments"][0].pop("task_id")
        self.store.update(self.job_id, manifest=manifest)
        (self.cache / "raw-0000.json").unlink()
        self.assert_mismatch_without_requests()
