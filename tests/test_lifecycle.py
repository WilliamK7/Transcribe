from __future__ import annotations

import time
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from tests.fixtures import FakeService, SavedResultFixture, result


class LifecycleTests(SavedResultFixture):
    def unknown(self, *, legacy=False):
        self.save_result(normalized=False)
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["segments"][0].pop("task_id")
        manifest["segments"][0].pop("raw")
        if not legacy:
            manifest["segments"][0]["submitting"] = True
        self.store.update(self.job_id, "submitting", manifest)
        return Engine(self.config, self.store)

    def test_startup_and_explicit_resume_never_resend_unknown_submission(self):
        for legacy in [False, True]:
            with self.subTest(legacy=legacy):
                engine = self.unknown(legacy=legacy)
                engine.recover_jobs()
                engine.resume(self.job_id)
                fake = FakeService(result())
                self.assertEqual(self.run_fake(fake), "submission_unknown")
                self.assertEqual(fake.calls, [])

    def test_cancelled_legacy_unknown_can_be_bound_only_on_explicit_resume(self):
        engine = self.unknown(legacy=True)
        engine.cancel_job(self.job_id)
        self.assertEqual(self.store.job(self.job_id)["state"], "cancelled")
        engine.recover_jobs()
        self.assertEqual(self.store.job(self.job_id)["state"], "cancelled")
        engine.resume(self.job_id)
        self.assertEqual(self.store.job(self.job_id)["state"], "submission_unknown")
        engine.resolve_unknown(self.job_id, task_id="offline-bound")
        manifest = self.store.job(self.job_id)["manifest"]
        self.assertEqual(manifest["segments"][0]["task_id"], "offline-bound")
        self.assertFalse(manifest["segments"][0].get("submitting"))

    def test_legacy_resubmit_is_explicit_and_ambiguous_multiple_segments_stay_blocked(self):
        engine = self.unknown(legacy=True)
        engine.recover_jobs()
        with self.assertRaises(ToolError):
            engine.resolve_unknown(self.job_id)
        engine.resolve_unknown(self.job_id, allow_resubmit=True)
        self.assertEqual(self.store.job(self.job_id)["state"], "queued")
        engine = self.unknown(legacy=True)
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["segments"].append({**manifest["segments"][0], "id": "0001"})
        self.store.update(self.job_id, "submitting", manifest)
        engine.recover_jobs()
        with self.assertRaises(ToolError):
            engine.resolve_unknown(self.job_id, allow_resubmit=True)

    def test_late_error_preserves_new_cloud_task_id_over_old_database_copy(self):
        engine = self.unknown()
        manifest = self.store.job(self.job_id)["manifest"]
        # The newly returned cloud fact must survive the failing first persistence call.
        manifest["segments"][0].update(audio="audio.flac", submitting=False, oss_url="oss://offline/audio.flac",
                                       upload_expires=time.time() + 3600)
        self.store.update(self.job_id, "queued", manifest)
        update = self.store.update
        injected = []
        def fail_once(job_id, state=None, *args, **kwargs):
            if state == "recognizing" and not injected:
                injected.append(True)
                raise OSError()
            return update(job_id, state, *args, **kwargs)
        fake = FakeService(result(("保存。", "zh")))
        (self.cache / "raw-0000.json").unlink()
        with mock.patch.object(self.store, "update", side_effect=fail_once):
            self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["manifest"]["segments"][0]["task_id"], "offline-task")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)
