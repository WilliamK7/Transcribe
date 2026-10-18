from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.config import Config
from subtitle_tool.main import main
from subtitle_tool.store import Store
from tests.fixtures import ROOT, TemporaryTest


class ReportProtectionTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        self.config.settings.workspace_id = "offline-space"
        self.config.set_key("offline-report-key")
        self.source = self.path / "source.mkv"
        self.source.write_bytes(b"original media")
        self.store = Store(self.config.data / "jobs.sqlite")
        self.job_id = self.store.enqueue(self.source, workspace_id="offline-space")
    def tearDown(self):
        self.store.close()
        super().tearDown()

    def cli(self, args):
        with mock.patch("subtitle_tool.main.Config", return_value=self.config), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            return main([*map(str, args), "--non-interactive"])

    def assert_conflict(self, args, path):
        before = path.read_bytes() if path.is_file() else None
        job = self.store.job(self.job_id)
        with mock.patch("requests.request") as request, mock.patch("requests.post") as upload:
            with self.assertRaises(ToolError) as caught:
                self.cli([*args, "--report", path])
            self.assertEqual(caught.exception.code, "report_conflict")
            request.assert_not_called()
            upload.assert_not_called()
        self.assertEqual(self.store.job(self.job_id), job)
        self.assertEqual(path.read_bytes() if path.is_file() else None, before)

    def test_all_actions_reject_inputs_before_recovery_or_state_changes(self):
        self.store.update(self.job_id, "recognizing")
        for args in [["--jobs"], ["--cancel", self.job_id], ["--resume", self.job_id],
                     ["--reflow", self.job_id], ["--review", self.job_id], [self.source], ["--self-test"]]:
            with self.subTest(args=args):
                self.assert_conflict(args, self.source)

    def test_outputs_backups_internal_files_and_snapshots_are_protected(self):
        output = self.source.with_suffix(".srt")
        backup = output.with_name(output.name + ".20261018-000000.bak")
        cache = self.config.data / "cache" / self.job_id / "raw-0000.json"
        snapshot = self.path / ".build" / "reflow" / self.job_id / "snapshot.json"
        for path in [output, backup, cache, snapshot]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"private saved bytes")
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["backup"] = str(backup)
        self.store.update(self.job_id, manifest=manifest)
        for path in [output, backup, cache, snapshot, self.config.data / "key.txt",
                     self.config.data / "jobs.sqlite", self.config.data / "future.json",
                     self.path / ".build" / "reflow" / "future.json"]:
            with self.subTest(path=path.name):
                self.assert_conflict(["--jobs"], path)

    def test_new_inputs_and_expected_outputs_are_protected_before_enqueue(self):
        source = self.path / "new.mkv"
        source.write_bytes(b"new input")
        for path in [source, source.with_suffix(".srt"), self.path / "chosen" / "new.srt"]:
            with self.subTest(path=path):
                self.assert_conflict([source, "--output-dir", self.path / "chosen"] if path.parent.name == "chosen"
                                     else [source], path)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_changed_resume_output_is_protected_before_mutation(self):
        target = self.path / "chosen" / self.source.with_suffix(".srt").name
        self.assert_conflict(["--resume", self.job_id, "--output-dir", target.parent], target)

    def test_hardlinks_case_and_directory_aliases(self):
        alias = self.path / "report.json"
        for protected in [self.source, self.config.data / "key.txt"]:
            os.link(protected, alias)
            try:
                self.assert_conflict(["--jobs"], alias)
            finally:
                alias.unlink()
        if os.name == "nt":
            self.assert_conflict(["--jobs"], Path(str(self.source).swapcase()))
            import _winapi
            junction = self.path / "data-alias"
            _winapi.CreateJunction(str(self.config.data), str(junction))
            try:
                self.assert_conflict(["--jobs"], junction / "key.txt")
            finally:
                junction.rmdir()

    def test_windows_alternate_data_stream_is_not_a_report_target(self):
        if os.name == "nt":
            self.assert_conflict(["--jobs"], Path(str(self.source) + ":report.json"))

    def test_older_srt_backups_remain_protected(self):
        backup = self.source.with_suffix(".srt").with_name("source.srt.bak.older")
        backup.write_bytes(b"older subtitles")
        self.assert_conflict(["--jobs"], backup)

    def test_unverifiable_protected_identity_is_rejected(self):
        target = self.path / "report.json"
        target.write_bytes(b"old report")
        with mock.patch("subtitle_tool.reports.Path.samefile", side_effect=PermissionError()):
            self.assert_conflict(["--jobs"], target)

    def test_write_rechecks_new_backup_created_after_preflight(self):
        target = self.path / "created-later.bak"
        def run(engine, job_id):
            target.write_bytes(b"saved backup")
            manifest = self.store.job(job_id)["manifest"]
            manifest["backup"] = str(target)
            self.store.update(job_id, "completed", manifest)
            return "completed"
        with mock.patch("subtitle_tool.main.Engine.run", new=run):
            with self.assertRaises(ToolError) as caught:
                self.cli(["--resume", self.job_id, "--report", target])
        self.assertEqual(caught.exception.code, "report_conflict")
        self.assertEqual(target.read_bytes(), b"saved backup")

    def test_normal_reports_can_be_atomically_replaced(self):
        target = self.path / "report.txt"
        target.write_bytes(b"previous report")
        self.assertEqual(self.cli(["--jobs", "--report", target]), 0)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))[0]["job_id"], self.job_id)

    def test_unrelated_unreadable_cache_does_not_block_new_report(self):
        target = self.path / "report.json"
        with mock.patch("subtitle_tool.reports.os.walk", side_effect=PermissionError()):
            self.assertEqual(self.cli(["--jobs", "--report", target]), 0)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))[0]["job_id"], self.job_id)

    def test_interrupt_report_rechecks_a_new_input_alias(self):
        target = self.path / "interrupt.json"
        def interrupt(engine, job_id):
            os.link(self.source, target)
            raise KeyboardInterrupt()
        with mock.patch("subtitle_tool.main.Engine.run", new=interrupt):
            with self.assertRaises(ToolError) as caught:
                self.cli(["--resume", self.job_id, "--report", target])
        self.assertEqual(caught.exception.code, "report_conflict")
        self.assertEqual(self.source.read_bytes(), b"original media")

    def test_selftest_default_exception_does_not_allow_hardlink_to_credentials(self):
        target = self.config.data / "self-test.json"
        os.link(self.config.data / "key.txt", target)
        self.assert_conflict(["--self-test"], target)

    def test_selftest_default_and_external_reports_do_not_recover_or_read_key(self):
        from subtitle_tool.media import Media
        self.store.update(self.job_id, "recognizing")
        for target in [self.config.data / "self-test.json", self.path / "diagnostics.json"]:
            with mock.patch.object(Config, "get_key", side_effect=AssertionError("must not read key")), \
                    mock.patch.object(Store, "recover", side_effect=AssertionError("must not recover")), \
                    mock.patch.object(Media, "executable", lambda media, name: ROOT / "tools" / (name + ".exe")), \
                    mock.patch("requests.request") as request:
                self.assertEqual(self.cli(["--self-test", "--report", target]), 0)
                request.assert_not_called()
            self.assertTrue(json.loads(target.read_text(encoding="utf-8"))["passed"])
            self.assertEqual(self.store.job(self.job_id)["state"], "recognizing")
