"""Gateway snapshots reject corrupted identities and preserve glossary validation."""
import copy
from contextlib import closing
from subtitle_tool.common import ToolError
from subtitle_tool.config import Config
from subtitle_tool.mt_backend import key_fingerprint
from subtitle_tool.mt_pool import configured_route, saved_route
from subtitle_tool.policy import load_policy
from subtitle_tool.store import Store
from tests.fixtures import set_asr_account, TemporaryTest


class GatewaySnapshotValidationTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        set_asr_account(self.config, 'synthetic-asr')
        self.manifest = {'region': 'beijing', 'access_mode': 'workspace', 'workspace_id': 'space',
                         'account': key_fingerprint('synthetic-asr')}

    def manifest_with(self, route):
        return {**self.manifest, 'translation_strategy_version': 5, 'mt_route_snapshot': copy.deepcopy(route)}

    def test_enqueue_and_report_preserve_the_saved_route_on_reuse(self):
        from subtitle_tool.main import summary
        route = configured_route(self.config, self.manifest)
        media = self.path / 'synthetic.mp4'
        media.write_bytes(b'not-real-media')
        with closing(Store(self.config.data / 'jobs.sqlite')) as store:
            options = dict(workspace_id='space', account=self.manifest['account'], mt_route_snapshot=route)
            job_id = store.enqueue(media, **options)
            manifest = store.job(job_id)['manifest']
            self.assertEqual(manifest['translation_strategy_version'], 5)
            self.assertEqual(saved_route(manifest), route)
            load_policy(manifest)
            primary = next(entry['backend'] for entry in route['entries'] if entry['tier'] == 'paid')
            report = summary(store, job_id)
            self.assertEqual((report['mt_provider'], report['models']['mt']),
                             (primary['provider'], primary['model']))
            self.assertEqual(store.enqueue(media, **options), job_id)
            self.assertEqual(summary(store, job_id), report)
            self.config.settings.mt_routing = {'paid_enabled': False}
            options['mt_route_snapshot'] = configured_route(self.config, self.manifest)
            with self.assertRaises(ToolError) as error:
                store.enqueue(media, **options)
            self.assertEqual(error.exception.code, 'mt_config_changed')
            self.assertEqual(summary(store, job_id), report)


    def test_saved_snapshot_rejects_bad_id_duplicate_backend_and_key_mismatch(self):
        route = configured_route(self.config, self.manifest)
        original = self.manifest_with(route)
        mutations = []
        changed = copy.deepcopy(original)
        changed['mt_route_snapshot']['entries'][1]['id'] = changed['mt_route_snapshot']['entries'][0]['id']
        mutations.append(changed)
        changed = copy.deepcopy(original)
        entries = changed['mt_route_snapshot']['entries']
        entries[1]['credential_ref'] = 'qiniu'
        entries[1]['backend'] = copy.deepcopy(entries[0]['backend'])
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed['mt_route_snapshot']['entries'][0]['credential_ref'] = 'openrouter'
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed['mt_route_snapshot']['entries'][1]['backend']['key_fingerprint'] = 'f' * 64
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed['mt_route_snapshot']['entries'][0]['backend']['base_url'] = 'https://example.com/v1?secret=private'
        mutations.append(changed)
        for changed in mutations:
            with self.subTest(index=mutations.index(changed)), self.assertRaises(ToolError):
                saved_route(changed)


    def test_malformed_snapshot_and_capabilities_are_rejected_without_content(self):
        original = self.manifest_with(configured_route(self.config, self.manifest))
        mutations = []
        changed = copy.deepcopy(original)
        del changed['mt_route_snapshot']
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed['mt_route_snapshot']['format_version'] = True
        mutations.append(changed)
        for backend in (None, {}):
            changed = copy.deepcopy(original)
            changed['mt_route_snapshot']['entries'][0]['backend'] = backend
            mutations.append(changed)
        changed = copy.deepcopy(original)
        changed['mt_route_snapshot']['entries'].pop()
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed['mt_route_snapshot']['entries'][1]['tier'] = 'free'
        mutations.append(changed)
        for key, value in [('response_format', 'bogus'), ('input_bytes', True), ('input_bytes', 0),
                           ('output_tokens', -1), ('output_tokens', 32769)]:
            changed = copy.deepcopy(original)
            changed['mt_route_snapshot']['entries'][0]['capabilities'][key] = value
            mutations.append(changed)
        changed = copy.deepcopy(original)
        del changed['mt_route_snapshot']['entries'][0]['capabilities']['response_format']
        mutations.append(changed)
        for changed in mutations:
            with self.subTest(index=mutations.index(changed)):
                with self.assertRaises(ToolError) as error:
                    saved_route(changed)
                self.assertNotIn('synthetic-', str(error.exception))


    def test_saved_quality_requires_current_limits(self):
        baseline = self.manifest_with(configured_route(self.config, self.manifest))
        changes = [
            ('quality', 'input_bytes', 32767),
            ('quality', 'output_tokens', 2048),
            ('quality', 'max_revisions', 1),
            ('quality', 'input_bytes', 32768.0),
            ('quality', 'output_tokens', 4096.0),
            ('quality', 'max_revisions', 2.0),
        ]
        for section, field, value in changes:
            changed = copy.deepcopy(baseline)
            changed['mt_route_snapshot'][section][field] = value
            with self.subTest(section=section, field=field, value=value), self.assertRaises(ToolError):
                saved_route(changed)


    def test_malformed_backend_fields_are_rejected(self):
        baseline = self.manifest_with(configured_route(self.config, self.manifest))
        cases = [('format_version', True), ('format_version', 3), ('request_version', 4),
                 ('request_version', 5.0), ('provider', []), ('provider', 'unknown'),
                 ('model', ''), ('model', 'a b'), ('model', 'a\u202eb'), ('model', 'x' * 501),
                 ('key_fingerprint', None), ('key_fingerprint', 'f' * 63),
                 ('base_url', None), ('base_url', 'https://API.QNAIGC.COM/v1'),
                 ('base_url', 'https://user:secret@example.com/v1'), ('extra', 'unsupported')]
        for field, value in cases:
            changed = copy.deepcopy(baseline)
            backend = changed['mt_route_snapshot']['entries'][0]['backend']
            backend[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ToolError) as error:
                saved_route(changed)
            self.assertEqual(error.exception.code, 'mt_config')
        for field in baseline['mt_route_snapshot']['entries'][0]['backend']:
            changed = copy.deepcopy(baseline)
            del changed['mt_route_snapshot']['entries'][0]['backend'][field]
            with self.subTest(missing=field), self.assertRaises(ToolError):
                saved_route(changed)


    def test_glossary_validation_is_equally_strict_for_config_and_snapshot(self):
        invalid = [None, {'': '术语'}, {'source': ''}, {'s' * 201: '术语'}, {'source': '译' * 501},
                   {'source': 3}, {'so\u202eurce': '术语'}, {'source': '术\u0085语'}, {'source': '\ud800'}]
        baseline = self.manifest_with(configured_route(self.config, self.manifest))
        for manual in invalid:
            with self.subTest(kind=type(manual).__name__, length=len(manual) if isinstance(manual, dict) else 0):
                self.config.settings.mt_quality = {'manual_glossary': manual}
                with self.assertRaises(ToolError):
                    configured_route(self.config, self.manifest)
                changed = copy.deepcopy(baseline)
                changed['mt_route_snapshot']['quality']['manual_glossary'] = manual
                with self.assertRaises(ToolError):
                    saved_route(changed)
        self.config.settings.mt_quality = {'manual_glossary': {'epoch': '训练轮次'}}
        route = configured_route(self.config, self.manifest)
        self.assertEqual(saved_route(self.manifest_with(route))['quality']['manual_glossary'], {'epoch': '训练轮次'})
