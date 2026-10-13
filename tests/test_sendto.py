from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import unittest
import venv
from pathlib import Path

from tests.fixtures import ROOT, TemporaryTest


POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
SHORTCUT_NAME = "!百炼字幕.lnk"


@unittest.skipUnless(os.name == "nt" and POWERSHELL.is_file(), "Windows PowerShell required")
class SendToTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.project = self.path / "项目 空格 [1] & (2) $字幕 ';%"
        self.scripts = self.project / "scripts"
        self.scripts.mkdir(parents=True)
        for name in ("sendto.ps1", "install-sendto.ps1", "transcribe.ps1"):
            shutil.copyfile(ROOT / "scripts" / name, self.scripts / name)
        # A real, dependency-free interpreter exercises the existing launch script.
        runtime = self.path / "runtime"
        venv.EnvBuilder(with_pip=False).create(runtime)
        # venv creation rejects ';', but an existing environment can be moved there.
        self.assertTrue(runtime.resolve().is_relative_to(self.path.resolve()))
        self.assertTrue((self.project / ".venv").resolve().is_relative_to(self.path.resolve()))
        runtime.rename(self.project / ".venv")
        self.record = self.project / "invocation.json"
        (self.project / "launcher.py").write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "root = Path(__file__).parent\n"
            "record = {'args': sys.argv[1:], 'cwd': os.getcwd()}\n"
            "if os.environ.get('SENDTO_TEST_REVIEW'):\n"
            "    record['answer'] = input('CLI review: ')\n"
            "(root / 'invocation.json').write_text(json.dumps(record), encoding='utf-8')\n"
            "print('completed', flush=True)\n"
            "sys.exit(int(os.environ.get('SENDTO_TEST_EXIT', '0')))\n",
            encoding="utf-8",
        )
        self.menu = self.path / "隔离 菜单 [3] & (4) $菜单 ';%"
        self.media = [self.path / "中文 空格.多点 & (1) [x] $name ';%!.mkv", self.path / "另一段 音频.flac"]
        for path in self.media:
            path.write_bytes(b"offline fixture")

    def command(self, script, *args):
        return [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *map(str, args)]

    def run_script(self, script, *args, input="", env=None):
        return subprocess.run(self.command(script, *args), cwd=self.path, input=input, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=25, env=env, shell=False)

    def install(self, project=None, remove=False):
        script = (project or self.project) / "scripts/install-sendto.ps1"
        arguments = ["-SendToDirectory", str(self.menu)]
        if remove:
            arguments.append("-Remove")
        return self.run_script(script, *arguments)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def inspect_shortcut(self):
        script = self.path / "inspect.ps1"
        script.write_text(
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
            "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($args[0])\n"
            "@{target=$link.TargetPath; arguments=$link.Arguments; working_directory=$link.WorkingDirectory; "
            "description=$link.Description; window_style=$link.WindowStyle} | ConvertTo-Json -Compress\n",
            encoding="utf-8-sig",
        )
        result = self.run_script(script, self.menu / SHORTCUT_NAME)
        self.assert_ok(result)
        return json.loads(result.stdout)

    def test_shortcut_target_arguments_and_normal_window(self):
        self.assert_ok(self.install())
        link = self.inspect_shortcut()
        self.assertEqual(os.path.normcase(link["target"]), os.path.normcase(str(POWERSHELL)))
        self.assertEqual(link["arguments"], f'-NoProfile -ExecutionPolicy Bypass -File "{self.scripts / "sendto.ps1"}"')
        self.assertEqual(link["working_directory"], str(self.project))
        self.assertEqual(link["window_style"], 1)
        self.assertTrue(link["description"])
        # Use the saved command line, as the shell does when appending selected files.
        command_line = (subprocess.list2cmdline([link["target"]]) + " " + link["arguments"] + " " +
                        subprocess.list2cmdline(list(map(str, self.media))))
        result = subprocess.run(command_line, executable=link["target"], cwd=link["working_directory"],
                                input="", capture_output=True, encoding="utf-8", errors="replace", timeout=25, shell=False)
        self.assert_ok(result)
        self.assertEqual(json.loads(self.record.read_text(encoding="utf-8"))["args"], ["--", *map(str, self.media)])

    def test_setup_installs_after_assets_and_reports_preparation_or_install_failure(self):
        shutil.copyfile(ROOT / "scripts/setup.ps1", self.scripts / "setup.ps1")
        shutil.copyfile(self.scripts / "install-sendto.ps1", self.scripts / "install-sendto-core.ps1")
        (self.scripts / "install-sendto.ps1").write_text(
            "if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'assets-ready'))) { throw 'Wrong setup order.' }\n"
            "& (Join-Path $PSScriptRoot 'install-sendto-core.ps1') -SendToDirectory $env:SENDTO_TEST_MENU\n"
            "exit $LASTEXITCODE\n",
            encoding="utf-8-sig",
        )
        (self.scripts / "prepare_assets.py").write_text(
            "import os, sys\nfrom pathlib import Path\n"
            "code = int(os.environ.get('SENDTO_TEST_ASSET_EXIT', '0'))\n"
            "if code == 0:\n    Path(__file__).with_name('assets-ready').touch()\n"
            "sys.exit(code)\n",
            encoding="utf-8",
        )
        env = os.environ | {"SENDTO_TEST_MENU": str(self.menu)}
        result = self.run_script(self.scripts / "setup.ps1", "-SkipDependencyInstall", env=env)
        self.assert_ok(result)
        self.assertTrue((self.menu / SHORTCUT_NAME).is_file())
        self.assertIn("Local CLI environment ready", result.stdout)
        self.assert_ok(self.run_script(self.scripts / "install-sendto-core.ps1", "-SendToDirectory", self.menu, "-Remove"))
        (self.scripts / "assets-ready").unlink()
        result = self.run_script(self.scripts / "setup.ps1", "-SkipDependencyInstall",
                                 env=env | {"SENDTO_TEST_ASSET_EXIT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.menu / SHORTCUT_NAME).exists())
        self.assertNotIn("Local CLI environment ready", result.stdout)
        conflict = self.menu / SHORTCUT_NAME
        conflict.write_bytes(b"foreign entry")
        result = self.run_script(self.scripts / "setup.ps1", "-SkipDependencyInstall", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Local CLI environment ready", result.stdout)
        self.assertEqual(conflict.read_bytes(), b"foreign entry")

    def test_reinstall_move_update_and_idempotent_remove_preserve_other_entries(self):
        self.assert_ok(self.install())
        other = self.menu / "其他工具.lnk"
        other.write_bytes(b"unrelated shortcut")
        self.assert_ok(self.install())
        moved = self.path / "移动后的 项目 [5] & $字幕 ';%"
        # Both absolute targets are confined to this test's workspace directory.
        self.assertTrue(self.project.resolve().is_relative_to(self.path.resolve()))
        self.assertTrue(moved.resolve().is_relative_to(self.path.resolve()))
        self.project.rename(moved)
        self.assert_ok(self.install(moved))
        link = self.inspect_shortcut()
        self.assertEqual(link["working_directory"], str(moved))
        self.assertEqual(link["arguments"], f'-NoProfile -ExecutionPolicy Bypass -File "{moved / "scripts/sendto.ps1"}"')
        # Removal still works if the old project has lost its launch script.
        (moved / "scripts/sendto.ps1").unlink()
        self.assert_ok(self.install(moved, remove=True))
        self.assert_ok(self.install(moved, remove=True))
        self.assertFalse((self.menu / SHORTCUT_NAME).exists())
        self.assertEqual(other.read_bytes(), b"unrelated shortcut")

    def test_install_and_remove_refuse_foreign_shortcut_without_changing_it(self):
        self.menu.mkdir()
        script = self.path / "foreign.ps1"
        script.write_text(
            "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($args[0])\n"
            "$link.TargetPath = $args[1]\n$link.Description = 'Another application'\n$link.Save()\n",
            encoding="utf-8-sig",
        )
        foreign = self.menu / SHORTCUT_NAME
        self.assert_ok(self.run_script(script, foreign, POWERSHELL))
        before = foreign.read_bytes()
        for remove in (False, True):
            with self.subTest(remove=remove):
                result = self.install(remove=remove)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("同名", result.stderr)
                self.assertEqual(foreign.read_bytes(), before)

    def test_own_shortcut_can_be_updated_or_removed_after_arguments_change(self):
        script = self.path / "customize.ps1"
        script.write_text(
            "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($args[0])\n"
            "$link.Arguments = '-NoLogo ' + $link.Arguments\n$link.Save()\n",
            encoding="utf-8-sig",
        )
        for remove in (False, True):
            with self.subTest(remove=remove):
                self.assert_ok(self.install())
                self.assert_ok(self.run_script(script, self.menu / SHORTCUT_NAME))
                self.assert_ok(self.install(remove=remove))
                if remove:
                    self.assertFalse((self.menu / SHORTCUT_NAME).exists())
                else:
                    self.assertNotIn("-NoLogo", self.inspect_shortcut()["arguments"])

    def test_install_refuses_same_named_file_or_directory(self):
        self.menu.mkdir()
        conflict = self.menu / SHORTCUT_NAME
        for directory in (False, True):
            with self.subTest(directory=directory):
                if directory:
                    conflict.mkdir()
                else:
                    conflict.write_bytes(b"not a shortcut")
                for remove in (False, True):
                    result = self.install(remove=remove)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(conflict.exists())
                if directory:
                    conflict.rmdir()
                else:
                    self.assertEqual(conflict.read_bytes(), b"not a shortcut")
                    conflict.unlink()

    def test_multi_file_paths_are_separate_literal_arguments_and_success_does_not_wait(self):
        process = subprocess.Popen(self.command(self.scripts / "sendto.ps1", *self.media), cwd=self.path,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   shell=False)
        try:
            # Keep stdin open: an unexpected close prompt would block here.
            self.assertEqual(process.wait(timeout=20), 0)
            stdout, stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertIn(b"completed", stdout)
        self.assertEqual(stderr, b"")
        recorded = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(recorded["args"], ["--", *map(str, self.media)])
        self.assertEqual(recorded["cwd"], str(self.path))

    def test_failed_review_and_cancelled_exit_codes_wait_for_enter_and_are_preserved(self):
        for exit_code in (2, 3, 130):
            with self.subTest(exit_code=exit_code):
                self.record.unlink(missing_ok=True)
                env = os.environ | {"SENDTO_TEST_EXIT": str(exit_code)}
                process = subprocess.Popen(self.command(self.scripts / "sendto.ps1", self.media[0]), cwd=self.path,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env=env, shell=False)
                try:
                    deadline = time.monotonic() + 20
                    while not self.record.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(self.record.exists())
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.wait(timeout=0.6)
                    stdout, stderr = process.communicate(input=b"\r\n", timeout=10)
                    self.assertEqual(process.returncode, exit_code, (stdout, stderr))
                    self.assertIn("回车", stdout.decode("utf-8", errors="replace"))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_cli_review_receives_input_before_wrapper_close_prompt(self):
        env = os.environ | {"SENDTO_TEST_REVIEW": "1"}
        result = self.run_script(self.scripts / "sendto.ps1", self.media[0], input="right\n", env=env)
        self.assert_ok(result)
        self.assertEqual(json.loads(self.record.read_text(encoding="utf-8"))["answer"], "right")
        self.assertNotIn("回车", result.stdout)

    def test_no_files_directory_and_missing_file_do_not_start_cli(self):
        for arguments in ([], [self.path], [self.media[0], self.path / "missing.mkv"]):
            with self.subTest(arguments=arguments):
                result = self.run_script(self.scripts / "sendto.ps1", *arguments, input="\n")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("回车", result.stdout)
                self.assertFalse(self.record.exists())

    def test_missing_environment_or_launcher_shows_safe_setup_message(self):
        for missing in (self.project / ".venv/Scripts/python.exe", self.scripts / "transcribe.ps1"):
            with self.subTest(missing=missing):
                saved = missing.read_bytes()
                missing.unlink()
                result = self.run_script(self.scripts / "sendto.ps1", self.media[0], input="\n")
                missing.write_bytes(saved)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("setup.ps1", result.stderr)
                self.assertIn("回车", result.stdout)
                self.assertNotIn("CategoryInfo", result.stderr)
                self.assertFalse(self.record.exists())

    def test_launcher_exception_is_not_echoed(self):
        (self.scripts / "transcribe.ps1").write_text("throw 'Bearer offline-secret Signature=offline-private'\n", encoding="utf-8-sig")
        result = self.run_script(self.scripts / "sendto.ps1", self.media[0], input="\n")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("offline-secret", result.stdout + result.stderr)
        self.assertNotIn("offline-private", result.stdout + result.stderr)
        self.assertNotIn("CategoryInfo", result.stderr)
        self.assertIn("回车", result.stdout)

    def test_real_cli_skips_existing_subtitles_without_cloud_requests(self):
        shutil.copytree(ROOT / "subtitle_tool", self.project / "subtitle_tool", ignore=shutil.ignore_patterns("__pycache__"))
        packages = self.project / ".venv/Lib/site-packages"
        (packages / "test-dependencies.pth").write_text(str(ROOT / ".venv/Lib/site-packages") + "\n", encoding="utf-8")
        (self.project / "launcher.py").write_text(
            "import requests\n"
            "def no_network(*args, **kwargs):\n    raise AssertionError('Cloud requests are forbidden')\n"
            "requests.request = requests.post = no_network\n"
            "from subtitle_tool.main import entry\nentry()\n",
            encoding="utf-8",
        )
        data = self.project / "data"
        data.mkdir()
        (data / "settings.json").write_text('{"workspace_id":"offline-space"}', encoding="utf-8")
        (data / "key.txt").write_text("offline-key", encoding="utf-8")
        for media in self.media:
            media.with_suffix(".srt").write_bytes(b"keep existing subtitle")
        result = self.run_script(self.scripts / "sendto.ps1", *self.media)
        self.assert_ok(result)
        reports = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([item["state"] for item in reports], ["skipped", "skipped"])
        self.assertEqual([item["source"] for item in reports], list(map(str, self.media)))
        for media in self.media:
            self.assertEqual(media.with_suffix(".srt").read_bytes(), b"keep existing subtitle")
