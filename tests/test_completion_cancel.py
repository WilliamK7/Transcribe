from __future__ import annotations

import threading
from pathlib import Path
from unittest import mock

from subtitle_tool.api import Bailian, Http
from subtitle_tool.common import Cancelled
from subtitle_tool.engine import Engine
from tests.fixtures import FakeService, PipelineFixture, TemporaryTest, response, result


class CompletionTests(PipelineFixture):
    def test_real_windows_file_lock_preserves_completion_and_single_backup(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        output = self.source.with_suffix(".srt")
        output.write_bytes(b"previous subtitles")
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["overwrite"] = True
        self.store.update(self.job_id, manifest=manifest)
        cleanup = Engine.cleanup_completed_audio
        def locked_cleanup(engine, job_id, manifest, cache):
            audio = next(cache.glob("audio.flac"))
            handle = kernel.CreateFileW(str(audio), 0x80000000, 3, None, 3, 128, None)
            self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
            try:
                cleanup(engine, job_id, manifest, cache)
                self.assertTrue(audio.exists())
            finally:
                kernel.CloseHandle(handle)
        fake = FakeService(result(("完整字幕。", "zh")))
        with mock.patch.object(Engine, "cleanup_completed_audio", new=locked_cleanup):
            self.assertEqual(self.run_fake(fake), "completed")
        self.assertTrue(self.store.job(self.job_id)["manifest"]["warnings"])
        self.assertEqual(len(list(self.path.glob("*.bak.*"))), 1)
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(list(self.path.glob("*.bak.*"))), 1)
        self.assertEqual(len(fake.named("/asr/transcription")), 1)

    def test_locked_audio_keeps_completed_state_and_safe_warning(self):
        fake = FakeService(result(("完整字幕。", "zh")))
        unlink = Path.unlink
        def locked(path, *args, **kwargs):
            if path.suffix in {".wav", ".flac"} and path.stem == "audio":
                raise PermissionError("private path and raw error")
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", new=locked):
            self.assertEqual(self.run_fake(fake), "completed")
        job = self.store.job(self.job_id)
        self.assertTrue(job["manifest"]["warnings"])
        self.assertNotIn("private path", str(job))
        content = self.source.with_suffix(".srt").read_bytes()
        calls = len(fake.calls)
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.calls), calls)
        self.assertEqual(self.source.with_suffix(".srt").read_bytes(), content)
        self.assertEqual(list(self.path.glob("*.bak.*")), [])

    def test_interrupt_or_error_after_completed_commit_cannot_downgrade(self):
        for fault in [KeyboardInterrupt, OSError]:
            with self.subTest(fault=fault.__name__):
                self.job_id = self.store.enqueue(self.source, workspace_id="offline-space", output_dir=self.path / fault.__name__)
                def progress(job_id, name, *args):
                    if name == "completed":
                        raise fault()
                fake = FakeService(result(("完整字幕。", "zh")))
                with mock.patch("requests.request", side_effect=fake.request), mock.patch("requests.post", side_effect=fake.upload):
                    engine = Engine(self.config, self.store, progress=progress)
                    try:
                        engine.run(self.job_id)
                    except (KeyboardInterrupt, OSError):
                        pass
                self.assertEqual(self.store.job(self.job_id)["state"], "completed")

    def test_cancel_after_mt_attempt_is_persisted_does_not_start_translation(self):
        fake = FakeService(result(("First sentence.", "en")))
        cancel = threading.Event()
        update = self.store.translation_update
        def persist(*args, **kwargs):
            update(*args, **kwargs)
            if kwargs.get("attempt"):
                cancel.set()
        with mock.patch.object(self.store, "translation_update", side_effect=persist), \
                mock.patch("requests.request", side_effect=fake.request), mock.patch("requests.post", side_effect=fake.upload):
            self.assertEqual(Engine(self.config, self.store, cancel).run(self.job_id), "cancelled")
        self.assertEqual(fake.named("/chat/completions"), [])
        self.assertEqual(self.store.references(self.job_id)[0]["attempts"], 1)

    def test_cancel_after_registered_asr_attempt_preserves_unknown_marker(self):
        cancel = threading.Event()
        count = self.store.count
        def registered(job_id, name, *args):
            count(job_id, name, *args)
            if name == "asr_submit":
                cancel.set()
        fake = FakeService(result())
        with mock.patch.object(self.store, "count", side_effect=registered), \
                mock.patch("requests.request", side_effect=fake.request), mock.patch("requests.post", side_effect=fake.upload):
            self.assertEqual(Engine(self.config, self.store, cancel).run(self.job_id), "submission_unknown")
        self.assertEqual(fake.named("/asr/transcription"), [])
        self.assertTrue(self.store.job(self.job_id)["manifest"]["segments"][0]["submitting"])
        self.assertEqual(self.run_fake(fake), "submission_unknown")


class CancelBoundaryTests(TemporaryTest):
    def test_cancel_between_registered_http_attempt_and_send(self):
        for name in ["mt", "asr_submit", "http"]:
            with self.subTest(name=name):
                cancel, registered, release = threading.Event(), threading.Event(), threading.Event()
                def count(_):
                    registered.set()
                    self.assertTrue(release.wait(5))
                errors = []
                def request():
                    try:
                        Http(cancel, count=count).json("POST", "https://offline.invalid", name=name)
                    except BaseException as error:
                        errors.append(error)
                with mock.patch("requests.request", return_value=response({})) as send:
                    worker = threading.Thread(target=request)
                    worker.start()
                    self.assertTrue(registered.wait(5))
                    cancel.set()
                    release.set()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                    send.assert_not_called()
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], Cancelled)

    def test_cancel_after_upload_preparation_or_count_does_not_post(self):
        audio = self.path / "audio.flac"
        audio.write_bytes(b"fake audio")
        policy = {"policy": "offline", "signature": "offline", "upload_dir": "offline",
                  "upload_host": "https://offline.invalid", "oss_access_key_id": "offline",
                  "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true", "max_file_size_mb": 1000}
        for boundary in ["progress", "count"]:
            cancel = threading.Event()
            cloud = Bailian("offline-key", "beijing", cancel)
            if boundary == "progress":
                cloud.progress = lambda *args: cancel.set()
            else:
                cloud.http.count = lambda *args: cancel.set()
            with mock.patch.object(cloud, "policy", return_value=policy), mock.patch("requests.post", return_value=response({})) as send:
                with self.assertRaises(Cancelled):
                    cloud.upload(audio)
                send.assert_not_called()
