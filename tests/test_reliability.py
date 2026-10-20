from __future__ import annotations

import copy
import sqlite3
from unittest import mock

from subtitle_tool.api import _MT_PACERS
from subtitle_tool.common import ToolError
from tests.fixtures import FakeService, SavedResultFixture, response, result, timed_raw


class TransactionRecoveryTests(SavedResultFixture):
    def test_local_stage_interruptions_follow_committed_records(self):
        for boundary in ["initialize", "references", "layout", "append"]:
            for point in ["before", "inside", "after"]:
                for fault in [KeyboardInterrupt, OSError]:
                    with self.subTest(boundary=boundary, point=point, fault=fault.__name__):
                        _MT_PACERS.clear()
                        self.job_id = self.store.enqueue(self.source, workspace_id="offline-space",
                            output_dir=self.path / (boundary + point + fault.__name__))
                        self.cache = self.config.data / "cache" / self.job_id
                        self.cache.mkdir(parents=True)
                        manifest = self.store.job(self.job_id)["manifest"]
                        manifest['translation_strategy_version'] = 2
                        manifest["subtitle_settings"].update(zh_line_width=22, other_line_width=42)
                        self.store.update(self.job_id, manifest=manifest)
                        raw = timed_raw(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"], word_ms=1000)
                        self.save_result(raw, normalized=False)
                        fake = FakeService(raw)
                        original_request = fake.request
                        def request(method, url, **kwargs):
                            if url.endswith("/chat/completions"):
                                fake.calls.append((method, url, kwargs))
                                body = kwargs["json"]
                                return response({"choices": [{"finish_reason": "stop", "message": {
                                    "content": "中" * (50 if body["translation_options"].get("tm_list") else 10)}}]})
                            return original_request(method, url, **kwargs)
                        fake.request = request
                        method = "initialize_subtitles" if boundary == "initialize" else "save_layout"
                        save = getattr(self.store, method)
                        expected, injected = {}, []
                        def interrupt(job_id, *args, **kwargs):
                            candidate = args[-1]
                            target = (boundary == "initialize" or
                                      boundary == "references" and kwargs.get("references") is not None or
                                      boundary == "layout" and candidate.get("layout_planned") and candidate.get("layout_round") == 0 or
                                      boundary == "append" and candidate.get("layout_round") == 1)
                            if not target or injected:
                                return save(job_id, *args, **kwargs)
                            injected.append(True)
                            def snapshot():
                                return (copy.deepcopy(self.store.job(job_id)["manifest"]),
                                        self.store.sources(job_id), self.store.cues(job_id), self.store.references(job_id))
                            expected["saved"] = snapshot()
                            if point == "after":
                                save(job_id, *args, **kwargs)
                                expected["saved"] = snapshot()
                            elif point == "inside":
                                replace = self.store._replace_records
                                def broken(*values, **options):
                                    replace(*values, **options)
                                    raise fault()
                                with mock.patch.object(self.store, "_replace_records", side_effect=broken):
                                    return save(job_id, *args, **kwargs)
                            raise fault()
                        with mock.patch.object(self.store, method, side_effect=interrupt):
                            self.assertEqual(self.run_fake(fake), "cancelled" if fault is KeyboardInterrupt else "failed")
                        self.assertTrue(injected)
                        saved, sources, cues, references = expected["saved"]
                        actual = self.store.job(self.job_id)["manifest"]
                        for flag in ["normalized", "normalized_count", "references_planned", "layout_planned", "layout_round"]:
                            self.assertEqual(actual.get(flag), saved.get(flag), flag)
                        self.assertEqual(self.store.sources(self.job_id), sources)
                        self.assertEqual(self.store.cues(self.job_id), cues)
                        self.assertEqual(self.store.references(self.job_id), references)
                        self.store.update(self.job_id, "queued")
                        self.assertEqual(self.run_fake(fake), "completed")
                        self.assertFalse(self.store.job(self.job_id)["manifest"].get("no_speech"))
                        self.assertEqual(len(fake.named("/asr/transcription")), 0)
                        # Keep each scenario independent of previous translations.
                        with self.store.db:
                            self.store.db.execute("DELETE FROM mt_cache")

    def test_legacy_initialization_is_atomic(self):
        self.save_result(result(("完整字幕。", "zh")), normalized=False)
        manifest = self.store.job(self.job_id)["manifest"]
        manifest.pop("subtitle_version")
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(result(("完整字幕。", "zh")))
        replace = self.store._replace_records
        def broken(*args):
            replace(*args)
            raise KeyboardInterrupt()
        with mock.patch.object(self.store, "_replace_records", side_effect=broken):
            self.assertEqual(self.run_fake(fake), "cancelled")
        self.assertFalse(self.store.job(self.job_id)["manifest"].get("normalized"))
        self.assertEqual(self.store.cues(self.job_id), [])
        self.store.update(self.job_id, "queued")
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.named("/asr/transcription")), 0)

    def test_empty_cache_needs_normalization_evidence(self):
        fake = FakeService(result(("完整字幕。", "zh")))
        self.save_result(fake.raw, normalized=False)
        self.assertEqual(self.run_fake(fake), "completed")
        self.source.with_suffix(".srt").unlink()
        manifest = self.store.job(self.job_id)["manifest"]
        manifest.pop("normalized_count", None)
        self.store.update(self.job_id, "queued", manifest)
        self.store.replace_cues(self.job_id, [])
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "cache_mismatch")
        self.assertEqual(len(fake.named("/asr/transcription")), 0)

    def test_legacy_empty_cache_can_use_complete_raw_but_not_missing_evidence(self):
        fake = FakeService(result())
        self.save_result(fake.raw, normalized=False)
        self.assertEqual(self.run_fake(fake), "completed")
        cache = self.config.data / "cache" / self.job_id
        manifest = self.store.job(self.job_id)["manifest"]
        manifest.pop("normalized_count", None)
        (cache / manifest["normalized_file"]).unlink()
        self.store.update(self.job_id, "queued", manifest)
        self.assertEqual(self.run_fake(fake), "completed")
        (cache / "raw-0000.json").unlink()
        self.store.update(self.job_id, "queued")
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "cache_mismatch")
        self.assertEqual(len(fake.named("/asr/transcription")), 0)

    def test_accounting_failure_does_not_replace_primary_failure(self):
        fake = FakeService(result())
        with mock.patch.object(self.store, "ensure_output", side_effect=RuntimeError("primary")), \
                mock.patch.object(self.store, "count", side_effect=sqlite3.OperationalError("accounting")):
            with self.assertRaisesRegex(RuntimeError, "primary"):
                self.run_fake(fake)

    def test_unreadable_commit_state_never_saves_candidate(self):
        fake = FakeService(result(("完整字幕。", "zh")))
        self.save_result(fake.raw, normalized=False)
        original_job = self.store.job
        broken = []
        def job(job_id):
            if broken:
                raise sqlite3.OperationalError("unavailable")
            return original_job(job_id)
        def initialize(*args):
            broken.append(True)
            raise OSError("commit failed")
        with mock.patch.object(self.store, "initialize_subtitles", side_effect=initialize), \
                mock.patch.object(self.store, "job", side_effect=job), \
                mock.patch.object(self.store, "update", wraps=self.store.update) as update:
            with self.assertRaises(ToolError):
                self.run_fake(fake)
            self.assertFalse(any(call.kwargs.get("manifest", {}).get("normalized") for call in update.call_args_list))
        self.assertFalse(self.store.job(self.job_id)["manifest"].get("normalized"))
