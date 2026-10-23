from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
from unittest import mock

import subtitle_tool.engine as engine_module
import subtitle_tool.publication as publication
from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.main import main, summary
from subtitle_tool.policy import load_policy
from subtitle_tool.subtitles import render_srt
from tests.fixtures import FakeService, SavedResultFixture, overlapping_raw, srt_intervals


class RetimeTests(SavedResultFixture):
    def prepare_legacy(self, *, overlap=40):
        self.save_result(overlapping_raw(overlap=overlap))
        manifest = self.store.job(self.job_id)["manifest"]
        manifest.pop("display_timing_version")
        self.store.update(self.job_id, manifest=manifest)
        self.fake = FakeService(overlapping_raw(overlap=overlap))
        self.assertEqual(self.run_fake(self.fake), "completed")
        self.output = self.source.with_suffix(".srt")
        self.old_content = self.output.read_bytes()
        self.fake.calls.clear()
        self.before = self.store.job(self.job_id)
        self.sources, self.cues, self.references = (self.store.sources(self.job_id), self.store.cues(self.job_id),
                                                   self.store.references(self.job_id))

    def assert_evidence_unchanged(self):
        self.assertEqual(self.store.sources(self.job_id), self.sources)
        self.assertEqual(self.store.cues(self.job_id), self.cues)
        self.assertEqual(self.store.references(self.job_id), self.references)

    def test_retime_publishes_saved_results_with_original_layout(self):
        self.prepare_legacy()
        counts = self.store.counts(self.job_id)
        self.source.unlink()
        for path in self.cache.glob("raw-*.json"):
            path.unlink()
        self.config.settings.zh_line_width = 12
        self.config.settings.other_line_width = 24
        self.config.settings.chinese_first = True
        engine = Engine(self.config, self.store)
        engine.retime(self.job_id, overwrite=True)
        prepared = self.store.job(self.job_id)
        manifest = prepared["manifest"]
        self.assertEqual(prepared["state"], "queued")
        self.assertEqual(self.output.read_bytes(), self.old_content)
        self.assertEqual(manifest["display_timing_version"], 2)
        self.assertEqual(manifest["subtitle_settings"], self.before["manifest"]["subtitle_settings"])
        self.assertEqual(manifest["execution_snapshot"], self.before["manifest"]["execution_snapshot"])
        self.assertEqual(manifest["contract"], self.before["manifest"]["contract"])
        self.assertEqual(manifest["display_timing_adjustments"][0]["adjustment_ms"], 40)
        snapshot = json.loads((self.config.root / manifest["retime"]["snapshot"]).read_text(encoding="utf-8"))
        self.assertEqual(snapshot["job"], self.before)
        self.assert_evidence_unchanged()
        self.assertEqual(self.run_fake(self.fake), "completed")
        content = self.output.read_bytes()
        self.assertEqual(content, self.old_content.replace(b"00:00:05,000", b"00:00:04,960", 1))
        self.assertEqual(srt_intervals(content), [(1000, 4960), (4960, 8320)])
        self.assertEqual(manifest["expected_srt_sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(self.fake.calls, [])
        saved = self.store.job(self.job_id)["manifest"]
        self.assertEqual(Path(saved["backup"]).read_bytes(), self.old_content)
        self.assertEqual(saved["output_settings"]["zh_line_width"], self.before["manifest"]["output_settings"]["zh_line_width"])
        for name in ["asr_submit", "asr_query", "mt", "mt_reference_request", "mt_subtitle_request"]:
            self.assertEqual(self.store.counts(self.job_id).get(name, 0), counts.get(name, 0))
        self.assert_evidence_unchanged()
        report = json.dumps(summary(self.store, self.job_id), ensure_ascii=False)
        self.assertNotIn(self.cues[0]["text"], report)
        self.assertNotIn(self.cues[0]["translated"], report)

    def test_overwrite_is_explicit_and_invalid_conflicts_do_not_change_the_task(self):
        self.prepare_legacy(overlap=501)
        for overwrite, code in [(False, "overwrite_required"), (True, "timeline")]:
            with self.subTest(overwrite=overwrite), self.assertRaises(ToolError) as caught:
                Engine(self.config, self.store).retime(self.job_id, overwrite=overwrite)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(self.store.job(self.job_id), self.before)
            self.assertEqual(self.output.read_bytes(), self.old_content)
            self.assert_evidence_unchanged()
        self.assertFalse((self.config.root / ".build" / "retime").exists())

    def test_incomplete_saved_layout_or_translation_is_not_rebuilt(self):
        self.prepare_legacy()
        for broken in ["layout", "translation"]:
            with self.subTest(broken=broken):
                manifest = copy.deepcopy(self.before["manifest"])
                cues = copy.deepcopy(self.cues)
                if broken == "layout":
                    manifest["layout_complete"] = False
                else:
                    cues[0]["translated"] = ""
                self.store.replace_cues(self.job_id, cues)
                self.store.update(self.job_id, manifest=manifest)
                before = self.store.job(self.job_id)
                with self.assertRaises(ToolError) as caught:
                    Engine(self.config, self.store).retime(self.job_id, overwrite=True)
                self.assertEqual(caught.exception.code, "retime_incomplete")
                self.assertEqual(self.store.job(self.job_id), before)
                self.assertEqual(self.output.read_bytes(), self.old_content)

    def test_identity_is_checked_before_reusing_saved_translations(self):
        self.prepare_legacy()
        self.config.set_key("another-offline-account")
        with self.assertRaises(ToolError) as caught:
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(caught.exception.code, "account_changed")
        self.assertEqual(self.store.job(self.job_id), self.before)
        self.assert_evidence_unchanged()

    def test_retime_preparation_transaction_rolls_back_all_publication_fields(self):
        self.prepare_legacy()
        save = self.store._save_output
        def fail_after_output(*args):
            save(*args)
            raise OSError()
        with mock.patch.object(self.store, "_save_output", side_effect=fail_after_output), self.assertRaises(OSError):
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(self.store.job(self.job_id), self.before)
        self.assertEqual(self.output.read_bytes(), self.old_content)
        self.assert_evidence_unchanged()

    def test_retime_commit_and_write_interruptions_resume_saved_content(self):
        self.prepare_legacy()
        for boundary in ["commit", "write"]:
            for fault in [OSError, KeyboardInterrupt]:
                with self.subTest(boundary=boundary, fault=fault.__name__):
                    self.output.write_bytes(self.old_content)
                    self.store.update(self.job_id, "completed", copy.deepcopy(self.before["manifest"]))
                    prior_backups = set(self.path.glob("*.bak.*"))
                    engine = Engine(self.config, self.store)
                    commit = self.store.commit_retime
                    if boundary == "commit":
                        def prepare(*args):
                            commit(*args)
                            raise fault()
                        with mock.patch.object(self.store, "commit_retime", side_effect=prepare), self.assertRaises(fault):
                            engine.retime(self.job_id, overwrite=True)
                    else:
                        engine.retime(self.job_id, overwrite=True)
                    intent = copy.deepcopy(self.store.job(self.job_id)["manifest"]["publication_intent"])
                    if boundary == "write":
                        with mock.patch.object(engine_module, "write_srt", side_effect=fault()):
                            self.assertIn(self.run_fake(self.fake), {"failed", "cancelled"})
                    self.assertEqual(self.store.job(self.job_id)["manifest"]["publication_intent"], intent)
                    engine.resume(self.job_id, overwrite=True)
                    engine.recover_jobs()
                    self.assertEqual(self.run_fake(self.fake), "completed")
                    self.assertEqual(self.store.job(self.job_id)["manifest"]["publication_intent"], intent)
                    backups = set(self.path.glob("*.bak.*")) - prior_backups
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(next(iter(backups)).read_bytes(), self.old_content)
                    self.assertEqual(self.fake.calls, [])
                    self.assert_evidence_unchanged()

    def test_external_edits_are_preserved_and_resume_can_select_another_directory(self):
        self.prepare_legacy()
        engine = Engine(self.config, self.store)
        engine.retime(self.job_id, overwrite=True)
        intent = self.store.job(self.job_id)["manifest"]["publication_intent"]
        self.output.write_bytes(b"external edit")
        self.assertEqual(self.run_fake(self.fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "output_conflict")
        self.assertEqual(self.output.read_bytes(), b"external edit")
        self.output.write_bytes(self.old_content)
        Path(intent["backup"]).write_bytes(b"external backup edit")
        self.assertEqual(self.run_fake(self.fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "output_conflict")
        self.assertEqual(Path(intent["backup"]).read_bytes(), b"external backup edit")
        directory = self.path / "another-output"
        engine.resume(self.job_id, output_dir=directory)
        self.assertEqual(self.run_fake(self.fake), "completed")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.output.read_bytes(), self.old_content)
        self.assertEqual(srt_intervals((directory / self.output.name).read_bytes()), [(1000, 4960), (4960, 8320)])
        self.assert_evidence_unchanged()

    def test_old_prompt_snapshot_can_retime_without_upgrading_translation(self):
        self.prepare_legacy()
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["execution_snapshot"]["prompt_version"] = 0
        self.store.update(self.job_id, manifest=manifest)
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(self.run_fake(self.fake), "completed")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.store.job(self.job_id)["manifest"]["execution_snapshot"]["prompt_version"], 0)

    def test_cli_retime_publishes_to_selected_directory(self):
        self.prepare_legacy()
        directory = self.path / "cli-output"
        with mock.patch("subtitle_tool.main.Config", return_value=self.config), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                mock.patch("sys.stderr", new_callable=io.StringIO), \
                mock.patch("requests.request", side_effect=self.fake.request), \
                mock.patch("requests.post", side_effect=self.fake.upload):
            self.assertEqual(main(["--retime", self.job_id, "--output-dir", str(directory), "--non-interactive"]), 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["retime"]["from_display_timing_version"], 0)
            self.assertEqual(report["display_timing_version"], 2)
        self.assertEqual(self.output.read_bytes(), self.old_content)
        self.assertTrue((directory / self.output.name).is_file())
        self.assertEqual(self.fake.calls, [])
        self.assert_evidence_unchanged()

    def test_pending_legacy_intent_keeps_original_bytes_until_explicit_upgrade(self):
        self.prepare_legacy()
        manifest = copy.deepcopy(self.before["manifest"])
        self.output.unlink()
        # The old intent expected an absent target, as if interrupted before its first write.
        manifest["publication_intent"] = publication.prepare_intent(self.output, self.old_content, False)
        manifest["overwrite"] = False
        self.store.update(self.job_id, "writing", manifest)
        self.assertEqual(self.run_fake(self.fake), "completed")
        self.assertEqual(self.output.read_bytes(), self.old_content)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(srt_intervals(self.old_content), [(1000, 5000), (4960, 8320)])
        self.assertNotIn("display_timing_version", self.store.job(self.job_id)["manifest"])

    def test_legacy_without_subtitle_version_uses_saved_output_layout(self):
        self.prepare_legacy()
        # Recreate a pre-source task using the same complete sentences and translations.
        cues = [{k: v for k, v in c.items() if k not in {"sentence_id", "word_start", "word_end", "char_start", "char_end"}}
                for c in self.cues]
        manifest = copy.deepcopy(self.before["manifest"])
        for field in ["subtitle_version", "translation_strategy_version", "subtitle_settings", "references_planned",
                      "layout_planned", "layout_complete", "publication_intent", "expected_srt_sha256"]:
            manifest.pop(field, None)
        self.store.initialize_subtitles(self.job_id, [], cues, manifest)
        settings = load_policy(manifest).settings(self.config.settings)
        self.output.write_bytes(render_srt(cues, settings, subtitle_version=0))
        before = self.output.read_bytes()
        self.config.settings.other_line_width = 24
        self.config.settings.chinese_first = True
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(self.run_fake(self.fake), "completed")
        after = self.output.read_bytes()
        self.assertEqual(srt_intervals(after), [(1000, 4960), (4960, 8320)])
        self.assertEqual(after.replace(b"00:00:04,960", b"00:00:05,000", 1), before)
