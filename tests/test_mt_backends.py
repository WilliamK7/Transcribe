from __future__ import annotations

import copy
import io
import json
import os
import threading
from dataclasses import replace
from unittest import mock

import requests

from subtitle_tool.api import Http, OpenAICompatible, RequestPacer, mt_pacer
from subtitle_tool.common import Cancelled, MT_INPUT_BUDGET, MT_MODEL, ToolError, atomic_bytes
from subtitle_tool.config import Config
from subtitle_tool.engine import Engine
from subtitle_tool.main import main, summary
from subtitle_tool.mt import batch_cache_key, batch_input_size
from subtitle_tool.mt_backend import MTBackend, configured_mt, key_fingerprint, normalize_mt_url, saved_mt
from subtitle_tool.policy import asr_contract, load_policy
from subtitle_tool.subtitles import normalize, plan_reference_units, plan_translation_batches
from tests.fixtures import FakeService, PipelineFixture, SavedResultFixture, TemporaryTest, response, result, timed_raw
from tests.test_translation_v3 import batch_input, batch_response


class MTConfigurationTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        self.config.set_key("separate-bailian-key")
        self.manifest = {"region": "beijing", "access_mode": "workspace", "workspace_id": "offline-space"}

    def test_defaults_and_independent_environment_file_credentials(self):
        backend = configured_mt(self.config, self.manifest)
        self.assertEqual((backend.provider, backend.model), ("bailian", MT_MODEL))
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
            backend = configured_mt(Config(self.path), self.manifest)
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

    def test_asr_contract_accepts_historical_mixed_and_new_separated_contract(self):
        clean = asr_contract()
        self.assertNotIn("mt", clean)
        self.assertEqual(load_policy({"contract": clean}).normalizer_version, clean["normalizer"])
        self.assertEqual(load_policy({"contract": {**clean, "mt": MT_MODEL}}).normalizer_version, clean["normalizer"])
        with self.assertRaises(ToolError):
            load_policy({"contract": {**clean, "mt": "unknown"}})


class CompatibleProtocolTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.key = "opaque.token+/=arbitrary-secret"
        self.backend = MTBackend("openai_compatible", "https://mt.example.test/custom/v1", "custom-model", key_fingerprint(self.key))
        self.cancel = threading.Event()
        self.cloud = OpenAICompatible(self.key, self.backend, self.cancel)

    def test_basic_fields_independent_bearer_and_simplification(self):
        with mock.patch("requests.request", return_value=batch_response(["繁體譯文"])) as request:
            self.assertEqual(self.cloud.translate_batch(["Input"]).translations, {"0": "繁体译文"})
        call = request.call_args
        self.assertEqual(call.args, ("POST", self.backend.base_url + "/chat/completions"))
        self.assertEqual(set(call.kwargs["json"]), {"model", "messages", "stream"})
        self.assertEqual(call.kwargs["json"]["model"], "custom-model")
        self.assertIs(call.kwargs["json"]["stream"], False)
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer " + self.key)
        self.assertFalse(call.kwargs["allow_redirects"])

    def test_repair_and_recursive_split_use_same_minimal_request_and_budget(self):
        calls, events = [], []
        def handler(*args, **kwargs):
            body = kwargs["json"]
            calls.append(body)
            targets = batch_input(body)
            return batch_response(["缺项"] if len(targets) > 1 else ["單片"])
        with mock.patch("requests.request", side_effect=handler):
            translated = self.cloud.translate_batch(["One", "Two"], context=[{"position": "before", "text": "Background"}],
                                                     on_event=events.append)
        self.assertTrue(translated.recovered)
        self.assertEqual(translated.translations, {"0": "单片", "1": "单片"})
        self.assertEqual(len(calls), 4)
        for body in calls:
            self.assertEqual(set(body), {"model", "messages", "stream"})
            self.assertEqual(body["model"], self.backend.model)
            self.assertLessEqual(len(json.dumps(body, ensure_ascii=False).encode()) + 512, MT_INPUT_BUDGET)
        self.assertTrue(any("上次响应" in body["messages"][0]["content"] for body in calls))
        self.assertTrue(all(event.get("request_id", "") == "" for event in events))

    def test_http_errors_are_static_even_if_code_id_and_body_contain_credentials_or_source(self):
        secret = "arbitrary-unprefixed-key-and-private-input"
        for status in [301, 400, 401, 403, 404, 429, 500, 503]:
            with self.subTest(status=status):
                cloud = OpenAICompatible(self.key, self.backend, self.cancel)
                cloud.http.attempts = 1
                with mock.patch("requests.request", return_value=response({"error": {"code": secret, "message": secret},
                        "code": secret, "request_id": secret}, status, {"Retry-After": "0"})), self.assertRaises(ToolError) as error:
                    cloud.translate_batch([secret])
                self.assertEqual(error.exception.code, f"mt_http_{status}")
                self.assertEqual(error.exception.request_id, "")
                self.assertNotIn(secret, str(error.exception))

    def test_business_error_in_success_status_is_safe(self):
        with mock.patch("requests.request", return_value=response({"code": self.key, "error": {"message": "private input"}})), \
                self.assertRaises(ToolError) as error:
            self.cloud.translate_batch(["private input"])
        self.assertEqual(error.exception.code, "mt_service_error")
        self.assertNotIn(self.key, str(error.exception))

    def test_network_timeouts_retry_and_cancellation_prevents_next_attempt(self):
        with mock.patch("requests.request", side_effect=requests.Timeout(self.key)) as request, \
                mock.patch.object(Http, "pause"), self.assertRaises(ToolError) as error:
            self.cloud.translate_batch(["private input"])
        self.assertEqual(request.call_count, 4)
        self.assertEqual(error.exception.code, "network")
        self.assertNotIn(self.key, str(error.exception))
        self.cancel.set()
        with mock.patch("requests.request") as request, self.assertRaises(Cancelled):
            self.cloud.translate_batch(["private input"])
        request.assert_not_called()

    def test_invalid_json_and_uuid_shaped_secret_never_enter_events_or_errors(self):
        secret = "01234567-89ab-cdef-0123-456789abcdef"
        events = []
        with mock.patch("requests.request", side_effect=lambda *a, **k: response({"id": secret, "choices": [
                {"finish_reason": "stop", "message": {"content": secret}}]})), self.assertRaises(ToolError) as error:
            self.cloud.translate_batch(["private input"], on_event=events.append)
        self.assertEqual(error.exception.code, "mt_batch_invalid")
        self.assertNotIn(secret, json.dumps(events) + str(error.exception) + error.exception.request_id)

    def test_model_changes_budget_planning_and_actual_request_together(self):
        backend = replace(self.backend, model="m" * 1500)
        options = backend.request_options()
        raw = timed_raw(["word" * 120] * 8, word_ms=1000)
        source = normalize(raw, 0, "0000", preserve_sentences=True)[0][0]
        units = plan_reference_units([source], strategy_version=3, request_options=options)
        groups = plan_translation_batches(units, [{"position": "before", "text": "context" * 1000}], request_options=options)
        seen = []
        cloud = OpenAICompatible(self.key, backend, self.cancel)
        def handler(*a, **k):
            seen.append(k["json"])
            return batch_response(["译文"] * len(batch_input(k["json"])))
        with mock.patch("requests.request", side_effect=handler):
            for members, context in groups:
                texts = [member["text"] for member in members]
                self.assertLessEqual(batch_input_size(texts, context=context, request_options=options), MT_INPUT_BUDGET)
                cloud.translate_batch(texts, context=context)
        self.assertTrue(seen)
        self.assertTrue(all(body["model"] == backend.model for body in seen))
        cloud = OpenAICompatible(self.key, replace(backend, model="m" * 7000), self.cancel)
        with mock.patch("requests.request") as request, self.assertRaises(ToolError) as error:
            cloud.translate_batch(["tiny input"])
        request.assert_not_called()
        self.assertEqual(error.exception.code, "mt_input_budget")

    def test_rolling_rpm_retry_quota_and_shared_identity_isolation(self):
        now = [0.0]
        cancel = mock.Mock()
        cancel.is_set.return_value = False
        def advance(seconds):
            now[0] += seconds
            return False
        cancel.wait.side_effect = advance
        pacer = RequestPacer(clock=lambda: now[0], requests_per_window=2, window_seconds=60)
        client = Http(cancel, pacer=pacer, safe_errors=True)
        starts = []
        def handler(*a, **k):
            starts.append(now[0])
            return response({}, 429, {"Retry-After": "0"}) if len(starts) == 2 else response({"ok": True})
        with mock.patch("requests.request", side_effect=handler):
            client.json("POST", self.backend.base_url, name="mt")
            client.json("POST", self.backend.base_url, name="mt")
        self.assertEqual(starts[:2], [0, 0])
        self.assertAlmostEqual(starts[2], 60)
        first = mt_pacer(self.backend, rpm=2)
        self.assertIs(first, mt_pacer(self.backend, rpm=0))
        self.assertEqual(first.requests_per_window, 0)
        for backend in [replace(self.backend, model="other"), replace(self.backend, base_url="https://other.test/v1"),
                        replace(self.backend, key_fingerprint=key_fingerprint("another-key")),
                        replace(self.backend, provider="bailian")]:
            self.assertIsNot(first, mt_pacer(backend, rpm=2))
        unlimited = RequestPacer(clock=lambda: now[0], requests_per_window=0)
        for _ in range(150):
            unlimited.wait(cancel)
        self.assertAlmostEqual(now[0], 60)


