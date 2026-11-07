"""Offline contract tests for configurable and recoverable translation pools."""

from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.mt_backend import OPENROUTER_BASE_URL, QINIU_BASE_URL, QINIU_FREE_MODEL, key_fingerprint
from subtitle_tool.mt_pool import configured_route, saved_route, with_bailian_free_models


QINIU_CAPABILITIES = {'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096}


class FakeConfig:
    """Never touches the user's data directory or real credentials."""

    def __init__(self):
        self.settings = SimpleNamespace(mt_provider='pool', mt_backends=None, mt_routing=None, mt_quality=None)
        self.keys = {'qiniu': 'synthetic-qiniu', 'openrouter': 'synthetic-router', 'bailian': 'synthetic-asr'}

    def mt_settings(self):
        return self.settings

    def mt_rpm(self):
        return 25

    def _key(self, ref):
        key = self.keys.get(ref)
        if key is None:
            raise ToolError('测试用凭据缺失。', 'credentials')
        return key

    def get_qiniu_key(self):
        return self._key('qiniu')

    def get_mt_key(self):
        return self._key('openrouter')

    def get_key(self):
        return self._key('bailian')


def custom_qiniu(identity='course-paid', model='example/course-model', tier='paid'):
    return {'id': identity, 'provider': 'qiniu', 'base_url': QINIU_BASE_URL, 'model': model,
            'credential_ref': 'qiniu', 'tier': tier, 'weight': 2, 'rpm': 12,
            'capabilities': dict(QINIU_CAPABILITIES)}


class MTPoolConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = FakeConfig()
        self.manifest = {'region': 'beijing', 'access_mode': 'workspace', 'workspace_id': 'course-space',
                         'account': key_fingerprint(self.config.keys['bailian'])}

    def manifest_with(self, route):
        return {**self.manifest, 'translation_strategy_version': 5, 'mt_route_snapshot': copy.deepcopy(route)}

    def test_select_official_free_models_preserves_existing_pool_and_paid_fallback(self):
        self.config.settings.mt_backends = [{'id': 'qiniu_free', 'weight': 5}, {'id': 'bailian_paid'}]
        models = ['qwen3.8-27b', 'qwen3.8-max-0902']
        entries = with_bailian_free_models(self.config, self.manifest, models)
        self.assertEqual(self.config.settings.mt_backends,
                         [{'id': 'qiniu_free', 'weight': 5}, {'id': 'bailian_paid'}])
        self.config.settings.mt_backends = entries
        route = configured_route(self.config, self.manifest)
        by_model = {item['backend']['model']: item for item in route['entries'] if item['backend']}
        self.assertEqual(by_model['qwen3.8-27b']['capabilities'],
                         {'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096})
        self.assertEqual(by_model['qwen3.8-max-0902']['capabilities'],
                         {'response_format': 'json_schema', 'input_bytes': 32768, 'output_tokens': 4096})
        self.assertEqual(by_model['qwen3.8-flash']['tier'], 'paid')
        self.assertEqual(saved_route(self.manifest_with(route)), route)
        self.assertEqual(with_bailian_free_models(self.config, self.manifest, models), entries)

    def test_select_models_rejects_unknown_and_conflicting_existing_routes(self):
        for models in (['qwen3.8-2.4t-a95b'], ['qwen3.8-27b', 'qwen3.8-27b']):
            with self.subTest(models=models), self.assertRaises(ToolError):
                with_bailian_free_models(self.config, self.manifest, models)
        self.config.settings.mt_backends = [{'id': 'bailian_paid', 'model': 'qwen3.8-27b'}]
        with self.assertRaises(ToolError):
            with_bailian_free_models(self.config, self.manifest, ['qwen3.8-27b'])

    def test_new_model_and_short_default_overrides_replace_the_pool(self):
        self.config.settings.mt_backends = [
            {'id': 'qiniu_free', 'weight': 5},
            custom_qiniu(),
            {'id': 'openrouter_free', 'rpm': 7},
        ]
        route = configured_route(self.config, self.manifest)
        self.assertEqual([entry['id'] for entry in route['entries']], ['qiniu_free', 'course-paid', 'openrouter_free'])
        first, paid, third = route['entries']
        self.assertEqual((first['weight'], paid['tier'], third['rpm']), (5, 'paid', 7))
        self.assertEqual(paid['backend']['model'], 'example/course-model')
        self.assertEqual(paid['backend']['key_fingerprint'], key_fingerprint(self.config.keys['qiniu']))
        self.assertEqual(paid['capabilities'], QINIU_CAPABILITIES)
        self.assertEqual(saved_route(self.manifest_with(route)), route)

    def test_existing_model_may_be_replaced_and_defaults_omitted(self):
        self.config.settings.mt_backends = [{'id': 'qiniu_free'}, {'id': 'qiniu_paid', 'model': 'example/new-paid'}]
        route = configured_route(self.config, self.manifest)
        self.assertEqual([entry['id'] for entry in route['entries']], ['qiniu_free', 'qiniu_paid'])
        self.assertEqual(route['entries'][1]['backend']['model'], 'example/new-paid')
        self.assertEqual(saved_route(self.manifest_with(route)), route)

    def test_new_openrouter_free_and_bailian_paid_models_recover(self):
        profile = {'response_format': 'json_schema', 'input_bytes': 32768, 'output_tokens': 2048}
        self.config.settings.mt_backends = [
            {'id': 'router-next', 'provider': 'openai_compatible', 'base_url': OPENROUTER_BASE_URL,
             'model': 'example/future-free:free', 'credential_ref': 'openrouter', 'tier': 'free',
             'weight': 3, 'rpm': 10, 'capabilities': profile},
            {'id': 'bailian-next', 'provider': 'bailian', 'base_url': None,
             'model': 'example-new-bailian', 'credential_ref': 'bailian', 'tier': 'paid',
             'weight': 1, 'rpm': 20, 'capabilities': profile},
        ]
        route = configured_route(self.config, self.manifest)
        self.assertEqual([entry['backend']['model'] for entry in route['entries']],
                         ['example/future-free:free', 'example-new-bailian'])
        self.assertEqual(route['entries'][1]['backend']['base_url'],
                         'https://course-space.cn-beijing.maas.aliyuncs.com/compatible-mode/v1')
        self.assertEqual(saved_route(self.manifest_with(route)), route)

    def test_configuration_and_return_values_do_not_mutate_frozen_snapshot(self):
        custom = custom_qiniu()
        self.config.settings.mt_backends = [custom]
        route = configured_route(self.config, self.manifest)
        manifest = self.manifest_with(route)
        custom['model'] = 'example/changed'
        custom['capabilities']['input_bytes'] = 1024
        restored = saved_route(manifest)
        self.assertEqual(restored['entries'][0]['backend']['model'], 'example/course-model')
        self.assertEqual(restored['entries'][0]['capabilities']['input_bytes'], 32768)
        restored['entries'][0]['weight'] = 9
        self.assertEqual(manifest['mt_route_snapshot'], route)

    def test_saved_route_does_not_depend_on_current_default_templates(self):
        self.config.settings.mt_backends = [{'id': 'qiniu_free'}, custom_qiniu()]
        route = configured_route(self.config, self.manifest)
        saved = self.manifest_with(route)
        with mock.patch('subtitle_tool.mt_pool.DEFAULT_ENTRIES', ()):
            self.assertEqual(saved_route(saved), route)

    def test_default_profiles_include_declared_output_format(self):
        route = configured_route(self.config, self.manifest)
        entries = {entry['id']: entry for entry in route['entries']}
        self.assertEqual(entries['qiniu_free']['capabilities'], QINIU_CAPABILITIES)
        self.assertEqual(entries['openrouter_free']['capabilities']['response_format'], 'json_schema')
        self.assertEqual(entries['bailian_paid']['capabilities']['response_format'], 'json_schema')
        self.assertEqual(entries['openrouter_free']['backend']['base_url'], OPENROUTER_BASE_URL)

    def test_disabled_and_missing_optional_keys_are_unavailable_not_reused(self):
        self.config.settings.mt_backends = [{'id': 'qiniu_free', 'enabled': False},
                                            {'id': 'qiniu_paid'}, {'id': 'openrouter_free'}]
        self.config.keys['qiniu'] = None
        route = configured_route(self.config, self.manifest, paid_enabled=False)
        self.assertIsNone(route['entries'][0]['backend'])
        self.assertIsNone(route['entries'][1]['backend'])
        self.assertEqual(route['entries'][2]['backend']['key_fingerprint'], key_fingerprint(self.config.keys['openrouter']))
        self.assertFalse(route['routing']['paid_enabled'])
        self.assertEqual(saved_route(self.manifest_with(route)), route)
        self.config.keys['openrouter'] = None
        with self.assertRaises(ToolError) as error:
            configured_route(self.config, self.manifest, paid_enabled=False)
        self.assertEqual(error.exception.code, 'credentials')

    def test_configuration_rejects_duplicate_id_and_backend_identity(self):
        examples = [
            [{'id': 'qiniu_free'}, {'id': 'qiniu_free'}],
            [{'id': 'qiniu_free'}, custom_qiniu('duplicate', QINIU_FREE_MODEL, 'free')],
        ]
        for entries in examples:
            with self.subTest(kind=len(entries)), self.assertRaises(ToolError):
                self.config.settings.mt_backends = entries
                configured_route(self.config, self.manifest)

    def test_custom_model_requires_complete_explicit_capability_profile(self):
        item = custom_qiniu()
        for changed in (None, {}, {'response_format': 'json_object'},
                        {'response_format': 'other', 'input_bytes': 32768, 'output_tokens': 4096},
                        {'response_format': 'text', 'input_bytes': True, 'output_tokens': 4096},
                        {'response_format': 'text', 'input_bytes': 0, 'output_tokens': 4096},
                        {'response_format': 'text', 'input_bytes': 32768, 'output_tokens': 32769}):
            with self.subTest(kind=type(changed).__name__, value=changed), self.assertRaises(ToolError):
                self.config.settings.mt_backends = [dict(item, capabilities=changed)]
                configured_route(self.config, self.manifest)
        for response_format in ('json_object', 'json_schema', 'text'):
            profile = {'response_format': response_format, 'input_bytes': 4096, 'output_tokens': 1024}
            self.config.settings.mt_backends = [dict(item, capabilities=profile)]
            route = configured_route(self.config, self.manifest)
            self.assertEqual(route['entries'][0]['capabilities'], profile)
            self.assertEqual(saved_route(self.manifest_with(route)), route)

    def test_boolean_numeric_fields_and_non_list_config_are_rejected(self):
        for overrides in ({'weight': True}, {'rpm': False}, {'enabled': 1}):
            with self.subTest(overrides=overrides), self.assertRaises(ToolError):
                self.config.settings.mt_backends = [dict({'id': 'qiniu_free'}, **overrides)]
                configured_route(self.config, self.manifest)
        self.config.settings.mt_backends = {}
        with self.assertRaises(ToolError):
            configured_route(self.config, self.manifest)
        self.config.settings.mt_backends = [{'id': 'qiniu_free'}]
        self.config.settings.mt_routing = {'free_wait_seconds': True}
        with self.assertRaises(ToolError):
            configured_route(self.config, self.manifest)

    def test_endpoint_normalization_and_cross_provider_key_rejected(self):
        item = custom_qiniu()
        item['base_url'] = 'https://API.QNAIGC.COM:443/v1/'
        self.config.settings.mt_backends = [item]
        route = configured_route(self.config, self.manifest)
        self.assertEqual(route['entries'][0]['backend']['base_url'], QINIU_BASE_URL)
        for changes in ({'credential_ref': 'openrouter'}, {'base_url': OPENROUTER_BASE_URL},
                        {'base_url': 'https://untrusted.example/v1'}, {'provider': 'openai_compatible'}):
            with self.subTest(changes=changes), self.assertRaises(ToolError):
                self.config.settings.mt_backends = [dict(item, **changes)]
                configured_route(self.config, self.manifest)

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
        changed['mt_route_snapshot']['entries'][-1]['backend']['key_fingerprint'] = 'f' * 64
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed['mt_route_snapshot']['entries'][2]['backend']['key_fingerprint'] = 'f' * 64
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed['mt_route_snapshot']['entries'][0]['backend']['base_url'] = 'https://untrusted.example/v1'
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
        for key, value in [('response_format', 'bogus'), ('input_bytes', True), ('input_bytes', 0),
                           ('output_tokens', -1), ('output_tokens', 32769)]:
            changed = copy.deepcopy(original)
            changed['mt_route_snapshot']['entries'][0]['capabilities'][key] = value
            mutations.append(changed)
        changed = copy.deepcopy(original)
        del changed['mt_route_snapshot']['entries'][0]['capabilities']['response_format']
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed['mt_route_snapshot']['quality']['max_revisions'] = 2.0
        mutations.append(changed)
        for changed in mutations:
            with self.subTest(index=mutations.index(changed)):
                with self.assertRaises(ToolError) as error:
                    saved_route(changed)
                self.assertNotIn('synthetic-', str(error.exception))

    def test_unimplemented_global_strategy_overrides_are_rejected_on_restore(self):
        baseline = self.manifest_with(configured_route(self.config, self.manifest))
        changes = [
            ('routing', 'stage_deadline_seconds', 181),
            ('routing', 'http_timeout_seconds', 59),
            ('routing', 'max_attempts', 1),
            ('quality', 'input_bytes', 32767),
            ('quality', 'output_tokens', 2048),
            ('quality', 'max_revisions', 1),
            ('routing', 'stage_deadline_seconds', 180.0),
            ('routing', 'http_timeout_seconds', 60.0),
            ('routing', 'max_attempts', 2.0),
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
        cases = [('format_version', True), ('format_version', 2), ('request_version', 4),
                 ('request_version', 5.0), ('provider', []), ('provider', 'unknown'),
                 ('model', ''), ('model', 'a b'), ('model', 'a\u202eb'), ('model', 'x' * 501),
                 ('key_fingerprint', None), ('key_fingerprint', 'f' * 63),
                 ('base_url', None), ('base_url', 'https://API.QNAIGC.COM/v1'),
                 ('base_url', 'https://untrusted.example/v1'), ('extra', 'unsupported')]
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


if __name__ == '__main__':
    unittest.main()
