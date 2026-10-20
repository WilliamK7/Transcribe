from __future__ import annotations

import copy
from pathlib import Path
from unittest import mock

import subtitle_tool.engine as engine_module
from subtitle_tool.engine import Engine
from subtitle_tool.policy import load_policy
from subtitle_tool.subtitles import render_srt
from tests.fixtures import FakeService, SavedResultFixture, result


class PublicationTests(SavedResultFixture):
    def prepare(self, *, overwrite=True):
        self.save_result(result(("发布的字幕。", "zh")))
        self.output = self.source.with_suffix(".srt")
        if overwrite:
            self.output.write_bytes(b"previous subtitles")
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["overwrite"] = overwrite
        self.store.update(self.job_id, "queued", manifest)
        return FakeService(result())

    def test_replay_after_write_never_makes_another_backup_even_with_overwrite(self):
        for overwrite in [True, False]:
            with self.subTest(overwrite=overwrite):
                if self.source.with_suffix(".srt").exists():
                    self.source.with_suffix(".srt").unlink()
                fake = self.prepare(overwrite=overwrite)
                before = set(self.path.glob("*.bak.*"))
                write = engine_module.write_srt
                def interrupted(*args, **kwargs):
                    write(*args, **kwargs)
                    raise OSError()
                with mock.patch.object(engine_module, "write_srt", side_effect=interrupted):
                    self.assertEqual(self.run_fake(fake), "failed")
                content = self.output.read_bytes()
                backups = set(self.path.glob("*.bak.*")) - before
                self.assertEqual(len(backups), int(overwrite))
                Engine(self.config, self.store).resume(self.job_id, overwrite=True)
                with mock.patch.object(engine_module, "write_srt", side_effect=AssertionError("already published")):
                    self.assertEqual(self.run_fake(fake), "completed")
                self.assertEqual(self.output.read_bytes(), content)
                self.assertEqual(set(self.path.glob("*.bak.*")) - before, backups)
                self.assertEqual(fake.calls, [])

    def test_interruptions_at_intent_backup_replace_and_completion_reuse_intent(self):
        import subtitle_tool.publication as publication
        for boundary in ["intent", "backup", "replace", "completion_before", "completion_after"]:
            for fault in [OSError, KeyboardInterrupt]:
                with self.subTest(boundary=boundary, fault=fault.__name__):
                    self.source = self.path / (boundary + fault.__name__ + ".mkv")
                    self.source.write_bytes(b"test source")
                    self.job_id = self.store.enqueue(self.source, workspace_id="offline-space")
                    self.cache = self.config.data / "cache" / self.job_id
                    self.cache.mkdir(parents=True)
                    fake = self.prepare()
                    update, backup, replace = self.store.update, publication.backup_original, publication.os.replace
                    injected = []
                    def save(job_id, state=None, manifest=None, *args, **kwargs):
                        target = (boundary == "intent" and state is None and manifest and manifest.get("publication_intent") or
                                  boundary.startswith("completion") and state == "completed")
                        if target and not injected:
                            injected.append(True)
                            if boundary != "completion_before":
                                update(job_id, state, manifest, *args, **kwargs)
                            raise fault()
                        return update(job_id, state, manifest, *args, **kwargs)
                    def back_up(*args, **kwargs):
                        value = backup(*args, **kwargs)
                        if boundary == "backup" and not injected:
                            injected.append(True)
                            raise fault()
                        return value
                    def replace_file(source, target):
                        value = replace(source, target)
                        if boundary == "replace" and Path(target) == self.output and not injected:
                            injected.append(True)
                            raise fault()
                        return value
                    with mock.patch.object(self.store, "update", side_effect=save), \
                            mock.patch.object(publication, "backup_original", side_effect=back_up), \
                            mock.patch.object(publication.os, "replace", side_effect=replace_file):
                        self.run_fake(fake)
                    self.assertTrue(injected)
                    intent = copy.deepcopy(self.store.job(self.job_id)["manifest"]["publication_intent"])
                    Engine(self.config, self.store).resume(self.job_id, overwrite=True)
                    self.assertEqual(self.run_fake(fake), "completed")
                    self.assertEqual(self.store.job(self.job_id)["manifest"]["publication_intent"], intent)
                    backups = list(self.output.parent.glob(self.output.name + ".bak.*"))
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(backups[0].read_bytes(), b"previous subtitles")
                    self.assertEqual(fake.calls, [])

    def test_external_target_or_backup_change_is_never_overwritten(self):
        import subtitle_tool.publication as publication
        fake = self.prepare()
        backup = publication.backup_original
        def interrupted(*args, **kwargs):
            backup(*args, **kwargs)
            raise OSError()
        with mock.patch.object(publication, "backup_original", side_effect=interrupted):
            self.assertEqual(self.run_fake(fake), "failed")
        intent = self.store.job(self.job_id)["manifest"]["publication_intent"]
        self.output.write_bytes(b"external edit")
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "output_conflict")
        self.assertEqual(self.output.read_bytes(), b"external edit")
        self.output.write_bytes(b"previous subtitles")
        Path(intent["backup"]).write_bytes(b"external backup edit")
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "output_conflict")
        self.assertEqual(Path(intent["backup"]).read_bytes(), b"external backup edit")
        self.assertEqual(self.output.read_bytes(), b"previous subtitles")

    def test_output_change_and_content_change_clear_intent_atomically(self):
        self.prepare()
        manifest = self.store.job(self.job_id)["manifest"]
        manifest.update(expected_srt_sha256="old", publication_intent={"old": True})
        self.store.update(self.job_id, manifest=manifest)
        self.store.change_output(self.job_id, overwrite=True)
        self.assertIn("publication_intent", self.store.job(self.job_id)["manifest"])
        self.store.change_output(self.job_id, self.path / "other")
        self.assertNotIn("publication_intent", self.store.job(self.job_id)["manifest"])
        self.assertNotIn("expected_srt_sha256", self.store.job(self.job_id)["manifest"])
        self.store.update(self.job_id, manifest=manifest)
        self.store.save_layout(self.job_id, self.store.cues(self.job_id), manifest)
        self.assertNotIn("publication_intent", self.store.job(self.job_id)["manifest"])

    def test_partial_backup_is_never_visible_and_retry_preserves_original(self):
        import subtitle_tool.publication as publication
        fake = self.prepare()
        def partial(source, target):
            target.write(source.read(4))
            raise OSError()
        with mock.patch.object(publication.shutil, "copyfileobj", side_effect=partial):
            self.assertEqual(self.run_fake(fake), "failed")
        intent = self.store.job(self.job_id)["manifest"]["publication_intent"]
        self.assertFalse(Path(intent["backup"]).exists())
        self.assertEqual(self.output.read_bytes(), b"previous subtitles")
        self.assertEqual(list(self.path.glob("*.tmp")), [])
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(Path(intent["backup"]).read_bytes(), b"previous subtitles")

    def test_pending_revision_mismatch_blocks_write_but_published_intent_can_finish(self):
        import subtitle_tool.publication as publication
        fake = self.prepare()
        with mock.patch.object(engine_module, "write_srt", side_effect=OSError()):
            self.assertEqual(self.run_fake(fake), "failed")
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["execution_snapshot"]["prompt_version"] = 999
        self.store.update(self.job_id, manifest=manifest)
        with mock.patch.object(engine_module, "write_srt", side_effect=AssertionError("no republish")):
            self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "cache_version")
        content = render_srt(self.store.cues(self.job_id),
                             load_policy(manifest, verify_execution=False).settings(self.config.settings))
        publication.write_srt(self.output, content, intent=manifest["publication_intent"])
        manifest["subtitle_version"] = 999
        self.store.update(self.job_id, manifest=manifest)
        with mock.patch.object(engine_module, "write_srt", side_effect=AssertionError("already published")):
            self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(fake.calls, [])

    def test_legacy_hash_recovers_overwrite_without_guessing_backup_ownership(self):
        import hashlib
        fake = self.prepare()
        content = b"legacy published result"
        self.output.write_bytes(content)
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["expected_srt_sha256"] = hashlib.sha256(content).hexdigest()
        manifest["execution_snapshot"] = {"format_version": 999}
        self.store.update(self.job_id, "writing", manifest)
        Engine(self.config, self.store).resume(self.job_id, overwrite=True)
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertNotIn("backup", self.store.job(self.job_id)["manifest"])
        self.assertEqual(list(self.path.glob("*.bak.*")), [])
        self.assertEqual(fake.calls, [])

    def test_failed_content_transaction_preserves_original_intent_and_records(self):
        self.prepare()
        manifest = self.store.job(self.job_id)["manifest"]
        manifest.update(expected_srt_sha256="old", publication_intent={"old": True})
        self.store.update(self.job_id, manifest=manifest)
        before = self.store.job(self.job_id)
        cues = self.store.cues(self.job_id)
        with mock.patch.object(self.store, "_replace_records", side_effect=OSError()), self.assertRaises(OSError):
            self.store.save_layout(self.job_id, cues, copy.deepcopy(manifest))
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual(self.store.cues(self.job_id), cues)
