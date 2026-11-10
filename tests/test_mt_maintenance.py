"""Incremental gateway maintenance. All inference and catalogs are simulated."""
import copy
import json
from unittest import mock

from subtitle_tool.common import ToolError, atomic_json
from subtitle_tool.mt_catalog import Catalog
from subtitle_tool.mt_gateway_setup import GatewaySetup
from subtitle_tool.mt_transport import HTTPReply
from tests.test_bailian_free import BailianFreeFixture


class MaintenanceTests(BailianFreeFixture):
    def exclude_providers(self):
        policy = self.policy()
        policy['excluded_providers'] = ['blocked-first', 'blocked-second']
        atomic_json(self.config.data / 'mt-channels.json', policy)
        raw = self.guard._registry()
        raw['providers'] += [{'id': 'blocked-first', 'models': ['model-a']},
                             {'id': 'blocked-second', 'models': ['model-b']}]
        raw['groups'].append({'id': 'forbidden', 'members': ['blocked-first/model-a']})
        atomic_json(self.registry, raw)
        return policy

    def test_excluded_provider_cannot_be_enrolled_again(self):
        from subtitle_tool.mt_channel_policy import load_policy
        policy = self.exclude_providers()
        policy['providers']['blocked-first'] = policy['providers']['existing']
        atomic_json(self.config.data / 'mt-channels.json', policy)
        with self.assertRaises(ToolError) as caught:
            load_policy(self.config)
        self.assertEqual(caught.exception.code, 'mt_provider_excluded')

    def test_excluded_candidate_and_judge_never_reach_transport(self):
        from subtitle_tool.mt_admission import ModelEvaluator
        self.exclude_providers()
        for member, judge in [('blocked-first/model-a', 'existing/new'), ('existing/new', 'blocked-second/model-b')]:
            transport = mock.Mock()
            evaluator = ModelEvaluator(self.config, self.parent, judge=judge, transport=transport)
            with self.assertRaises(ToolError) as caught:
                evaluator.evaluate(member)
            self.assertEqual(caught.exception.code, 'mt_provider_excluded')
            transport.request.assert_not_called()

    def test_excluded_supplier_inside_nested_live_group_blocks_request(self):
        from subtitle_tool.mt_channel_policy import check_provider_policy
        self.exclude_providers()
        raw = self.guard._registry()
        raw['groups'].append({'id': 'nested', 'members': ['group/forbidden']})
        atomic_json(self.registry, raw)
        with self.assertRaises(ToolError) as caught:
            check_provider_policy(self.config, 'group/nested')
        self.assertEqual(caught.exception.code, 'mt_provider_excluded')

    def test_excluded_group_can_switch_to_existing_fallback(self):
        from types import SimpleNamespace
        from subtitle_tool.mt_pipeline import Pipeline
        from subtitle_tool.mt_routing import RouteFailure
        self.exclude_providers()
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.engine = SimpleNamespace(config=self.config)
        entry = SimpleNamespace(backend=SimpleNamespace(model='group/forbidden', base_url=self.config.settings.mt_base_url))
        with self.assertRaises(RouteFailure) as caught:
            pipeline._validate_gateway(entry)
        self.assertTrue(caught.exception.fallback_allowed)
        self.assertEqual(caught.exception.category, 'permission_denied')

    def test_missing_exclusions_default_to_empty(self):
        from subtitle_tool.mt_channel_policy import load_policy
        policy = self.policy()
        del policy['excluded_providers']
        atomic_json(self.config.data / 'mt-channels.json', policy)
        self.assertEqual(load_policy(self.config)['excluded_providers'], [])

    def test_old_qualification_cannot_readmit_excluded_account(self):
        policy, manager, setup, catalog = self.prepared()
        policy['excluded_providers'] = ['blocked-first']
        atomic_json(self.config.data / 'mt-channels.json', policy)
        setup.policy = policy
        member = 'blocked-first/model-a'
        manager.state['members'][member] = {'accepted': {'roles': ['translate']}}
        catalog.discover.return_value['identities'] = {member: {
            'models': ['model-a'], 'dynamic': False, 'billing': 'commercial'}}
        setup.evaluate = mock.Mock()
        result = manager.refresh(policy=policy, discover=catalog, setup=setup)
        self.assertEqual(result['members'][member]['status'], 'provider_excluded')
        self.assertNotIn(member, setup.current_members())
        setup.evaluate.assert_not_called()

    def policy(self):
        from subtitle_tool.mt_channel_policy import default_policy
        policy = default_policy()
        policy.update(enabled=True, max_evaluations_per_cycle=1, max_evaluations_per_day=2)
        policy['providers'] = {'existing': {'adapter': 'registry', 'preferred': [],
            'include': ['*'], 'exclude': [], 'billing': ['free', 'commercial'],
            'max_candidates': {'free': 5, 'commercial': 3}}}
        atomic_json(self.config.data / 'mt-channels.json', policy)
        return policy

    def test_custom_explicit_provider_discovers_without_source_code_changes(self):
        policy = self.policy()
        raw = self.guard._registry()
        raw['providers'] += [{'id': 'existing', 'models': ['new', 'auto', 'embedding'], 'modelsOff': ['paused']}]
        atomic_json(self.registry, raw)
        transport = mock.Mock()
        transport.request.return_value = HTTPReply(200, json.dumps({'data': [
            {'id': 'existing/new'}, {'id': 'existing/paused'}, {'id': 'existing/auto'},
            {'id': 'unspecified/surprise'}]}).encode())
        with mock.patch.object(self.config, 'get_magpie_key', return_value='private'):
            catalog = Catalog(self.config, self.parent, transport)
            result = catalog.discover(policy=policy)
        self.assertEqual(set(result['identities']), {'existing/new'})

    def prepared(self):
        from subtitle_tool.mt_maintenance import ChannelMaintenance, identity_key
        from subtitle_tool.mt_catalog import read_registry
        policy = self.policy()
        policy['providers']['existing']['preferred'] = ['new']
        atomic_json(self.config.data / 'mt-channels.json', policy)
        raw = self.guard._registry()
        raw['providers'] += [{'id': 'existing', 'models': ['draft', 'reviewer', 'arbiter', 'new'], 'key': 'private'}]
        raw['groups'] = []
        atomic_json(self.registry, raw)
        rows = {'existing/' + name: {'roles': [role], 'actual_models': [name], 'suite': 3}
                for name, role in [('draft', 'translate'), ('reviewer', 'review'), ('arbiter', 'judge')]}
        setup = GatewaySetup(self.config, self.parent, manager=self.guard)
        setup.apply(rows)
        manager = ChannelMaintenance(self.config, self.parent, trial=self.guard)
        registry = read_registry(self.config.settings, setup.catalog)
        for member, result in rows.items():
            manager.state['members'][member] = {'accepted': copy.deepcopy(result),
                'proof': identity_key(member, registry, setup.catalog)}
        self.addCleanup(manager.close)
        catalog = mock.Mock()
        catalog.complete = {'existing'}
        catalog.available = set(rows) | {'existing/new'}
        catalog.skipped = []
        catalog.pending = set()
        catalog.discover.return_value = {'version': 1, 'models': {}, 'identities': {
            'existing/new': {'models': ['new'], 'dynamic': False, 'billing': 'commercial'}}}
        return policy, manager, setup, catalog

    def test_incremental_cycle_uses_old_qualifications_and_is_idempotent(self):
        policy, manager, setup, catalog = self.prepared()
        setup.evaluate = mock.Mock(return_value={'roles': ['translate'], 'actual_models': ['new'], 'retry_after': None})
        result = manager.refresh(policy=policy, discover=catalog, setup=setup)
        self.assertEqual(result['last_status'], 'updated')
        setup.evaluate.assert_called_once()
        version = self.config.settings.mt_policy_version
        manager.refresh(policy=policy, discover=catalog)
        self.assertEqual(self.config.settings.mt_policy_version, version)
        self.assertEqual(manager.state['budget']['evaluations'], 1)

    def test_transient_candidate_cools_down_across_restart_without_retiring_pool(self):
        from subtitle_tool.mt_maintenance import ChannelMaintenance
        policy, manager, setup, catalog = self.prepared()
        setup.evaluate = mock.Mock(return_value={'roles': [], 'retry_after': 1})
        result = manager.refresh(policy=policy, discover=catalog, setup=setup)
        self.assertEqual(result['members']['existing/new']['status'], 'cooldown')
        fresh = ChannelMaintenance(self.config, self.parent, trial=self.guard)
        with mock.patch.object(GatewaySetup, 'evaluate') as evaluate:
            fresh.refresh(policy=policy, discover=catalog)
        evaluate.assert_not_called()
        self.assertEqual(fresh.state['members']['existing/draft']['status'], 'qualified')

    def test_catalog_failure_keeps_pool_and_does_not_infer(self):
        policy, manager, setup, catalog = self.prepared()
        catalog.complete = set()
        catalog.discover.return_value = {'version': 1, 'models': {}, 'identities': {}}
        setup.evaluate = mock.Mock()
        before = self.guard._registry()
        result = manager.refresh(policy=policy, discover=catalog, setup=setup)
        self.assertEqual(result['last_status'], 'unchanged')
        self.assertEqual(before, self.guard._registry())
        setup.evaluate.assert_not_called()

    def test_unqualified_discovery_does_not_bump_production_policy(self):
        policy, manager, setup, catalog = self.prepared()
        policy['max_evaluations_per_cycle'] = 0
        atomic_json(self.config.data / 'mt-channels.json', policy)
        version = self.config.settings.mt_policy_version
        result = manager.refresh(policy=policy, discover=catalog, setup=setup)
        self.assertEqual(result['last_status'], 'unchanged')
        self.assertEqual(self.config.settings.mt_policy_version, version)

    def test_manual_model_switch_prevents_background_inference(self):
        policy, manager, setup, catalog = self.prepared()
        raw = self.guard._registry()
        next(p for p in raw['providers'] if p['id'] == 'existing')['modelsOff'] = ['new']
        atomic_json(self.registry, raw)
        setup.evaluate = mock.Mock()
        result = manager.refresh(policy=policy, discover=catalog, setup=setup)
        self.assertEqual(result['members']['existing/new']['status'], 'disabled_or_missing')
        setup.evaluate.assert_not_called()

    def test_exclusion_in_old_translation_group_does_not_remove_active_reviewer(self):
        policy, manager, setup, catalog = self.prepared()
        setup.managed['excluded'].append('transcribe-existing-paid:existing/reviewer')
        atomic_json(setup.managed_path, setup.managed)
        setup.evaluate = mock.Mock(return_value={'roles': ['translate'], 'actual_models': ['new']})
        result = manager.refresh(policy=policy, discover=catalog, setup=setup)
        self.assertEqual(result['members']['existing/reviewer']['status'], 'qualified')
        self.assertIn('existing/reviewer', manager.state['last_application']['roles']['review'])

    def test_qualified_standby_remains_considered_beyond_discovery_limit(self):
        policy, manager, setup, catalog = self.prepared()
        # This standby was admitted earlier but wasn't part of this cycle's
        # short discovery list; it still has its exact account/capability proof.
        from subtitle_tool.mt_maintenance import identity_key
        from subtitle_tool.mt_catalog import read_registry
        member = 'existing/new'
        registry = read_registry(self.config.settings, setup.catalog)
        manager.state['members'][member] = {'accepted': {'roles': ['translate'], 'actual_models': ['new']},
            'proof': identity_key(member, registry, setup.catalog), 'status': 'manual_hold'}
        catalog.discover.return_value = {'version': 1, 'models': {}, 'identities': {}}
        setup.evaluate = mock.Mock()
        manager.refresh(policy=policy, discover=catalog, setup=setup)
        setup.evaluate.assert_not_called()
        self.assertIn(member, manager.state['last_application']['roles']['translate'])

    def test_credential_change_never_reuses_old_qualification(self):
        policy, manager, setup, catalog = self.prepared()
        policy['max_evaluations_per_cycle'] = 0
        atomic_json(self.config.data / 'mt-channels.json', policy)
        raw = self.guard._registry()
        next(p for p in raw['providers'] if p['id'] == 'existing')['key'] = 'changed'
        atomic_json(self.registry, raw)
        result = manager.refresh(policy=policy, discover=catalog, setup=setup)
        self.assertEqual(result['last_status'], 'application_blocked')
        self.assertNotEqual(result['members']['existing/draft']['status'], 'qualified')

    def test_stop_prevents_discovery_and_group_writes(self):
        from subtitle_tool.common import Cancelled
        policy, manager, setup, catalog = self.prepared()
        manager.stop.set()
        with self.assertRaises(Cancelled):
            manager.refresh(policy=policy, discover=catalog, setup=setup)
        catalog.discover.assert_not_called()

    def test_provider_refresh_only_touches_enrolled_enabled_list_sources(self):
        policy, manager, setup, catalog = self.prepared()
        raw = self.guard._registry()
        raw['providers'].append({'id': 'unrelated', 'models': ['secret']})
        atomic_json(self.registry, raw)
        with mock.patch.object(self.guard, '_command') as command:
            manager.refresh_provider_lists(policy)
        command.assert_called_once_with('provider', 'refresh', 'existing')

    def test_removed_auto_exposure_is_never_added_back(self):
        policy, manager, setup, catalog = self.prepared()
        manager.state['exposed'] = {'existing/new': 'managed'}
        raw = self.guard._registry()
        next(p for p in raw['providers'] if p['id'] == 'existing')['models'].remove('new')
        atomic_json(self.registry, raw)
        catalog.pending = {'existing/new'}
        with mock.patch.object(manager, 'expose') as expose:
            result = manager.refresh(policy=policy, discover=catalog, setup=setup)
        expose.assert_not_called()
        self.assertEqual(result['members']['existing/new']['status'], 'manual_hold')

    def test_upstream_new_free_model_is_previewed_without_exposure(self):
        policy = self.policy()
        policy['providers']['existing']['adapter'] = 'openrouter'
        raw = self.guard._registry()
        raw['providers'].append({'id': 'existing', 'listOff': True, 'models': ['old']})
        atomic_json(self.registry, raw)
        transport = mock.Mock()
        transport.request.side_effect = [HTTPReply(200, b'{"data":[]}'), HTTPReply(200, json.dumps({'data': [
            {'id': 'vendor/new:free', 'architecture': {'output_modalities': ['text']},
             'pricing': {'prompt': '0', 'completion': '0'}}]}).encode())]
        with mock.patch.object(self.config, 'get_magpie_key', return_value='private'):
            catalog = Catalog(self.config, self.parent, transport)
            data = catalog.discover(policy=policy)
        self.assertEqual(catalog.pending, {'existing/vendor/new:free'})
        self.assertEqual(data['identities']['existing/vendor/new:free']['billing'], 'free')
        self.assertEqual(raw, self.guard._registry())

    def test_unrelated_plugin_model_does_not_expire_existing_qualification(self):
        from subtitle_tool.mt_catalog import read_registry
        from subtitle_tool.mt_admission import admission_key
        row = {'id': 'existing', 'spec': 'example@1.0', 'signedIn': True, 'models': [{'id': 'old', 'free': True}]}
        atomic_json(self.folder / 'plugin-providers.json', [row])
        a = next(p for p in read_registry(self.config.settings)['providers'] if p['id'] == 'existing')
        row['models'].append({'id': 'new', 'free': True})
        atomic_json(self.folder / 'plugin-providers.json', [row])
        b = next(p for p in read_registry(self.config.settings)['providers'] if p['id'] == 'existing')
        self.assertEqual(admission_key(a, 'old'), admission_key(b, 'old'))
        row['models'][0]['free'] = False
        atomic_json(self.folder / 'plugin-providers.json', [row])
        c = next(p for p in read_registry(self.config.settings)['providers'] if p['id'] == 'existing')
        self.assertNotEqual(admission_key(a, 'old'), admission_key(c, 'old'))

    def test_catalog_descriptive_enrichment_is_not_a_new_runtime_identity(self):
        from subtitle_tool.mt_maintenance import identity_key
        raw = {'providers': [{'id': 'provider', 'key': 'private', 'chat': 'https://openrouter.ai/api/v1'}]}
        catalog = {'identities': {}}
        first = identity_key('provider/vendor/model:free', raw, catalog)
        raw['providers'][0]['_models'] = {'vendor/model:free': {
            'id': 'vendor/model:free', 'free': True, 'modalities': {'output': ['text']}}}
        self.assertEqual(first, identity_key('provider/vendor/model:free', raw, catalog))

    def test_custom_plugin_package_version_participates_in_admission_key(self):
        from subtitle_tool.mt_catalog import read_registry
        from subtitle_tool.mt_admission import admission_key
        policy = self.policy()
        policy['providers']['existing']['package'] = 'custom-auth'
        atomic_json(self.folder / 'plugin-providers.json', [{'id': 'existing', 'spec': 'github:example/auth',
                     'models': [{'id': 'model'}]}])
        package = self.folder / 'plugins' / 'node_modules' / 'custom-auth' / 'package.json'
        atomic_json(package, {'version': '1.0'})
        one = next(p for p in read_registry(self.config.settings, policy=policy)['providers'] if p['id'] == 'existing')
        atomic_json(package, {'version': '2.0'})
        two = next(p for p in read_registry(self.config.settings, policy=policy)['providers'] if p['id'] == 'existing')
        self.assertNotEqual(admission_key(one, 'model'), admission_key(two, 'model'))

    def test_transient_refresh_preserves_qualified_result(self):
        from subtitle_tool.mt_maintenance import retained_result
        old = {'roles': ['translate'], 'actual_models': ['draft']}
        self.assertEqual(retained_result(old, {'roles': [], 'retry_after': 999}, same_identity=True), old)
        self.assertEqual(retained_result(old, {'roles': [], 'retry_after': 999}, same_identity=False)['roles'], [])
        self.assertEqual(retained_result(old, {'roles': [], 'retry_after': None}, same_identity=True)['roles'], [])

    def test_daily_admission_budget_survives_restart_and_reserves_before_call(self):
        from subtitle_tool.mt_maintenance import ChannelMaintenance
        policy = self.policy()
        manager = ChannelMaintenance(self.config, self.parent, trial=self.guard)
        self.assertTrue(manager.reserve_evaluation(policy))
        self.assertTrue(manager.reserve_evaluation(policy))
        fresh = ChannelMaintenance(self.config, self.parent, trial=self.guard)
        self.assertFalse(fresh.reserve_evaluation(policy))
        self.assertEqual(fresh.state['budget']['evaluations'], 2)

    def test_policy_rejects_unknown_settings_and_invalid_limits(self):
        from subtitle_tool.mt_channel_policy import load_policy
        policy = self.policy()
        for key, value in [('max_evaluations_per_day', -1), ('refresh_seconds', 0), ('enabledd', True)]:
            changed = {**policy, key: value}
            atomic_json(self.config.data / 'mt-channels.json', changed)
            with self.assertRaises(ToolError):
                load_policy(self.config)

    def test_unknown_paid_candidates_wait_for_explicit_rule(self):
        from subtitle_tool.mt_channel_policy import candidate_allowed
        rule = self.policy()['providers']['existing']
        self.assertEqual(candidate_allowed('new', 'commercial', {}, rule), 'price_unknown')
        rule['preferred'] = ['new']
        self.assertIsNone(candidate_allowed('new', 'commercial', {}, rule))
        self.assertIsNone(candidate_allowed('new', 'free', {}, rule))

    def test_status_never_contains_credentials(self):
        from subtitle_tool.mt_maintenance import ChannelMaintenance
        self.policy()
        manager = ChannelMaintenance(self.config, self.parent, trial=self.guard)
        self.assertNotIn('trial-key', json.dumps(manager.status()))