class CompatibleRecoveryTests(SavedResultFixture):
    def setUp(self):
        super().setUp()
        self.config.settings.mt_provider = "openai_compatible"
        self.config.settings.mt_base_url = "https://mt.example.test/prefix/v1/"
        self.config.settings.mt_model = "saved-model"
        atomic_bytes(self.config.data / "mt-key.txt", b"opaque-mt-key")

    def install_snapshot(self, raw=None):
        sources = self.save_result(raw)
        manifest = self.store.job(self.job_id)["manifest"]
        Engine(self.config, self.store).identity(self.job_id, manifest)
        manifest["mt_snapshot"] = configured_mt(self.config, manifest).snapshot()
        self.store.update(self.job_id, manifest=manifest)
        return sources

    def test_resume_routes_using_saved_backend_despite_invalid_current_selection(self):
        self.install_snapshot()
        self.config.settings.mt_provider = "unavailable-current-provider"
        self.config.settings.mt_base_url = "not a url"
        self.config.settings.mt_model = ""
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertTrue(fake.calls)
        self.assertTrue(all(call[1] == "https://mt.example.test/prefix/v1/chat/completions" for call in fake.calls))
        self.assertTrue(all(call[2]["json"]["model"] == "saved-model" for call in fake.calls))
        self.assertTrue(all(call[2]["headers"]["Authorization"] == "Bearer opaque-mt-key" for call in fake.calls))
        item = summary(self.store, self.job_id)
        self.assertEqual((item["mt_provider"], item["models"]["mt"]), ("openai_compatible", "saved-model"))
        self.assertNotIn("opaque-mt-key", json.dumps(item))

    def test_format_diagnostics_identify_the_saved_compatible_provider(self):
        self.install_snapshot()
        with mock.patch('requests.request', return_value=response({'choices': [
                {'finish_reason': 'stop', 'message': {'content': 'invalid json'}}]})):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'failed')
        path = self.config.root / '.build' / 'mt-diagnostics' / (self.job_id + '.jsonl')
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(events)
        self.assertEqual({event['provider'] for event in events}, {'openai_compatible'})

    def test_changed_mt_key_blocks_cached_results_and_unpublished_intent(self):
        self.install_snapshot()
        fake = FakeService(result())
        def stop_before_publish(*args, **kwargs):
            raise KeyboardInterrupt()
        with mock.patch("subtitle_tool.engine.write_srt", side_effect=stop_before_publish):
            self.assertEqual(self.run_fake(fake), "cancelled")
        self.assertTrue(self.store.job(self.job_id)["manifest"].get("publication_intent"))
        self.assertTrue(all(c["translated"] for c in self.store.cues(self.job_id)))
        atomic_bytes(self.config.data / "mt-key.txt", b"new-independent-key")
        self.store.update(self.job_id, "queued")
        fake.calls.clear()
        with mock.patch.object(self.store, "cache_get", side_effect=AssertionError("must validate first")):
            self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "mt_account_changed")
        self.assertFalse(self.source.with_suffix(".srt").exists())
        self.assertEqual(fake.calls, [])

    def test_already_published_only_completion_requires_original_asr_identity(self):
        self.install_snapshot()
        fake = FakeService(result())
        from subtitle_tool.publication import write_srt
        def interrupt(*args, **kwargs):
            write_srt(*args, **kwargs)
            raise KeyboardInterrupt()
        with mock.patch("subtitle_tool.engine.write_srt", side_effect=interrupt):
            self.assertEqual(self.run_fake(fake), "cancelled")
        self.assertTrue(self.source.with_suffix(".srt").exists())
        (self.config.data / "mt-key.txt").unlink()
        self.store.update(self.job_id, "queued")
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(fake.calls, [])

    def test_reflow_switches_without_old_mt_key_and_keeps_asr_and_manual_decisions(self):
        self.install_snapshot(result(("Unknown words.", "")))
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), "needs_review")
        from subtitle_tool.review import ReviewDecision
        self.store.apply_review(self.job_id, self.store.cues(self.job_id)[0]["cue_id"], ReviewDecision("translate", "English"))
        self.store.update(self.job_id, "queued")
        self.assertEqual(self.run_fake(fake), "completed")
        before = self.store.job(self.job_id)["manifest"]
        old_output = self.source.with_suffix(".srt").read_bytes()
        self.config.settings.mt_base_url = "https://new.example.test/another/v1"
        self.config.settings.mt_model = "new-model"
        atomic_bytes(self.config.data / "mt-key.txt", b"new-independent-key")
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        prepared = self.store.job(self.job_id)["manifest"]
        for name in ["account", "cache_identity", "segments", "fingerprint", "track", "channel"]:
            self.assertEqual(prepared[name], before[name])
        self.assertEqual(self.store.sources(self.job_id)[0]["source_lang"], "English")
        self.assertEqual(self.store.sources(self.job_id)[0]["decision_origin"], "manual")
        fake.calls.clear()
        fake.mt_failure.add("Unknown words.")
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.source.with_suffix(".srt").read_bytes(), old_output)
        self.assertTrue(all(call[1] == "https://new.example.test/another/v1/chat/completions" for call in fake.calls))
        fake.mt_failure.clear()
        self.config.settings.mt_provider = "bailian"
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertTrue(all(call[2]["json"]["model"] == "new-model" for call in fake.calls))
        self.assertEqual(fake.named("/asr/transcription"), [])

    def test_reflow_preparation_failure_preserves_original_snapshot_records_and_output(self):
        self.install_snapshot()
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), "completed")
        before = self.store.job(self.job_id)
        records = self.store.sources(self.job_id), self.store.cues(self.job_id), self.store.references(self.job_id)
        old = self.source.with_suffix(".srt").read_bytes()
        self.config.settings.mt_model = ""
        with self.assertRaises(ToolError):
            Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual((self.store.sources(self.job_id), self.store.cues(self.job_id), self.store.references(self.job_id)), records)
        self.assertEqual(self.source.with_suffix(".srt").read_bytes(), old)

    def test_historical_missing_snapshot_ignores_current_compatible_configuration_and_key(self):
        self.save_result()
        (self.config.data / "mt-key.txt").unlink()
        self.config.settings.mt_model = ""
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertTrue(all("offline-space.cn-beijing.maas.aliyuncs.com" in call[1] for call in fake.calls))
        self.assertNotIn("mt_snapshot", self.store.job(self.job_id)["manifest"])

    def test_cache_isolated_by_provider_model_address_key_and_preserves_legacy_algorithm(self):
        self.install_snapshot()
        manifest = self.store.job(self.job_id)["manifest"]
        backend = saved_mt(manifest)
        scope = manifest["cache_identity"]
        def key(candidate):
            return batch_cache_key(["same input"], **scope, **candidate.cache_scope(), request_options=candidate.request_options())
        original = key(backend)
        for candidate in [replace(backend, model="other-model"), replace(backend, base_url="https://other.test/v1"),
                          replace(backend, key_fingerprint=key_fingerprint("other-key")),
                          replace(backend, provider="bailian", model=MT_MODEL)]:
            self.assertNotEqual(key(candidate), original)
        legacy = saved_mt({k: v for k, v in manifest.items() if k != "mt_snapshot"})
        self.assertEqual(key(legacy), batch_cache_key(["same input"], **scope))
        self.config.settings.mt_workers = 1
        self.config.settings.mt_rpm = 27
        self.assertEqual(configured_mt(self.config, manifest), backend)

    def test_new_enqueue_refuses_different_mt_config_before_mutating_existing_job(self):
        self.install_snapshot()
        before = self.store.job(self.job_id)
        other = replace(saved_mt(before["manifest"]), model="other-model")
        with self.assertRaises(ToolError) as error:
            self.store.enqueue(self.source, workspace_id="offline-space", account=before["manifest"]["account"],
                               overwrite=True, mt_snapshot=other.snapshot())
        self.assertEqual(error.exception.code, "mt_config_changed")
        self.assertIn("--resume", str(error.exception))
        self.assertIn("--reflow", str(error.exception))
        self.assertEqual(self.store.job(self.job_id), before)

    def test_snapshot_version_and_strategy_validation_precedes_cache_and_network(self):
        self.install_snapshot()
        original = self.store.job(self.job_id)["manifest"]
        for change in [{"request_version": 999}, {"format_version": True}, {"provider": "other"},
                       {"key_fingerprint": "raw-key"}, {"base_url": "https://bad.test/v1?secret=private"}]:
            manifest = copy.deepcopy(original)
            manifest["mt_snapshot"].update(change)
            self.store.update(self.job_id, "queued", manifest)
            with mock.patch.object(self.store, "cache_get", side_effect=AssertionError("no cache")), \
                    mock.patch("requests.request") as request:
                self.assertEqual(Engine(self.config, self.store).run(self.job_id), "failed")
            request.assert_not_called()
        bad = copy.deepcopy(original)
        bad["translation_strategy_version"] = 2
        with self.assertRaises(ToolError) as error:
            saved_mt(bad)
        self.assertEqual(error.exception.code, "cache_version")

    def test_concurrent_success_is_saved_when_another_compatible_request_fails(self):
        self.install_snapshot(result(("First sentence.", "en"), ("Second sentence.", "en")))
        self.config.settings.mt_workers = 2
        entered = threading.Barrier(2)
        def handler(method, url, **kwargs):
            text = batch_input(kwargs["json"])[0]["text"]
            entered.wait(timeout=5)
            return batch_response(["完成一条"]) if text.startswith("First") else response({"error": {"code": "private-input"}}, 403)
        with mock.patch("requests.request", side_effect=handler):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), "failed")
        references = self.store.references(self.job_id)
        self.assertEqual([bool(r["translated"]) for r in references], [True, False])
        self.assertEqual(self.store.counts(self.job_id)["mt_reference_completed"], 1)
        self.assertNotIn("private-input", self.store.job(self.job_id)["error"])
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.calls), 1)


