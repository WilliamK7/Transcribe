"""Migration preserves unrelated gateway settings and resumable local data."""
import io
import json
from unittest import mock

from subtitle_tool.common import ToolError, atomic_json
from subtitle_tool.config import Config
from subtitle_tool.magpie_migration import migrate
from tests.fixtures import TemporaryTest


class MigrationTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        self.config.settings.workspace_id = 'synthetic-space'
        self.config.settings.mt_provider = 'pool'
        self.config.settings.mt_backends = [
            {'id': 'qiniu_free'}, {'id': 'openrouter_free'}, {'id': 'qiniu_paid'}, {'id': 'bailian_paid'},
            {'id': 'trial', 'provider': 'bailian', 'base_url': None, 'credential_ref': 'bailian',
             'tier': 'free', 'model': 'trial-model', 'weight': 1, 'rpm': 60,
             'capabilities': {'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096}}]
        self.config.set_key('synthetic-asr')
        self.config.set_qiniu_key('synthetic-qiniu')
        (self.config.data / 'mt-key.txt').write_text('synthetic-openrouter', encoding='utf-8')
        self.config.save()
        self.registry = self.path / 'gateway' / 'data'
        self.registry.mkdir(parents=True)
        self.executable = self.registry.parent / 'magpie.com'
        self.executable.write_bytes(b'fake')
        (self.executable.parent / '.portable').touch()
        self.original = {'providers': [{'id': 'unrelated', 'key': 'other-private-key', 'models': ['other']}],
                         'groups': [{'id': 'unrelated', 'members': ['unrelated/other']}]}
        atomic_json(self.registry / 'providers.json', self.original)
        self.calls = []
        hello = mock.Mock()
        hello.json.return_value = {'name': 'magpie', 'version': '0.1.3323'}
        self.enterContext(mock.patch('subtitle_tool.magpie_migration.requests.get', return_value=hello))
        self.enterContext(mock.patch('subtitle_tool.magpie_migration.qiniu_free_active', return_value=True))
        self.enterContext(mock.patch('subtitle_tool.magpie_migration._run', side_effect=self.command))

    def command(self, executable, *args):
        self.calls.append(args)
        path = self.registry / 'providers.json'
        raw = json.loads(path.read_bytes())
        kind, verb, identity, *words = args
        collection = raw['providers' if kind == 'provider' else 'groups']
        if verb == 'add':
            collection.append({'id': identity})
        item = next(row for row in collection if row['id'] == identity)
        if verb in {'add', 'set'}:
            for word in words:
                field, value = word.split('=', 1)
                if field == 'models':
                    item['models' if kind == 'provider' else 'members'] = value.split(',') if value else []
                elif field == 'free':
                    item['countFree'] = value == 'yes'
                elif field.startswith('header.'):
                    item.setdefault('headers', {})[field[7:]] = value
                else:
                    item[{'url': 'chat', 'stays': 'affinity'}.get(field, field)] = value
        elif verb == 'off':
            item['off'] = True
        elif verb == 'fallback':
            item['fallback'] = []
        elif verb == 'rpm':
            item['maxRPM'] = int(words[0])
        elif verb == 'models':
            item['models'] = []
        atomic_json(path, raw)

    def test_import_is_backed_up_and_leaves_other_agents_and_keys_intact(self):
        before = (self.config.data / 'settings.json').read_bytes()
        plan = migrate(self.config, self.executable, self.registry)
        self.assertEqual(plan['status'], 'planned')
        self.assertFalse(self.calls)
        result = migrate(self.config, self.executable, self.registry, apply=True)
        self.assertEqual(result['status'], 'configured')
        raw = json.loads((self.registry / 'providers.json').read_bytes())
        self.assertEqual(raw['providers'][0], self.original['providers'][0])
        self.assertEqual(raw['groups'][0], self.original['groups'][0])
        subtitle_groups = {row['id']: row for row in raw['groups'] if row['id'].startswith('transcribe')}
        self.assertEqual(subtitle_groups['transcribe']['routing'], 'free')
        self.assertEqual(subtitle_groups['transcribe-free']['routing'], 'order')
        self.assertEqual(subtitle_groups['transcribe']['affinity'], 'off')
        trial = next(row for row in raw['providers'] if row['id'] == 'transcribe-bailian-trial')
        self.assertTrue(trial['off'])
        qiniu = [p for p in raw['providers'] if p['id'].startswith('transcribe-qiniu-')]
        self.assertNotEqual(qiniu[0]['headers'], qiniu[1]['headers'])
        self.assertEqual(self.config.get_key(), 'synthetic-asr')
        self.assertEqual(self.config.settings.asr_provider, 'magpie')
        self.assertEqual(self.config.settings.mt_provider, 'magpie')
        self.assertIsNone(self.config.settings.mt_backends)
        from pathlib import Path
        self.assertEqual((Path(result['backup']) / 'settings.json').read_bytes(), before)
        self.assertEqual((self.config.data / 'key.txt').read_text(), 'synthetic-asr')
        for secret in ('synthetic-asr', 'synthetic-qiniu', 'synthetic-openrouter', 'other-private-key'):
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret, (self.config.data / 'magpie-migration.json').read_text())
        count = len(self.calls)
        self.assertEqual(migrate(self.config, self.executable, self.registry, apply=True)['status'], 'already_configured')
        self.assertEqual(len(self.calls), count)

    def test_import_preserves_fee_permission_and_discards_local_scheduler_settings(self):
        from subtitle_tool.mt_pool import configured_route
        from subtitle_tool.mt_backend import key_fingerprint
        self.config.settings.mt_routing = {'paid_enabled': False, 'free_wait_seconds': 15}
        migrate(self.config, self.executable, self.registry, apply=True)
        route = configured_route(self.config, {'account': key_fingerprint('synthetic-asr')})
        self.assertFalse(route['routing']['paid_enabled'])
        self.assertEqual(self.config.settings.mt_routing, {'paid_enabled': False})

    def test_name_collision_is_rejected_before_any_write(self):
        self.original['providers'].append({'id': 'transcribe-bailian', 'key': 'unrelated-key'})
        atomic_json(self.registry / 'providers.json', self.original)
        before = (self.config.data / 'settings.json').read_bytes()
        with self.assertRaises(ToolError):
            migrate(self.config, self.executable, self.registry, apply=True)
        self.assertFalse(self.calls)
        self.assertEqual((self.config.data / 'settings.json').read_bytes(), before)

    def test_interrupted_import_keeps_original_settings_and_can_be_retried(self):
        before = (self.config.data / 'settings.json').read_bytes()
        with mock.patch('subtitle_tool.magpie_migration._run', side_effect=ToolError('synthetic failure', 'test')):
            with self.assertRaises(ToolError):
                migrate(self.config, self.executable, self.registry, apply=True)
        self.assertEqual((self.config.data / 'settings.json').read_bytes(), before)
        self.assertEqual(migrate(self.config, self.executable, self.registry, apply=True)['status'], 'configured')

    def test_compatible_key_is_imported_only_at_its_original_host(self):
        self.config.settings.mt_backends[1].update(base_url='https://other.example/v1', tier='paid', model='paid-model')
        result = migrate(self.config, self.executable, self.registry, apply=True)
        raw = json.loads((self.registry / 'providers.json').read_bytes())
        destinations = [p['chat'] for p in raw['providers'] if p.get('key') == 'synthetic-openrouter']
        self.assertEqual(destinations, ['https://other.example/v1'])
        self.assertNotIn('transcribe-openrouter-free/openrouter/free', result['free_members'])

    def test_cli_configure_uses_hidden_qiniu_input_without_changing_other_keys(self):
        from subtitle_tool.main import main
        config = Config(self.path)
        config.set_key('bailian-synthetic')
        with mock.patch('subtitle_tool.main.Config', return_value=config), \
             mock.patch('subtitle_tool.main.sys.stdin.isatty', return_value=True), \
             mock.patch('subtitle_tool.main.getpass.getpass', return_value='qiniu-synthetic') as getpass, \
             mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(main(['--configure-mt', 'qiniu']), 0)
        self.assertEqual(getpass.call_count, 1)
        self.assertNotIn('qiniu-synthetic', stderr.getvalue())
        self.assertEqual(config.get_qiniu_key(), 'qiniu-synthetic')
        self.assertEqual(config.get_key(), 'bailian-synthetic')
        self.assertEqual(config.settings.mt_provider, 'pool')
