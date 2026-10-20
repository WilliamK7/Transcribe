from __future__ import annotations

import hashlib
import io
import json
from contextlib import chdir
from pathlib import Path
from unittest import mock

from subtitle_tool.common import ToolError, output_key
from subtitle_tool.config import Config
from subtitle_tool.engine import Engine
from subtitle_tool.main import main
from subtitle_tool.store import Store
from tests.fixtures import FakeService, PipelineFixture, TemporaryTest, result


class OutputStoreTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.a, self.b = self.path / 'A', self.path / 'B'
        self.a.mkdir()
        self.b.mkdir()
        self.source = self.a / 'Mixed Case.视频.mkv'
        self.source.write_bytes(b'fixture')
        self.config = Config(self.path)
        self.store = Store(self.config.data / 'jobs.sqlite')

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def enqueue(self, **options):
        return self.store.enqueue(self.source, workspace_id='offline-space', account='offline-account', **options)

    def legacy_job(self):
        with chdir(self.a):
            job_id = self.enqueue(output_dir=Path('SubTitles'))
        relative = Path('SubTitles') / self.source.with_suffix('.srt').name
        with self.store.db:
            self.store.db.execute('UPDATE jobs SET output=? WHERE id=?', (str(relative), job_id))
        return job_id

    def test_enqueue_saves_absolute_output_and_matching_key(self):
        with chdir(self.a):
            job_id = self.enqueue(output_dir=Path('SubTitles'))
        expected = self.a / 'SubTitles' / self.source.with_suffix('.srt').name
        job = self.store.job(job_id)
        self.assertEqual(job['output'], str(expected))
        self.assertEqual(job['output_key'], output_key(expected))
        self.assertFalse(expected.parent.exists())

    def test_matching_legacy_path_is_fixed_before_existing_srt_skip(self):
        job_id = self.legacy_job()
        output = self.a / 'SubTitles' / self.source.with_suffix('.srt').name
        output.parent.mkdir()
        output.write_bytes(b'existing')
        with chdir(self.a), mock.patch('requests.request') as request:
            self.assertEqual(Engine(self.config, self.store).run(job_id), 'skipped')
        self.assertEqual(self.store.job(job_id)['output'], str(output))
        self.assertEqual(self.store.job(job_id)['output_key'], output_key(output))
        self.assertEqual(output.read_bytes(), b'existing')
        request.assert_not_called()

    def test_ambiguous_legacy_resume_stops_before_output_preparation_or_network(self):
        job_id = self.legacy_job()
        before = self.store.job(job_id)
        with chdir(self.b), mock.patch.object(Engine, 'prepare_output') as prepare, mock.patch('requests.request') as request:
            self.assertEqual(self.store.jobs(), [before])
            self.assertEqual(Engine(self.config, self.store).run(job_id), 'failed')
        job = self.store.job(job_id)
        self.assertEqual(job['error_code'], 'output_path_required')
        self.assertIn('--output-dir', job['error'])
        self.assertEqual((job['output'], job['output_key'], job['manifest']),
                         (before['output'], before['output_key'], before['manifest']))
        self.assertFalse((self.b / 'SubTitles').exists())
        prepare.assert_not_called()
        request.assert_not_called()

    def test_ambiguous_change_output_without_directory_preserves_task(self):
        job_id = self.legacy_job()
        before = self.store.job(job_id)
        with chdir(self.b), self.assertRaises(ToolError) as error:
            self.store.change_output(job_id, overwrite=True)
        self.assertEqual(error.exception.code, 'output_path_required')
        self.assertEqual(self.store.job(job_id), before)

    def test_explicit_directory_resolves_ambiguous_task_atomically(self):
        job_id = self.legacy_job()
        with chdir(self.b):
            self.store.change_output(job_id, Path('chosen'), overwrite=True)
        expected = self.b / 'chosen' / self.source.with_suffix('.srt').name
        job = self.store.job(job_id)
        self.assertEqual(job['output'], str(expected))
        self.assertEqual(job['output_key'], output_key(expected))
        self.assertTrue(job['manifest']['overwrite'])
        self.assertFalse(expected.parent.exists())

    def test_change_output_conflict_preserves_legacy_path_and_overwrite(self):
        job_id = self.legacy_job()
        other = self.source.with_suffix('.mp4')
        other.write_bytes(b'other')
        self.store.enqueue(other, output_dir=self.b / 'chosen', workspace_id='offline-space', account='offline-account')
        before = self.store.job(job_id)
        with chdir(self.b), self.assertRaises(ToolError) as error:
            self.store.change_output(job_id, Path('chosen'), overwrite=True)
        self.assertEqual(error.exception.code, 'output_conflict')
        self.assertEqual(self.store.job(job_id), before)

    def test_reuse_promotes_overwrite_without_changing_saved_work_or_state(self):
        for state in ['failed', 'needs_review']:
            with self.subTest(state=state):
                job_id = self.enqueue(output_dir=self.a / state)
                manifest = self.store.job(job_id)['manifest']
                manifest.update(segments=[{'task_id': 'offline-task'}], references_planned=True)
                self.store.update(job_id, state, manifest, error='saved error', code='saved')
                self.store.replace_cues(job_id, [{'cue_id': '0', 'begin_ms': 0, 'end_ms': 900, 'text': 'Hello',
                                                'language': 'en', 'decision': 'translate', 'reason': '用户在终端确认',
                                                'source_lang': 'English', 'translated': '已保存译文'}])
                before, cues = self.store.job(job_id), self.store.cues(job_id)
                self.assertEqual(self.enqueue(output_dir=self.a / state, overwrite=True), job_id)
                after = self.store.job(job_id)
                self.assertTrue(after['manifest']['overwrite'])
                expected = before['manifest'] | {'overwrite': True}
                self.assertEqual(after['manifest'], expected)
                self.assertEqual((after['state'], after['error'], after['error_code']),
                                 (before['state'], before['error'], before['error_code']))
                self.assertEqual(self.store.cues(job_id), cues)

    def test_reuse_without_overwrite_preserves_previous_setting(self):
        for overwrite in [False, True]:
            with self.subTest(overwrite=overwrite):
                directory = self.a / str(overwrite)
                job_id = self.enqueue(output_dir=directory, overwrite=overwrite)
                self.assertEqual(self.enqueue(output_dir=directory), job_id)
                self.assertEqual(self.store.job(job_id)['manifest']['overwrite'], overwrite)

    def test_reuse_checks_every_conflict_before_promoting_overwrite(self):
        other = self.source.with_suffix('.mp4')
        other.write_bytes(b'other')
        old_id = self.store.enqueue(other, workspace_id='offline-space', account='offline-account')
        self.store.update(old_id, 'completed')
        job_id = self.enqueue()
        self.source.with_suffix('.srt').write_bytes(b'other output')
        before = self.store.job(job_id)
        with self.assertRaises(ToolError) as error:
            self.enqueue(overwrite=True)
        self.assertEqual(error.exception.code, 'output_conflict')
        self.assertEqual(self.store.job(job_id), before)

    def test_reuse_account_or_workspace_mismatch_preserves_task(self):
        job_id = self.enqueue()
        before = self.store.job(job_id)
        for workspace, account in [('offline-space', 'other-account'), ('other-space', 'offline-account')]:
            with self.subTest(workspace=workspace, account=account), self.assertRaises(ToolError) as error:
                self.store.enqueue(self.source, workspace_id=workspace, account=account, overwrite=True)
            self.assertEqual(error.exception.code, 'cache_mismatch')
            self.assertEqual(self.store.job(job_id), before)

    def test_reuse_checks_older_active_identity_before_mutation(self):
        old_id = self.enqueue()
        self.store.update(old_id, 'completed')
        job_id = self.enqueue()
        old_manifest = self.store.job(old_id)['manifest'] | {'account': 'other-account'}
        self.store.update(old_id, 'failed', old_manifest)
        before = self.store.job(job_id)
        with self.assertRaises(ToolError) as error:
            self.enqueue(overwrite=True)
        self.assertEqual(error.exception.code, 'cache_mismatch')
        self.assertEqual(self.store.job(job_id), before)

    def test_different_output_target_creates_new_task(self):
        job_id = self.enqueue()
        second = self.enqueue(output_dir=self.b, overwrite=True)
        self.assertNotEqual(second, job_id)
        self.assertFalse(self.store.job(job_id)['manifest']['overwrite'])