class BackendRoutingPipelineTests(PipelineFixture):
    def test_uploaded_asr_scope_survives_translation_failure_resume_and_backend_switch(self):
        self.config.settings.mt_provider = "openai_compatible"
        self.config.settings.mt_model = "chosen-model"
        atomic_bytes(self.config.data / "mt-key.txt", b"independent-mt-token")
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)["manifest"]
        engine.identity(self.job_id, manifest)
        manifest["mt_snapshot"] = configured_mt(self.config, manifest).snapshot()
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(result(("Hello.", "en")))
        fake.mt_failure.add("Hello.")
        self.assertEqual(self.run_fake(fake), "failed")
        fake.mt_failure.clear()
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(fake.named("/asr/transcription"), [])
        manifest = self.store.job(self.job_id)["manifest"]
        self.assertEqual(manifest["segments"][0]["upload_identity"], manifest["cache_identity"])
        self.assertNotIn("mt_identity", manifest["cache_identity"])
        self.config.settings.mt_provider = "bailian"
        (self.config.data / "mt-key.txt").unlink()
        engine.reflow(self.job_id, overwrite=True)
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertTrue(fake.calls)
        self.assertTrue(all("offline-space.cn-beijing.maas.aliyuncs.com" in call[1] for call in fake.calls))
        self.assertEqual(fake.named("/asr/transcription"), [])
        self.assertEqual(len(fake.uploads), 1)

    def test_cli_creates_snapshot_and_routes_asr_and_mt_with_separate_keys(self):
        # Use a different target because the fixture also contains a historical job.
        self.config.settings.mt_provider = "openai_compatible"
        self.config.settings.mt_base_url = "https://mt.example.test/prefix/v1/"
        self.config.settings.mt_model = "chosen-model"
        atomic_bytes(self.config.data / "mt-key.txt", b"independent-mt-token")
        fake = FakeService(result(("Hello.", "en")))
        with mock.patch("subtitle_tool.main.Config", return_value=self.config), \
                mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", new_callable=io.StringIO), \
                mock.patch("requests.request", side_effect=fake.request), mock.patch("requests.post", side_effect=fake.upload):
            self.assertEqual(main([str(self.source), "--non-interactive", "--output-dir", str(self.path / "new-target")]), 0)
        submits, translations = fake.named("/asr/transcription"), fake.named("/chat/completions")
        self.assertEqual(len(submits), 1)
        self.assertTrue(translations)
        self.assertEqual(submits[0][2]["headers"]["Authorization"], "Bearer offline-test-key-no-cloud-authorization")
        self.assertTrue(all(call[2]["headers"]["Authorization"] == "Bearer independent-mt-token" for call in translations))
        self.assertTrue(all(call[1] == "https://mt.example.test/prefix/v1/chat/completions" for call in translations))
        job = self.store.jobs()[-1]
        self.assertEqual(job["manifest"]["mt_snapshot"]["model"], "chosen-model")
        self.assertNotIn("mt", job["manifest"]["contract"])
        self.assertNotIn("independent-mt-token", json.dumps(job))
