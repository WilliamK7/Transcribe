from __future__ import annotations

import copy
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from tests.fixtures import FakeService, SavedResultFixture, result


class ExecutionPolicyTests(SavedResultFixture):
    def test_mismatch_stops_before_cache_cloud_and_publishing(self):
        from subtitle_tool.policy import current_execution_snapshot
        self.save_result()
        original = self.store.job(self.job_id)["manifest"]
        paths = [(field,) for field in ['processing_version', 'subtitle_version', 'display_timing_version',
                                        'translation_strategy_version', 'normalized_version']]
        paths += [('execution_snapshot', field) for field in current_execution_snapshot()]
        paths += [('contract', 'normalizer'), ('media_plan', 'extract_recipe_version')]
        invalid = [(path, value) for path in paths for value in [0, 999, True, '2', None]]
        invalid.append((('mt_glossary_schedule',), 'unsupported'))
        for path, value in invalid:
            with self.subTest(path=path, value=value):
                manifest = copy.deepcopy(original)
                target = manifest
                for name in path[:-1]:
                    target = target.setdefault(name, {})
                target[path[-1]] = value
                self.store.update(self.job_id, "queued", manifest)
                fake = FakeService(result())
                with mock.patch.object(self.store, "cache_get", side_effect=AssertionError("no cache")):
                    self.assertEqual(self.run_fake(fake), "failed")
                job = self.store.job(self.job_id)
                self.assertEqual(job["error_code"], "cache_version")
                self.assertIn("--reflow", job["error"])
                self.assertEqual(fake.calls, [])
                self.assertFalse(self.source.with_suffix(".srt").exists())

    def test_reflow_refreshes_snapshot_only_after_successful_preparation(self):
        from subtitle_tool.policy import current_execution_snapshot
        self.save_result()
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["execution_snapshot"] = {"format_version": 1, "prompt_version": -1, "recovery_version": -1}
        self.store.update(self.job_id, manifest=manifest)
        before = self.store.job(self.job_id)
        raw = self.cache / "raw-0000.json"
        content = raw.read_bytes()
        raw.unlink()
        with self.assertRaises(ToolError):
            Engine(self.config, self.store).reflow(self.job_id)
        self.assertEqual(self.store.job(self.job_id), before)
        raw.write_bytes(content)
        Engine(self.config, self.store).reflow(self.job_id)
        self.assertEqual(self.store.job(self.job_id)["manifest"]["execution_snapshot"], current_execution_snapshot())

    def test_resume_uses_saved_layout_and_current_concurrency(self):
        from subtitle_tool.policy import load_policy
        manifest = self.store.job(self.job_id)["manifest"]
        policy = load_policy(manifest)
        self.config.settings.mt_workers = 1
        self.config.settings.zh_line_width = 12
        settings = policy.settings(self.config.settings)
        self.assertEqual(settings.mt_workers, 1)
        self.assertEqual(settings.zh_line_width, manifest["subtitle_settings"]["zh_line_width"])
