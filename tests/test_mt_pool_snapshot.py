"""Versioned route snapshots keep accounts, provider capabilities and pricing separate."""
from unittest import mock
import os

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
        self.config.settings.mt_provider = 'pool'
        self.manifest = {'region': 'beijing', 'access_mode': 'workspace', 'workspace_id': 'space',
                         'account': key_fingerprint('synthetic-bailian')}

    def test_default_pool_is_versioned_and_separates_credentials(self):
        route = configured_route(self.config, self.manifest)
        entries = route['entries']
        self.assertEqual([(e['id'], e['tier'], e['weight']) for e in entries],
                         [('qiniu_free', 'free', 3), ('openrouter_free', 'free', 1),
                          ('qiniu_paid', 'paid', 3), ('bailian_paid', 'paid', 1)])
        self.assertEqual(entries[0]['backend']['key_fingerprint'], entries[2]['backend']['key_fingerprint'])
        self.assertNotEqual(entries[0]['backend']['key_fingerprint'], entries[1]['backend']['key_fingerprint'])
        self.assertNotEqual(entries[2]['backend']['key_fingerprint'], entries[3]['backend']['key_fingerprint'])
        self.assertEqual(route['routing']['free_wait_seconds'], 15)
        self.assertEqual(route['routing']['stage_deadline_seconds'], 180)
        self.assertTrue(route['routing']['paid_enabled'])
        self.assertNotIn('synthetic-qiniu', str(route))
        primary = entries[0]['backend']
        loaded = saved_route({**self.manifest, 'translation_strategy_version': 5, 'mt_snapshot': primary,
                              'mt_route_snapshot': route})
        self.assertEqual(loaded, route)

    def test_new_install_defaults_to_the_pool(self):
        self.assertEqual(Config(self.path / 'fresh').settings.mt_provider, 'pool')

    def test_missing_key_is_saved_as_unavailable_not_replaced_by_other_account(self):
        (self.config.data / 'qiniu-key.txt').unlink()
        with mock.patch.dict(os.environ, {'QINIU_API_KEY': ''}):
            route = configured_route(self.config, self.manifest, paid_enabled=False)
        qiniu = [entry for entry in route['entries'] if entry['credential_ref'] == 'qiniu']
        self.assertEqual([entry['backend'] for entry in qiniu], [None, None])
        self.assertFalse(route['routing']['paid_enabled'])
        self.assertIsNotNone(next(entry for entry in route['entries'] if entry['id'] == 'openrouter_free')['backend'])

    def test_excessive_free_wait_is_rejected(self):
        self.config.settings.mt_routing = {'free_wait_seconds': 60}
        with self.assertRaises(ToolError):
            configured_route(self.config, self.manifest)

    def test_new_job_freezes_full_route_and_rejects_changed_route_on_reuse(self):
        route = configured_route(self.config, self.manifest)
        media = self.path / 'synthetic.mp4'
        media.write_bytes(b'not-real-media')
        store = Store(self.config.data / 'jobs.sqlite')
        primary = next(e['backend'] for e in route['entries'] if e['backend'])
        try:
            job_id = store.enqueue(media, workspace_id='space', account=self.manifest['account'],
                                   mt_snapshot=primary, mt_route_snapshot=route)
            saved = store.job(job_id)['manifest']
            self.assertEqual(saved['translation_strategy_version'], 5)
            self.assertEqual(saved_route(saved), route)
            load_policy(saved)
            self.config.settings.mt_routing = {'free_wait_seconds': 2}
            changed = configured_route(self.config, self.manifest)
            with self.assertRaises(ToolError) as error:
                store.enqueue(media, workspace_id='space', account=self.manifest['account'],
                              mt_snapshot=primary, mt_route_snapshot=changed)
            self.assertEqual(error.exception.code, 'mt_config_changed')
        finally:
            store.close()
