from __future__ import annotations

import io
import json
import os
import threading
from unittest import mock

from subtitle_tool.api import Bailian
from subtitle_tool.common import ToolError
from subtitle_tool.config import Config, Settings, validate_workspace_id
from subtitle_tool.instance import ProcessLock
from subtitle_tool.engine import Engine
from subtitle_tool.main import Progress, main
from subtitle_tool.store import Store
from subtitle_tool.subtitles import mt_cache_key
from tests.fixtures import TemporaryTest, response


class ConfigTests(TemporaryTest):
    def test_line_width_defaults_and_saved_overrides(self):
        config = Config(self.path)
        self.assertEqual((config.settings.zh_line_width, config.settings.other_line_width), (44, 84))
        for values, expected in [({}, (44, 84)), ({'zh_line_width': 22}, (22, 84)),
                                 ({'other_line_width': 42}, (44, 42)),
                                 ({'zh_line_width': 36, 'other_line_width': 80}, (36, 80))]:
            with self.subTest(values=values):
                (config.data / 'settings.json').write_text(json.dumps(values), encoding='utf-8')
                loaded = Config(self.path)
                loaded.save()
                settings = Config(self.path).settings
                self.assertEqual((settings.zh_line_width, settings.other_line_width), expected)

    def test_line_width_limits(self):
        for name, lower, upper in [('zh_line_width', 12, 72), ('other_line_width', 24, 160)]:
            for value in [lower - 1, lower, upper, upper + 1]:
                with self.subTest(name=name, value=value):
                    settings = Settings(**{name: value})
                    if lower <= value <= upper:
                        settings.validate()
                    else:
                        with self.assertRaises(ToolError) as error:
                            settings.validate()
                        self.assertEqual(error.exception.code, 'config')

    def test_plaintext_key_atomic_and_environment_precedence(self):
        config = Config(self.path)
        with mock.patch.dict(os.environ, {}, clear=True):
            config.set_key("offline-local-key")
            self.assertEqual((config.data / "key.txt").read_text(), "offline-local-key")
            self.assertEqual(config.get_key(), "offline-local-key")
            self.assertFalse((config.data / "key.dpapi").exists())
            self.assertFalse(list(config.data.glob("*.tmp")))
        with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "offline-env-key"}):
            self.assertEqual(config.get_key(), "offline-env-key")

    def test_legacy_key_removed_only_after_successful_atomic_migration(self):
        config = Config(self.path)
        legacy = config.data / "key.dpapi"
        legacy.write_bytes(b"old-ciphertext")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("subtitle_tool.config.decrypt_legacy", return_value=b"offline-migrated"):
            with mock.patch("subtitle_tool.config.atomic_bytes", side_effect=OSError()):
                with self.assertRaises(ToolError):
                    config.get_key()
            self.assertTrue(legacy.exists())
            self.assertEqual(config.get_key(), "offline-migrated")
        self.assertFalse(legacy.exists())

    def test_failed_decryption_keeps_legacy_file(self):
        config = Config(self.path)
        legacy = config.data / "key.dpapi"
        legacy.write_bytes(b"unreadable")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("subtitle_tool.config.decrypt_legacy", side_effect=ToolError("重新输入", "credentials")):
            with self.assertRaises(ToolError):
                config.get_key()
        self.assertTrue(legacy.exists())
        self.assertFalse((config.data / "key.txt").exists())

    def test_workspace_is_one_dns_label(self):
        self.assertEqual(validate_workspace_id("Abc-123"), "abc-123")
        for value in ["", "https://abc", "abc.example", "a/b", "-abc", "abc-", "a_b", "中", "a" * 64]:
            with self.subTest(value=value), self.assertRaises(ToolError):
                validate_workspace_id(value)

    def test_workspace_endpoints_and_public_upload_policy(self):
        cloud = Bailian("offline-key", "beijing", threading.Event(), workspace_id="space-a", access_mode="workspace")
        with mock.patch("requests.request", return_value=response({"output": {"task_id": "offline-id"}})) as request:
            cloud.submit("oss://offline/a.flac")
            self.assertEqual(request.call_args.args[1], "https://space-a.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription")
        with mock.patch("requests.request", return_value=response({"output": {"task_status": "PENDING"}})) as request:
            cloud.query("offline-id")
            self.assertIn("https://space-a.cn-beijing.maas.aliyuncs.com/api/v1/tasks/", request.call_args.args[1])
        with mock.patch("requests.request", return_value=response({"choices": [{"finish_reason": "stop", "message": {"content": "译文"}}]})) as request:
            cloud.translate("Hello", "English")
            self.assertEqual(request.call_args.args[1], "https://space-a.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions")
        with mock.patch.object(cloud.http, "json", return_value={"data": {}}) as request:
            with self.assertRaises(ToolError):
                cloud.policy()
            self.assertEqual(request.call_args.args[1], "https://dashscope.aliyuncs.com/api/v1/uploads")

    def test_mt_cache_isolated_by_account_workspace_and_version(self):
        args = dict(account="account-a", workspace_id="space-a", access_mode="workspace", processing_version=2)
        original = mt_cache_key("hello", **args)
        for field, value in [("account", "account-b"), ("workspace_id", "space-b"), ("access_mode", "legacy_public"), ("processing_version", 1)]:
            self.assertNotEqual(original, mt_cache_key("hello", **(args | {field: value})))


class CliTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        self.config.settings.workspace_id = "offline-space"
        self.config.set_key("offline-cli-key")
        self.config.save()
        self.config_patch = mock.patch("subtitle_tool.main.Config", return_value=self.config)
        self.config_patch.start()
        self.source = self.path / "中文 多点.片段 & (1).mkv"
        self.source.write_bytes(b"fixture")

    def tearDown(self):
        self.config_patch.stop()
        super().tearDown()

    def test_recovery_progress_displays_interval_change_and_return_to_zero(self):
        progress = Progress()
        with mock.patch('sys.stderr', new_callable=io.StringIO) as output:
            progress('offline-job', 'mt_recovery', 8, 5.6)
            progress('offline-job', 'mt_recovery', 0.11, 0)
        self.assertEqual(output.getvalue().splitlines(), [
            '[offline-job] 翻译提速：请求间隔 8.00 → 5.60 秒',
            '[offline-job] 翻译提速：请求间隔 0.11 秒，恢复无额外间隔'])

    def test_quota_wait_is_distinct_from_server_throttling(self):
        progress = Progress()
        with mock.patch('sys.stderr', new_callable=io.StringIO) as output:
            progress('offline-job', 'mt_quota_wait', 40.5, 60)
        self.assertEqual(output.getvalue().strip(),
                         '[offline-job] 翻译配额等待：约 40.50 秒，滚动窗口最多 60 次请求')

    def test_no_arguments_displays_help_without_creating_store(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main([]), 0)
        self.assertIn("--configure", output.getvalue())
        self.assertFalse((self.config.data / "jobs.sqlite").exists())

    def test_jobs_cancel_and_noninteractive_review(self):
        store = Store(self.config.data / "jobs.sqlite")
        job_id = store.enqueue(self.source, "beijing", workspace_id="offline-space")
        store.replace_cues(job_id, [{"cue_id": "0", "begin_ms": 0, "end_ms": 900, "text": "OK", "language": "", "decision": "uncertain", "reason": "missing"}])
        store.update(job_id, "needs_review", code="language_uncertain")
        store.close()
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output, mock.patch("sys.stderr", new_callable=io.StringIO), mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.assertEqual(main(["--jobs"]), 0)
            self.assertEqual(json.loads(output.getvalue())["job_id"], job_id)
            output.truncate(0)
            output.seek(0)
            self.assertEqual(main(["--review", job_id, "--non-interactive"]), 2)
            self.assertEqual(json.loads(output.getvalue())["state"], "needs_review")
            self.assertEqual(main(["--cancel", job_id]), 0)
        store = Store(self.config.data / "jobs.sqlite")
        Engine(self.config, store).recover_jobs()
        self.assertEqual(store.job(job_id)["state"], "cancelled")
        store.close()

    def test_second_process_cannot_use_task_store(self):
        lock = ProcessLock(self.config.data)
        self.assertTrue(lock.acquire())
        try:
            with self.assertRaises(ToolError) as error:
                main(["--jobs"])
            self.assertEqual(error.exception.code, "instance_busy")
        finally:
            lock.close()

    def add_review_job(self, code="language_uncertain", *, media=None):
        store = Store(self.config.data / "jobs.sqlite")
        job_id = store.enqueue(self.source, "beijing", workspace_id="offline-space")
        manifest = store.job(job_id)["manifest"]
        if media:
            manifest["media"] = media
        store.replace_cues(job_id, [{"cue_id": "0", "begin_ms": 0, "end_ms": 900, "text": "OK", "language": "", "decision": "uncertain", "reason": "missing"}])
        store.update(job_id, "needs_review", manifest, code=code)
        store.close()
        return job_id

    def test_interactive_review_updates_stable_cue_and_manual_mt_language(self):
        job_id = self.add_review_job()
        def run(engine, current):
            cue = engine.store.cues(current)[0]
            self.assertEqual(cue["cue_id"], "0")
            self.assertEqual((cue["decision"], cue["source_lang"]), ("translate", "English"))
            engine.store.update(current, "completed")
            return "completed"
        with mock.patch("sys.stdin", new=mock.Mock(isatty=lambda: True)), mock.patch("builtins.input", side_effect=["t", "en", "English"]), mock.patch.object(Engine, "run", new=run), mock.patch("sys.stderr", new_callable=io.StringIO), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["--review", job_id]), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "completed")

    def test_interactive_review_pause_does_not_prompt_again(self):
        job_id = self.add_review_job()
        with mock.patch("sys.stdin", new=mock.Mock(isatty=lambda: True)), mock.patch("builtins.input", return_value="q") as prompt, mock.patch("sys.stderr", new_callable=io.StringIO), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--review", job_id]), 2)
            self.assertEqual(prompt.call_count, 1)

    def test_interactive_audio_track_then_channel_choice(self):
        job_id = self.add_review_job("audio_track", media={"duration_ms": 1000, "origin_ms": 0, "tracks": [
            {"index": 1, "channels": 1}, {"index": 2, "channels": 2}]})
        def run(engine, current):
            manifest = engine.store.job(current)["manifest"]
            self.assertEqual((manifest["track"], manifest["channel"]), (2, "right"))
            engine.store.update(current, "completed")
            return "completed"
        with mock.patch("sys.stdin", new=mock.Mock(isatty=lambda: True)), mock.patch("builtins.input", side_effect=["2", "right"]), mock.patch.object(Engine, "run", new=run), mock.patch("sys.stderr", new_callable=io.StringIO), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--review", job_id]), 0)

    def test_interactive_channel_review_uses_selected_audio_track(self):
        job_id = self.add_review_job("audio_channel", media={"duration_ms": 1000, "origin_ms": 0, "tracks": [
            {"index": 2, "channels": 2, "disposition": {"default": 1}}, {"index": 3, "channels": 1}]})

        def run(engine, current):
            manifest = engine.store.job(current)["manifest"]
            self.assertEqual((manifest["track"], manifest["channel"]), (2, "right"))
            engine.store.update(current, "completed")
            return "completed"

        with mock.patch("sys.stdin", new=mock.Mock(isatty=lambda: True)), \
                mock.patch("builtins.input", side_effect=["right"]) as prompt, \
                mock.patch.object(Engine, "run", new=run), \
                mock.patch("sys.stderr", new_callable=io.StringIO), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--review", job_id]), 0)
        self.assertEqual(prompt.call_count, 1)

    def test_configure_uses_hidden_key_input_and_saves_workspace(self):
        with mock.patch("sys.stdin", new=mock.Mock(isatty=lambda: True)), mock.patch("builtins.input", return_value="SPACE-NEW"), mock.patch("getpass.getpass", return_value="offline-hidden-key") as hidden, mock.patch("sys.stderr", new_callable=io.StringIO) as output:
            self.assertEqual(main(["--configure"]), 0)
        hidden.assert_called_once()
        self.assertNotIn("offline-hidden-key", output.getvalue())
        self.assertEqual(json.loads((self.config.data / "settings.json").read_text())["workspace_id"], "space-new")
        self.assertEqual((self.config.data / "key.txt").read_text(), "offline-hidden-key")

    def test_workspace_override_cannot_move_saved_job(self):
        job_id = self.add_review_job()
        with self.assertRaises(ToolError) as error:
            main(["--resume", job_id, "--workspace-id", "other-space", "--non-interactive"])
        self.assertEqual(error.exception.code, "cache_mismatch")

    def test_new_job_requires_workspace_without_prompt_in_noninteractive_mode(self):
        self.config.settings.workspace_id = ""
        with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")), self.assertRaises(ToolError) as error:
            main([str(self.source), "--non-interactive"])
        self.assertEqual(error.exception.code, "workspace_id")