class OutputPipelineTests(PipelineFixture):
    def test_relative_enqueue_in_a_resumes_in_b_and_writes_only_to_a(self):
        a, b = self.path / 'A', self.path / 'B'
        a.mkdir()
        b.mkdir()
        with chdir(a):
            self.job_id = self.store.enqueue(self.source, output_dir=Path('subs'), workspace_id='offline-space')
        fake = FakeService(result(('中文字幕。', 'zh')))
        with chdir(b):
            self.assertEqual(self.run_fake(fake), 'completed')
        expected = a / 'subs' / self.source.with_suffix('.srt').name
        self.assertTrue(expected.is_file())
        self.assertEqual(self.store.job(self.job_id)['output'], str(expected))
        self.assertFalse((b / 'subs').exists())

    def run_cli(self, fake, *args):
        with mock.patch('subtitle_tool.main.Config', return_value=self.config), mock.patch('sys.stdout', new_callable=io.StringIO) as output, mock.patch('sys.stderr', new_callable=io.StringIO), mock.patch('requests.request', side_effect=fake.request), mock.patch('requests.post', side_effect=fake.upload):
            code = main([*args, '--non-interactive'])
        return code, json.loads(output.getvalue())

    def test_failed_task_repeated_with_overwrite_reuses_work_and_replaces_srt(self):
        fake = FakeService(result(('First sentence.', 'en'), ('Second sentence.', 'en')))
        fake.mt_failure.add('Second sentence.')
        self.assertEqual(self.run_fake(fake), 'failed')
        saved = self.store.references(self.job_id)[0]['translated']
        output = self.source.with_suffix('.srt')
        output.write_bytes(b'previous SRT')
        fake.mt_failure.clear()
        code, report = self.run_cli(fake, str(self.source), '--overwrite')
        self.assertEqual((code, report['job_id'], report['state']), (0, self.job_id, 'completed'))
        self.assertEqual(len(fake.named('/asr/transcription')), 1)
        self.assertEqual(self.store.references(self.job_id)[0]['translated'], saved)
        self.assertEqual(Path(self.store.job(self.job_id)['manifest']['backup']).read_bytes(), b'previous SRT')
        self.assertNotEqual(output.read_bytes(), b'previous SRT')

    def test_review_task_repeated_with_overwrite_keeps_review_then_replaces_srt(self):
        fake = FakeService(result(('OK', '')))
        self.assertEqual(self.run_fake(fake), 'needs_review')
        cues = self.store.cues(self.job_id)
        output = self.source.with_suffix('.srt')
        output.write_bytes(b'previous SRT')
        code, report = self.run_cli(fake, str(self.source), '--overwrite')
        self.assertEqual((code, report['job_id'], report['state']), (2, self.job_id, 'needs_review'))
        self.assertEqual(self.store.cues(self.job_id), cues)
        self.assertTrue(self.store.job(self.job_id)['manifest']['overwrite'])
        self.assertEqual(output.read_bytes(), b'previous SRT')
        from subtitle_tool.review import ReviewDecision
        self.store.apply_review(self.job_id, cues[0]['cue_id'], ReviewDecision('original'))
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(Path(self.store.job(self.job_id)['manifest']['backup']).read_bytes(), b'previous SRT')
        self.assertEqual(len(fake.named('/asr/transcription')), 1)

    def legacy_completed_output(self):
        self.assertEqual(self.run_fake(FakeService(result(('中文字幕。', 'zh')))), 'completed')
        with self.store.db:
            self.store.db.execute('UPDATE jobs SET output=? WHERE id=?', (self.source.with_suffix('.srt').name, self.job_id))
        return self.store.job(self.job_id)

    def test_reflow_fixes_matching_relative_path_at_commit(self):
        self.legacy_completed_output()
        with chdir(self.path):
            Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.store.job(self.job_id)['output'], str(self.source.with_suffix('.srt')))

    def test_ambiguous_reflow_requires_directory_without_changing_task(self):
        before = self.legacy_completed_output()
        directory = self.path / 'elsewhere'
        directory.mkdir()
        with chdir(directory), self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(error.exception.code, 'output_path_required')
        self.assertEqual(self.store.job(self.job_id), before)
        with chdir(directory):
            Engine(self.config, self.store).reflow(self.job_id, output_dir=Path('chosen'))
        self.assertEqual(self.store.job(self.job_id)['output'], str(directory / 'chosen' / self.source.with_suffix('.srt').name))

    def test_reflow_preparation_failure_preserves_matching_relative_path(self):
        before = self.legacy_completed_output()
        (self.config.data / 'cache' / self.job_id / 'raw-0000.json').unlink()
        with chdir(self.path), self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(error.exception.code, 'reflow_cache_missing')
        self.assertEqual(self.store.job(self.job_id), before)

    def test_reflow_conflict_preserves_path_manifest_and_subtitles(self):
        before = self.legacy_completed_output()
        other = self.source.with_suffix('.mp4')
        other.write_bytes(b'other')
        directory = self.path / 'chosen'
        account = hashlib.sha256(self.config.get_key().encode()).hexdigest()
        self.store.enqueue(other, output_dir=directory, workspace_id='offline-space', account=account)
        cues = self.store.cues(self.job_id)
        previous = self.source.with_suffix('.srt').read_bytes()
        with self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).reflow(self.job_id, output_dir=directory, overwrite=True)
        self.assertEqual(error.exception.code, 'output_conflict')
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual(self.store.cues(self.job_id), cues)
        self.assertEqual(self.source.with_suffix('.srt').read_bytes(), previous)
