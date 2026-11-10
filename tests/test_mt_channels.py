"""Channel discovery and model identity regressions; all endpoints are simulated."""
import json
from unittest import mock

from subtitle_tool.common import ToolError, atomic_json
from subtitle_tool.magpie import billing_route, validate_group
from subtitle_tool.mt_identity import canonical_model
from subtitle_tool.mt_gateway_setup import GatewaySetup, choose_roles
from tests.test_bailian_free import BailianFreeFixture


class ChannelTests(BailianFreeFixture):
    def test_plugin_provider_is_a_valid_group_member_and_free_offer(self):
        atomic_json(self.folder / 'plugin-providers.json', [{'id': 'opencode-zen-free', 'signedIn': True,
            'api': 'https://opencode.ai/zen/v1', 'models': [{'id': 'deepseek-v4.1-flash-free', 'free': True}]}])
        raw = self.guard._registry()
        raw['groups'] = [{'id': 'plugin', 'members': ['opencode-zen-free/deepseek-v4.1-flash-free']}]
        atomic_json(self.registry, raw)
        validate_group(self.config.settings, 'group/plugin')
        self.assertEqual(billing_route(self.config.settings, raw['groups'][0]['members'][0]), 'free')
        with self.assertRaises(ToolError):
            validate_group(self.config.settings, 'group/plugin', paid_only=True)

    def test_verified_free_aliases_are_not_independent_models(self):
        for name in ('deepseek-v4.1-flash', 'glm-5.3-flash', 'mimo-v2.6-flash', 'nemotron-3.5-lightning'):
            self.assertEqual(canonical_model('opencode-zen-free/' + name + '-free'),
                             canonical_model('other/' + name))
        self.assertEqual(canonical_model('openrouter/zai/glm-5.3-flash:free:low'), 'glm-5.3-flash')
        self.assertEqual(canonical_model('provider/deepseek-v4.1-flash-1017:free'), 'deepseek-v4.1-flash')

    def test_routed_model_cannot_be_assigned_to_multiple_roles(self):
        rows = {'auto': {'actual_models': ['a', 'b'], 'roles': ['translate', 'review', 'judge']},
                'fixed-a': {'actual_models': ['a'], 'roles': ['translate', 'review', 'judge']},
                'c': {'actual_models': ['c'], 'roles': ['review']},
                'd': {'actual_models': ['d'], 'roles': ['judge']}}
        pools, assignments = choose_roles(rows, {'a': 'translate', 'b': 'translate'})
        self.assertEqual(sum(pool.count('auto') for pool in pools.values()), 1)
        self.assertEqual(assignments['a'], assignments['b'])

    def test_one_provider_can_have_both_free_and_paid_translation_members(self):
        raw = self.guard._registry()
        raw['providers'] += [{'id': 'vercel', 'chat': 'https://ai-gateway.vercel.sh/v1'}]
        raw['groups'] = []
        atomic_json(self.registry, raw)
        rows = {'vercel/' + model: {'actual_models': [model], 'roles': [role]}
                for model, role in [('paid', 'translate'), ('gift', 'translate'),
                                    ('reviewer', 'review'), ('arbiter', 'judge')]}
        setup = GatewaySetup(self.config, self.parent, manager=self.guard)
        setup.apply(rows, ['vercel/gift'])
        groups = {g['id']: g for g in self.guard._registry()['groups']}
        free = groups['transcribe-free']['members']
        paid = groups['transcribe-paid']['members']
        self.assertFalse(set(free) & set(paid))
        self.assertEqual(groups[free[0].removeprefix('group/')]['members'], ['vercel/gift'])
        self.assertEqual(groups[paid[0].removeprefix('group/')]['members'], ['vercel/paid'])

    def test_dynamic_reply_requires_allowlisted_underlying_identity(self):
        from subtitle_tool.mt_identity import resolve_model
        member = 'vllm-sr/vllm-sr/auto'
        identities = {member: {'models': ['glm-5.3-flash', 'qwen3.8-27b'], 'dynamic': True,
                               'billing': 'free'}}
        self.assertEqual(resolve_model(member, {'x-vsr-selected-model': 'qwen/qwen3.8-27b'}, identities),
                         'qwen3.8-27b')
        for headers in ({}, {'x-vsr-selected-model': 'unknown/model'}):
            with self.assertRaises(ValueError):
                resolve_model(member, headers, identities)
        identities[member]['admitted'] = ['glm-5.3-flash']
        with self.assertRaises(ValueError):
            resolve_model(member, {'x-vsr-selected-model': 'qwen/qwen3.8-27b'}, identities, require_admitted=True)

    def test_plugin_cache_key_changes_with_account_and_interface(self):
        from subtitle_tool.mt_admission import admission_key
        provider = {'id': 'plugin', '_plugin_fingerprint': 'account-one', '_models': {'model': {'free': True}}}
        key = admission_key(provider, 'model')
        self.assertNotEqual(key, admission_key({**provider, '_plugin_fingerprint': 'account-two'}, 'model'))
        self.assertNotEqual(key, admission_key({**provider, '_models': {'model': {'free': False}}}, 'model'))
        self.assertNotEqual(key, admission_key({**provider, 'replyHeaders': ['X-Vsr-Selected-Model']}, 'model'))

    def test_malformed_identity_snapshot_is_rejected_without_type_error(self):
        from subtitle_tool.mt_identity import valid_identity_map
        for field, value in [('models', [{}]), ('models', [1]), ('billing', []), ('dynamic', 'true')]:
            row = {'models': ['one'], 'dynamic': False, 'billing': 'free', field: value}
            self.assertFalse(valid_identity_map({'provider/one': row}))

    def test_vercel_zero_prices_require_every_charge_to_be_zero(self):
        from subtitle_tool.mt_catalog import zero_priced, offer_billing
        self.assertTrue(zero_priced({'input': '0', 'output': '0', 'input_tiers': [{'cost': '0', 'up_to': 128000}]}))
        self.assertFalse(zero_priced({'input': '0', 'output': '0', 'request': '.01'}))
        self.assertFalse(zero_priced({'input': '0', 'output': '0', 'input_tiers': [{'cost': '.1'}]}))
        for owner in ('cloudflare-workers-ai', 'github-copilot'):
            self.assertEqual(offer_billing({'id': owner, 'countFree': True, '_models': {'x': {'free': True}}}, 'x'), 'commercial')
        self.assertEqual(offer_billing({'id': 'mixed', 'chat': 'https://openrouter.ai/api/v1', 'countFree': True},
                                      'vendor/commercial'), 'commercial')

    def test_catalog_is_read_only_and_preserves_disabled_models(self):
        from subtitle_tool.mt_catalog import Catalog
        from subtitle_tool.mt_channel_policy import default_policy
        from subtitle_tool.mt_transport import HTTPReply
        raw = self.guard._registry()
        raw['providers'].append({'id': 'vercel', 'modelsOff': ['openai/gpt-6-luna']})
        atomic_json(self.registry, raw)
        before = {p: p.read_bytes() for p in self.path.rglob('*') if p.is_file()}
        names = default_policy()['providers']['vercel']['preferred']
        responses = [{'data': [{'id': 'vercel/' + n} for n in names]}, {'data': [
            {'id': n, 'modalities': {'output': ['text']}, 'pricing': {'input': '0' if n.endswith('-free') else '1', 'output': '0'}}
            for n in names]}]
        transport = mock.Mock()
        transport.request.side_effect = [HTTPReply(200, json.dumps(r).encode()) for r in responses]
        with mock.patch.object(self.config, 'get_magpie_key', return_value='not-exported'):
            data = Catalog(self.config, self.parent, transport).discover(('vercel',))
        self.assertNotIn('vercel/openai/gpt-6-luna', data['identities'])
        self.assertEqual(sum(r['billing'] == 'free' for r in data['identities'].values()), 2)
        self.assertTrue(all(c.args[0] == 'GET' for c in transport.request.call_args_list))
        self.assertEqual(before, {p: p.read_bytes() for p in self.path.rglob('*') if p.is_file()})

    def test_preview_configuration_does_not_create_or_probe_a_data_directory(self):
        from subtitle_tool.config import Config
        destination = self.path / 'read-only-preview'
        config = Config(destination, read_only=True)
        self.assertFalse(destination.exists())
        self.assertEqual(config.settings.mt_base_url, 'http://127.0.0.1:3425/v1')

    def test_dynamic_backend_change_invalidates_its_admission_cache(self):
        from subtitle_tool.mt_admission import admission_key
        from subtitle_tool.mt_catalog import Catalog
        from subtitle_tool.mt_identity import VSR_MEMBER
        self.assertEqual(Catalog._routes_to({'models': [{'id': 'auto', 'routes_to': ['qwen/one', 'zai/two']}]}), ['one', 'two'])
        row = {'id': 'vllm-sr', '_identities': {VSR_MEMBER: {'models': ['one'], 'dynamic': True, 'billing': 'free'}}}
        key = admission_key(row, 'vllm-sr/auto')
        row['_identities'][VSR_MEMBER]['models'].append('two')
        self.assertNotEqual(key, admission_key(row, 'vllm-sr/auto'))
