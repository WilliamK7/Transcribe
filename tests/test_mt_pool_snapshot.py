"""Versioned route snapshots keep accounts, provider capabilities and pricing separate."""

from subtitle_tool.common import ToolError
from subtitle_tool.mt_backend import key_fingerprint
from subtitle_tool.mt_pool import configured_route, saved_route
from tests.fixtures import TemporaryTest
from subtitle_tool.config import Config
from subtitle_tool.store import Store
from subtitle_tool.policy import load_policy


class PoolSnapshotTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        self.config.set_key('synthetic-bailian')
        self.config.set_qiniu_key('synthetic-qiniu')
        (self.config.data / 'mt-key.txt').write_text('synthetic-openrouter', encoding='utf-8')
        self.manifest = {'region': 'beijing', 'access_mode': 'workspace', 'workspace_id': 'space',
                         'account': key_fingerprint('synthetic-bailian')}


    def test_new_install_defaults_to_magpie(self):
        self.assertEqual(Config(self.path / 'fresh').settings.mt_provider, 'magpie')

    def test_enqueue_and_report_use_the_saved_route(self):
        from subtitle_tool.main import summary
        route = configured_route(self.config, self.manifest)
        media = self.path / 'synthetic.mp4'
        media.write_bytes(b'not-real-media')
        store = Store(self.config.data / 'jobs.sqlite')
        try:
            options = dict(workspace_id='space', account=self.manifest['account'], mt_route_snapshot=route)
            job_id = store.enqueue(media, **options)
            manifest = store.job(job_id)['manifest']
            self.assertEqual(saved_route(manifest), route)
            primary = next(entry['backend'] for entry in route['entries'] if entry['tier'] == 'paid')
            report = summary(store, job_id)
            self.assertEqual((report['mt_provider'], report['models']['mt']),
                             (primary['provider'], primary['model']))
            self.assertEqual(store.enqueue(media, **options), job_id)
            self.assertEqual(summary(store, job_id), report)
        finally:
            store.close()


    def test_new_job_freezes_full_route_and_rejects_changed_route_on_reuse(self):
        route = configured_route(self.config, self.manifest)
        media = self.path / 'synthetic.mp4'
        media.write_bytes(b'not-real-media')
        store = Store(self.config.data / 'jobs.sqlite')
        try:
            job_id = store.enqueue(media, workspace_id='space', account=self.manifest['account'],
                                   mt_route_snapshot=route)
            saved = store.job(job_id)['manifest']
            self.assertEqual(saved['translation_strategy_version'], 5)
            self.assertEqual(saved_route(saved), route)
            load_policy(saved)
            self.config.settings.mt_routing = {'paid_enabled': False}
            changed = configured_route(self.config, self.manifest)
            with self.assertRaises(ToolError) as error:
                store.enqueue(media, workspace_id='space', account=self.manifest['account'],
                              mt_route_snapshot=changed)
            self.assertEqual(error.exception.code, 'mt_config_changed')
        finally:
            store.close()
