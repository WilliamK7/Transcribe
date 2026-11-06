import hashlib
import json
from unittest import mock

from subtitle_tool.archives import export_archive, read_archive
from subtitle_tool.common import ToolError, atomic_json
from subtitle_tool.engine import Engine
from subtitle_tool.policy import load_policy
from tests.fixtures import FakeService, SavedResultFixture, result


class ArchiveTests(SavedResultFixture):
    def archive(self, *, manual=False):
        text, language = ('Keep this original.', 'en') if manual else ('保留这句原文。', 'zh')
        sources = self.save_result(result((text, language)))
        manifest = self.store.job(self.job_id)['manifest']
        if manual:
            from subtitle_tool.review import ReviewDecision
            from subtitle_tool.subtitles import make_fragment
            sources[0].update(ReviewDecision('original').values())
            self.store.initialize_subtitles(self.job_id, sources, [make_fragment(s) for s in sources], manifest)
        manifest.update(translation_strategy_version=1, execution_snapshot={'prompt_version': 1})
        content = ('1\r\n00:00:00,000 --> 00:00:00,900\r\n' + text + '\r\n\r\n').encode()
        manifest['expected_srt_sha256'] = hashlib.sha256(content).hexdigest()
        self.store.update(self.job_id, 'completed', manifest)
        folder = self.config.data / 'archives' / self.job_id
        folder.mkdir(parents=True)
        (folder / 'subtitle.srt').write_bytes(content)
        (folder / 'raw-0000.json').write_bytes((self.cache / 'raw-0000.json').read_bytes())
        self.store.snapshot(self.job_id, folder / 'task.json')
        atomic_json(folder / 'index.json', {'format_version': 1, 'job_id': self.job_id,
                    'files': {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                              for name in ['subtitle.srt', 'task.json', 'raw-0000.json']}})
        manifest['archived'] = True
        self.store.update(self.job_id, 'archived', manifest)
        return folder, content

    def test_export_is_byte_exact_without_credentials_or_network(self):
        _, expected = self.archive()
        with mock.patch.object(self.config, 'get_key', side_effect=AssertionError('No credentials needed')), \
                mock.patch('requests.request', side_effect=AssertionError('No network')):
            report = export_archive(self.config, self.job_id, self.path / 'exports')
        from pathlib import Path
        self.assertEqual(Path(report['output']).read_bytes(), expected)
        self.assertEqual(report['cloud_calls'], 0)
        self.assertEqual(self.store.job(self.job_id)['state'], 'archived')

    def test_corruption_or_path_escape_stops_before_export_or_rebuild(self):
        folder, _ = self.archive()
        (folder / 'raw-0000.json').write_bytes(b'changed')
        before = self.store.jobs()
        with self.assertRaises(ToolError) as caught:
            Engine(self.config, self.store).reflow(self.job_id, output_dir=self.path / 'rebuild')
        self.assertEqual(caught.exception.code, 'archive_invalid')
        self.assertEqual(self.store.jobs(), before)
        with self.assertRaises(ToolError):
            read_archive(self.config, '../outside')

    def test_archived_task_cannot_resume_and_startup_keeps_it_unchanged(self):
        self.archive()
        before = self.store.job(self.job_id)
        engine = Engine(self.config, self.store)
        engine.recover_jobs()
        with self.assertRaises(ToolError) as caught:
            engine.resume(self.job_id)
        self.assertEqual(caught.exception.code, 'job_archived')
        self.assertEqual(self.store.job(self.job_id), before)

    def test_rebuild_creates_current_task_and_preserves_archive_without_asr(self):
        folder, _ = self.archive()
        previous = self.store.job(self.job_id)
        frozen = (folder / 'task.json').read_bytes()
        self.source.unlink()
        new_id = Engine(self.config, self.store).reflow(self.job_id, output_dir=self.path / 'rebuilt')
        self.assertNotEqual(new_id, self.job_id)
        load_policy(self.store.job(new_id)['manifest'])
        self.assertEqual(self.store.job(self.job_id), previous)
        self.assertEqual((folder / 'task.json').read_bytes(), frozen)
        self.job_id = new_id
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(new_id)['error'])
        self.assertEqual(fake.calls, [])

    def test_export_does_not_overwrite_without_permission_or_write_into_data(self):
        self.archive()
        directory = self.path / 'exports'
        export_archive(self.config, self.job_id, directory)
        with self.assertRaises(ToolError):
            export_archive(self.config, self.job_id, directory)
        with self.assertRaises(ToolError):
            export_archive(self.config, self.job_id, self.config.data)

    def test_rebuild_rolls_back_database_and_cleans_only_new_files(self):
        self.archive()
        jobs = self.store.jobs()
        caches = set((self.config.data / 'cache').iterdir())
        initialize = self.store._initialize_subtitles
        def fail(*args):
            initialize(*args)
            raise OSError('synthetic disk failure')
        with mock.patch.object(self.store, '_initialize_subtitles', side_effect=fail), self.assertRaises(OSError):
            Engine(self.config, self.store).reflow(self.job_id, output_dir=self.path / 'rebuilt')
        self.assertEqual(self.store.jobs(), jobs)
        self.assertEqual(set((self.config.data / 'cache').iterdir()), caches)
        read_archive(self.config, self.job_id)

    def test_rebuild_preserves_manual_original_decision_under_current_normalizer(self):
        self.archive(manual=True)
        new_id = Engine(self.config, self.store).reflow(self.job_id, output_dir=self.path / 'rebuilt')
        source = self.store.sources(new_id)[0]
        self.assertEqual((source['decision'], source['decision_origin']), ('original', 'manual'))
        self.job_id = new_id
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(fake.calls, [])

    def test_cli_rebuild_reports_new_id_and_report_cannot_replace_export(self):
        import io
        from subtitle_tool.main import main
        self.archive()
        old_id = self.job_id
        with mock.patch('subtitle_tool.main.Config', return_value=self.config), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output, \
                mock.patch('sys.stderr', new_callable=io.StringIO), \
                mock.patch('requests.request', side_effect=AssertionError('No ASR needed')):
            self.assertEqual(main(['--reflow', old_id, '--output-dir', str(self.path / 'rebuilt'), '--non-interactive']), 0)
        report = json.loads(output.getvalue())
        self.assertNotEqual(report['job_id'], old_id)
        self.assertEqual(report['state'], 'completed')
        target = self.path / 'exports' / (old_id + '.srt')
        with mock.patch('subtitle_tool.main.Config', return_value=self.config), self.assertRaises(ToolError) as caught:
            main(['--export-archive', old_id, '--output-dir', str(target.parent), '--report', str(target)])
        self.assertEqual(caught.exception.code, 'report_conflict')
        self.assertFalse(target.exists())
