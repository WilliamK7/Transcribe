from __future__ import annotations

import copy
import io
import json
import time
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.main import hint, main, summary
from tests.fixtures import FakeService, SavedResultFixture, result, seed_job_state


class LifecycleTests(SavedResultFixture):
    def unknown(self):
        self.save_result(normalized=False)
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["segments"][0].pop("task_id")
        manifest["segments"][0].pop("raw")
        manifest["segments"][0]["submitting"] = True
        self.store.update(self.job_id, "submitting", manifest)
        return Engine(self.config, self.store)

    def test_startup_and_explicit_resume_never_resend_unknown_submission(self):
        engine = self.unknown()
        engine.recover_jobs()
        engine.resume(self.job_id)
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), 'submission_unknown')
        self.assertEqual(fake.calls, [])

    def test_cancelled_unknown_can_be_bound_only_on_explicit_resume(self):
        engine = self.unknown()
        engine.cancel_job(self.job_id)
        self.assertEqual(self.store.job(self.job_id)["state"], "cancelled")
        engine.recover_jobs()
        self.assertEqual(self.store.job(self.job_id)["state"], "cancelled")
        engine.resume(self.job_id)
        self.assertEqual(self.store.job(self.job_id)["state"], "submission_unknown")
        engine.resolve_unknown(self.job_id, task_id="offline-bound")
        manifest = self.store.job(self.job_id)["manifest"]
        self.assertEqual(manifest["segments"][0]["task_id"], "offline-bound")
        self.assertFalse(manifest["segments"][0].get("submitting"))

    def test_resubmit_is_explicit_and_ambiguous_multiple_segments_stay_blocked(self):
        engine = self.unknown()
        engine.recover_jobs()
        with self.assertRaises(ToolError):
            engine.resolve_unknown(self.job_id)
        engine.resolve_unknown(self.job_id, allow_resubmit=True)
        self.assertEqual(self.store.job(self.job_id)["state"], "queued")
        engine = self.unknown()
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["segments"].append({**manifest["segments"][0], "id": "0001"})
        self.store.update(self.job_id, "submitting", manifest)
        engine.recover_jobs()
        with self.assertRaises(ToolError):
            engine.resolve_unknown(self.job_id, allow_resubmit=True)

    def test_late_error_preserves_new_cloud_task_id_over_old_database_copy(self):
        engine = self.unknown()
        manifest = self.store.job(self.job_id)["manifest"]
        # The newly returned cloud fact must survive the failing first persistence call.
        manifest["segments"][0].update(audio="audio.flac", submitting=False, oss_url="oss://offline/audio.flac",
                                       upload_expires=time.time() + 3600)
        self.store.update(self.job_id, manifest=manifest)
        engine.recover_jobs()
        update = self.store.update
        injected = []
        def fail_once(job_id, state=None, *args, **kwargs):
            if state == "recognizing" and not injected:
                injected.append(True)
                raise OSError()
            return update(job_id, state, *args, **kwargs)
        fake = FakeService(result(("保存。", "zh")))
        (self.cache / "raw-0000.json").unlink()
        with mock.patch.object(self.store, "update", side_effect=fail_once):
            self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["manifest"]["segments"][0]["task_id"], "offline-task")
        self.assertEqual(len(fake.named("/asr/transcription")), 1)

    def test_completed_cannot_be_downgraded_and_manifest_is_atomic(self):
        self.store.update(self.job_id, 'completed')
        before = self.store.job(self.job_id)
        candidate = copy.deepcopy(before['manifest'])
        candidate['warnings'].append('must not be saved')
        with self.assertRaises(ToolError) as caught:
            self.store.update(self.job_id, 'failed', candidate, code='local_io')
        self.assertEqual(caught.exception.code, 'invalid_transition')
        self.assertEqual(self.store.job(self.job_id), before)

    def test_stopped_job_requires_explicit_resume_before_progress(self):
        engine = Engine(self.config, self.store)
        engine.cancel_job(self.job_id)
        before = self.store.job(self.job_id)
        with self.assertRaises(ToolError) as caught:
            self.store.update(self.job_id, 'uploading')
        self.assertEqual(caught.exception.code, 'invalid_transition')
        self.assertEqual(self.store.job(self.job_id), before)
        engine.resume(self.job_id)
        self.store.update(self.job_id, 'probing')
        self.assertEqual(self.store.job(self.job_id)['state'], 'probing')

    def test_unknown_submission_cannot_be_cleared_by_generic_update(self):
        manifest = self.store.job(self.job_id)['manifest']
        manifest['segments'] = [{'id': '0000', 'submitting': True}]
        self.store.update(self.job_id, 'submitting', manifest)
        Engine(self.config, self.store).recover_jobs()
        before = self.store.job(self.job_id)
        candidate = copy.deepcopy(before['manifest'])
        candidate['segments'][0]['submitting'] = False
        with self.assertRaises(ToolError) as caught:
            self.store.update(self.job_id, 'queued', candidate)
        self.assertEqual(caught.exception.code, 'invalid_transition')
        self.assertEqual(self.store.job(self.job_id), before)

    def test_resume_event_cannot_erase_unknown_fact_and_resolution_keeps_segment(self):
        manifest = self.store.job(self.job_id)['manifest']
        manifest['segments'] = [{'id': '0000', 'submitting': True}]
        self.store.update(self.job_id, 'submitting', manifest)
        engine = Engine(self.config, self.store)
        engine.cancel_job(self.job_id)
        before = self.store.job(self.job_id)
        candidate = copy.deepcopy(manifest)
        candidate['segments'] = []
        with self.assertRaises(ToolError) as caught:
            self.store.update(self.job_id, 'queued', candidate, event='resume')
        self.assertEqual(caught.exception.code, 'invalid_transition')
        self.assertEqual(self.store.job(self.job_id), before)
        engine.resume(self.job_id)
        before = self.store.job(self.job_id)
        with self.assertRaises(ToolError) as caught:
            self.store.update(self.job_id, 'queued', candidate, event='resolve_submission')
        self.assertEqual(caught.exception.code, 'invalid_transition')
        self.assertEqual(self.store.job(self.job_id), before)
        engine.resolve_unknown(self.job_id, task_id='known-task')
        self.assertEqual(self.store.job(self.job_id)['manifest']['segments'][0]['task_id'], 'known-task')

    def test_repeated_recovery_and_resume_preserve_unknown_diagnostics(self):
        manifest = self.store.job(self.job_id)['manifest']
        manifest['segments'] = [{'id': '0000', 'submitting': True}]
        self.store.update(self.job_id, 'submission_unknown', manifest,
                          error='saved diagnostic', code='submission_unknown', request_id='request-123')
        before = self.store.job(self.job_id)
        engine = Engine(self.config, self.store)
        for _ in range(2):
            engine.recover_jobs()
            engine.resume(self.job_id)
            self.assertEqual(engine.run(self.job_id), 'submission_unknown')
            self.assertEqual(self.store.job(self.job_id), before)

    def test_republish_transactions_require_prepared_evidence(self):
        sources = self.save_result()
        self.store.update(self.job_id, 'completed')
        before = self.store.job(self.job_id)
        cues = self.store.cues(self.job_id)
        manifest = before['manifest']
        for operation, args in [(self.store.commit_reflow, ([], [], manifest, self.path / 'new.srt')),
                                (self.store.commit_retime, (manifest, self.path / 'new.srt'))]:
            with self.subTest(operation=operation.__name__), self.assertRaises(ToolError) as caught:
                operation(self.job_id, *args)
            self.assertEqual(caught.exception.code, 'invalid_transition')
            self.assertEqual(self.store.job(self.job_id), before)
            self.assertEqual(self.store.cues(self.job_id), cues)
            self.assertEqual(self.store.sources(self.job_id), sources)

    def test_archived_record_cannot_be_reopened_in_place(self):
        manifest = self.store.job(self.job_id)['manifest']
        self.store.update(self.job_id, 'completed')
        manifest['archived'] = True
        seed_job_state(self.store, self.job_id, 'archived', manifest)
        before = self.store.job(self.job_id)
        for event in ('run', 'resume', 'recover', 'reflow', 'retime'):
            with self.subTest(event=event), self.assertRaises(ToolError) as caught:
                self.store.update(self.job_id, 'queued', event=event)
            self.assertEqual(caught.exception.code, 'invalid_transition')
            self.assertEqual(self.store.job(self.job_id), before)
        actions = summary(self.store, self.job_id)['lifecycle']['next_actions']
        self.assertEqual([action['id'] for action in actions], ['export_archive', 'reflow'])

    def test_listing_does_not_recover_or_clear_saved_progress(self):
        self.store.update(self.job_id, 'recognizing', request_id='synthetic-request')
        before = self.store.job(self.job_id)
        with mock.patch('subtitle_tool.main.Config', return_value=self.config), \
                mock.patch.object(self.config, 'get_key', side_effect=AssertionError('no credentials')), \
                mock.patch('requests.request', side_effect=AssertionError('no network')), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['--jobs']), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual(report['state'], 'recognizing')
        self.assertEqual(report['lifecycle']['next_actions'][0]['id'], 'resume')

    def test_cancelled_unknown_explains_binding_and_duplicate_charge(self):
        manifest = self.store.job(self.job_id)['manifest']
        manifest['segments'] = [{'id': '0000', 'submitting': True, 'oss_url': 'oss://private-secret'}]
        self.store.update(self.job_id, 'submitting', manifest)
        Engine(self.config, self.store).cancel_job(self.job_id)
        actions = summary(self.store, self.job_id)['lifecycle']['next_actions']
        self.assertEqual([a['id'] for a in actions], ['bind_task_id', 'allow_resubmit'])
        self.assertTrue(actions[1]['requires_confirmation'])
        self.assertNotIn('private-secret', json.dumps(actions))
        with mock.patch('sys.stderr', new_callable=io.StringIO) as output:
            hint(self.store, self.job_id)
        self.assertIn('--bind-task-id', output.getvalue())
        self.assertIn('重复计费', output.getvalue())

    def test_retired_contract_guidance_does_not_repeat_failing_resume(self):
        self.store.update(self.job_id, 'failed', code='cache_version')
        actions = summary(self.store, self.job_id)['lifecycle']['next_actions']
        self.assertEqual([a['id'] for a in actions], ['reflow'])
        self.assertEqual(actions[0]['argv'], ['--reflow', self.job_id])
        with mock.patch('sys.stderr', new_callable=io.StringIO) as output:
            hint(self.store, self.job_id)
        self.assertIn('--reflow', output.getvalue())
        self.assertNotIn('--resume', output.getvalue())

    def test_state_contract_is_available_without_config_database_or_network(self):
        with mock.patch('subtitle_tool.main.Config', side_effect=AssertionError('no configuration')), \
                mock.patch('subtitle_tool.main.Store', side_effect=AssertionError('no database')), \
                mock.patch('requests.request', side_effect=AssertionError('no network')), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['--state-machine']), 0)
        contract = json.loads(output.getvalue())
        self.assertEqual(contract['version'], 1)
        self.assertIn('submission_unknown', contract['states'])
        self.assertIn('resolve_submission', contract['events'])

    def test_state_contract_rejects_task_options_without_loading_configuration(self):
        with mock.patch('subtitle_tool.main.Config', side_effect=AssertionError('no configuration')):
            for args in (['--overwrite'], ['--output-dir', 'new'], ['--report', 'contract.json']):
                with self.subTest(args=args), self.assertRaises(ToolError) as caught:
                    main(['--state-machine', *args])
                self.assertEqual(caught.exception.code, 'input')
