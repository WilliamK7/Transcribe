from __future__ import annotations

import os
from unittest import mock


from subtitle_tool.common import ToolError, atomic_bytes
from subtitle_tool.config import Config
from subtitle_tool.main import main
from subtitle_tool.mt_backend import normalize_mt_url
from tests.fixtures import configured_backend, TemporaryTest


class MTConfigurationTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        self.config.settings.mt_provider = 'bailian'  # One-model shortcut.
        self.config.set_key("separate-bailian-key")
        self.manifest = {"region": "beijing", "access_mode": "workspace", "workspace_id": "offline-space"}

    def test_defaults_and_independent_environment_file_credentials(self):
        backend = configured_backend(self.config, self.manifest)
        self.assertEqual((backend.provider, backend.model), ("bailian", "qwen3.8-flash"))
        with self.assertRaises(ToolError):
            self.config.get_mt_key()
        atomic_bytes(self.config.data / "mt-key.txt", b"arbitrary.token:+/=123")
        self.assertEqual(self.config.get_mt_key(), "arbitrary.token:+/=123")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "other.token:456", "DASHSCOPE_API_KEY": "asr-env"}):
            self.assertEqual(self.config.get_mt_key(), "other.token:456")
            self.assertEqual(self.config.get_key(), "asr-env")
        self.config.settings.mt_provider = "openai_compatible"
        self.config.settings.mt_model = "saved-model"
        self.config.save()
        with mock.patch.dict(os.environ, {"MT_MODEL": "env-model", "MT_BASE_URL": "https://example.test/prefix/v1/", "MT_RPM": "12"}):
            backend = configured_backend(Config(self.path), self.manifest)
            self.assertEqual((backend.model, backend.base_url), ("env-model", "https://example.test/prefix/v1"))
            self.assertEqual(self.config.mt_rpm(), 12)
        self.assertEqual(Config(self.path).settings.mt_model, "saved-model")
        settings = (self.config.data / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("arbitrary.token", settings)

    def test_missing_provider_model_key_and_bad_rpm_fail_before_new_enqueue_or_asr(self):
        source = self.path / "new.mkv"
        source.write_bytes(b"no media needed")
        self.config.settings.workspace_id = "offline-space"
        cases = [("unknown", "model", 0, "key"), ("openai_compatible", "", 0, "key"),
                 ("openai_compatible", "model", 0, ""), ("openai_compatible", "model", -1, "key"),
                 ("openai_compatible", "model", True, "key"), ("openai_compatible", "model", 1.5, "key")]
        for provider, model, rpm, key in cases:
            with self.subTest(provider=provider, model=model, rpm=rpm, key_present=bool(key)):
                self.config.settings.mt_provider, self.config.settings.mt_model, self.config.settings.mt_rpm = provider, model, rpm
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": key}), \
                        mock.patch("subtitle_tool.main.Config", return_value=self.config), \
                        mock.patch("requests.request") as request, self.assertRaises(ToolError):
                    main([str(source), "--non-interactive"])
                request.assert_not_called()
        with mock.patch.dict(os.environ, {"MT_RPM": "nan"}), self.assertRaises(ToolError):
            self.config.mt_rpm()

    def test_urls_preserve_prefix_and_normalize_host_port_and_slashes(self):
        for value, expected in [("https://EXAMPLE.test:443/vendor/api/v1///", "https://example.test/vendor/api/v1"),
                                ("http://localhost:8080/v1/", "http://localhost:8080/v1"),
                                ("http://127.0.0.2/v1", "http://127.0.0.2/v1"),
                                ("http://[::1]:8000/v1/", "http://[::1]:8000/v1")]:
            with self.subTest(value=value):
                self.assertEqual(normalize_mt_url(value), expected)
        for value in ["", None, "http://example.test/v1", "ftp://localhost/v1", "https://user:password@example.test/v1",
                      "https://example.test/v1?token=secret", "https://example.test/v1?", "https://example.test/v1#",
                      "https://example.test/v1/chat/completions/", "https://example.test/v1/completions",
                      "https://example.test/v1/chat/%63ompletions", "https://example.test:bad/v1", "https://[broken",
                      "https://example.test/a/../v1", "https://example.test/a/%2e%2e/v1", "https://example.test\\@other.test/v1",
                      "https://example.test/v1\n"]:
            with self.subTest(value=value), self.assertRaises(ToolError) as error:
                normalize_mt_url(value)
            self.assertNotIn("password", str(error.exception))
            self.assertNotIn("secret", str(error.exception))
