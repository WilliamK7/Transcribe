from __future__ import annotations

import io
import json
import os
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.config import Config, Settings, validate_workspace_id
from subtitle_tool.instance import ProcessLock
from subtitle_tool.engine import Engine
from subtitle_tool.main import Progress, main
from subtitle_tool.store import Store
from tests.fixtures import set_asr_account, TemporaryTest


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


    def test_workspace_is_one_dns_label(self):
        self.assertEqual(validate_workspace_id("Abc-123"), "abc-123")
        for value in ["", "https://abc", "abc.example", "a/b", "-abc", "abc-", "a_b", "中", "a" * 64]:
            with self.subTest(value=value), self.assertRaises(ToolError):
                validate_workspace_id(value)


class CliTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        self.config.settings.workspace_id = "offline-space"
        set_asr_account(self.config, "offline-cli-key")
        self.config.save()
        self.config_patch = mock.patch("subtitle_tool.main.Config", return_value=self.config)
        self.config_patch.start()
        self.source = self.path / "中文 多点.片段 & (1).mkv"
        self.source.write_bytes(b"fixture")

    def tearDown(self):
        self.config_patch.stop()
        super().tearDown()


    def test_pool_progress_describes_active_stages(self):
        progress = Progress()
        with mock.patch('sys.stderr', new_callable=io.StringIO) as output:
            progress('offline-job', 'mt_v6_progress', {'stages': {'glossary': 1, 'translate': 2}, 'elapsed': 15})
        self.assertIn('术语分析 1 组', output.getvalue())
        self.assertIn('字幕翻译 2 组', output.getvalue())
        self.assertIn('15 秒', output.getvalue())


    def test_no_arguments_displays_help_without_creating_store(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main([]), 0)
        self.assertIn("--check-mt", output.getvalue())
        self.assertFalse((self.config.data / "jobs.sqlite").exists())

    def test_check_magpie_preserves_effective_gateway_and_groups(self):
        for source in ('settings', 'environment'):
            with self.subTest(source=source):
                self.config.settings.mt_base_url = 'https://gateway.example/v1'
                self.config.settings.mt_model = 'group/custom'
                self.config.settings.mt_fallback_model = 'group/custom-paid'
                environment = {'MAGPIE_API_KEY': 'synthetic-gateway'}
                if source == 'environment':
                    environment.update(MT_BASE_URL='https://gateway.example/v1',
                                       MT_MODEL='group/custom',
                                       MT_FALLBACK_MODEL='group/custom-paid')
                    self.config.settings.mt_base_url = 'https://openrouter.ai/api/v1'
                    self.config.settings.mt_model = 'openrouter/free'
                    self.config.settings.mt_fallback_model = 'group/unused-paid'
                self.config.save()
                previous = vars(self.config.settings).copy()
                saved = (self.config.data / 'settings.json').read_bytes()
                with mock.patch.dict(os.environ, environment), \
                        mock.patch('subtitle_tool.mt_pipeline.check_pool', return_value={}) as check, \
                        mock.patch('sys.stdout', new_callable=io.StringIO):
                    self.assertEqual(main(['--check-mt']), 0)
                route = check.call_args.args[2]
                self.assertEqual(route['routing']['strategy'], 'free_first')
                self.assertEqual([(entry['backend']['provider'], entry['backend']['base_url'],
                                   entry['backend']['model']) for entry in route['entries']],
                                 [('magpie', 'https://gateway.example/v1', name) for name in
                                  ['group/custom', 'group/custom-paid', 'group/transcribe-review',
                                   'group/transcribe-review-paid', 'group/transcribe-judge', 'group/transcribe-judge-paid']])
                self.assertEqual(vars(self.config.settings), previous)
                self.assertEqual((self.config.data / 'settings.json').read_bytes(), saved)


    def test_jobs_cancel_and_noninteractive_review(self):
        store = Store(self.config.data / "jobs.sqlite")
        job_id = store.enqueue(self.source, workspace_id="offline-space")
        from subtitle_tool.subtitles import normalize, make_fragment
        from tests.fixtures import result
        sources = normalize(result(('OK', '')), 0, '0000')[0]
        store.initialize_subtitles(job_id, sources, [make_fragment(s) for s in sources], store.job(job_id)['manifest'])
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
        job_id = store.enqueue(self.source, workspace_id="offline-space")
        manifest = store.job(job_id)["manifest"]
        if media:
            manifest["media"] = media
        from subtitle_tool.subtitles import normalize, make_fragment
        from tests.fixtures import result
        sources = normalize(result(('OK', '')), 0, '0000')[0]
        store.initialize_subtitles(job_id, sources, [make_fragment(s) for s in sources], store.job(job_id)['manifest'])
        store.update(job_id, "needs_review", manifest, code=code)
        store.close()
        return job_id

    def test_interactive_review_updates_stable_cue_and_manual_mt_language(self):
        job_id = self.add_review_job()
        def run(engine, current):
            cue = engine.store.cues(current)[0]
            self.assertTrue(cue["cue_id"].startswith("0000:"))
            self.assertEqual((cue["decision"], cue["source_lang"]), ("translate", "English"))
            engine.store.update(current, "completed")
            return "completed"
        with mock.patch("sys.stdin", new=mock.Mock(isatty=lambda: True)), mock.patch("builtins.input", side_effect=["t", "en", "English"]), mock.patch.object(Engine, "run", new=run), mock.patch("sys.stderr", new_callable=io.StringIO), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["--review", job_id]), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "completed")

    def test_review_hint_matches_pending_decision(self):
        for code in ('language_uncertain', 'audio_track', 'audio_channel'):
            with self.subTest(code=code):
                job_id = self.add_review_job(code)
                with mock.patch('sys.stdout', new_callable=io.StringIO), \
                        mock.patch('sys.stderr', new_callable=io.StringIO) as output, \
                        mock.patch('builtins.input', side_effect=AssertionError('must not prompt')):
                    self.assertEqual(main(['--review', job_id, '--non-interactive']), 2)
                message = output.getvalue()
                self.assertIn(f'--review {job_id}', message)
                if code == 'language_uncertain':
                    self.assertNotIn('--track', message)
                    self.assertNotIn('--channel', message)
                    self.assertIn('交互终端', message)
                else:
                    self.assertIn(f'--resume {job_id}', message)
                    self.assertIn('--channel', message)

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
