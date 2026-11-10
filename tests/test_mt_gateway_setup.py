import json
from pathlib import Path

from subtitle_tool.bailian_free import TRIAL_PROVIDER
from subtitle_tool.common import ToolError, atomic_json
from subtitle_tool.magpie import group_models
from subtitle_tool.mt_gateway_setup import GatewaySetup, choose_roles
from tests.test_bailian_free import BailianFreeFixture


class RoleChoiceTests(BailianFreeFixture):
    def test_same_underlying_model_cannot_fill_independent_roles(self):
        with self.assertRaises(ToolError):
            choose_roles({owner + '/same': {'actual_models': ['same'], 'roles': ['translate', 'review', 'judge']}
                          for owner in ('a', 'b', 'c')})

    def test_independence_and_measured_non_thinking_review(self):
        rows = {m: {'roles': ['translate', 'review', 'judge'], 'actual_models': [m],
                     'duration_ms': 1, 'non_thinking': {'review': m == 'quiet'}} for m in ('fast', 'quiet', 'judge')}
        pools, assignments = choose_roles(rows)
        self.assertEqual(pools['review'], ['quiet'])
        self.assertEqual(len(set(assignments.values())), 3)

    def test_incremental_migration_preserves_manual_member_and_exclusion(self):
        setup = GatewaySetup(self.config, self.parent, manager=self.guard)
        self.assertTrue(setup._group('transcribe-free', ['manual/keep', 'project/a'], 'weight'))
        self.command('group', 'set', 'transcribe-free', 'models-=project/a', 'models+=manual/new')
        self.assertTrue(setup._group('transcribe-free', ['manual/keep', 'project/a', 'project/b'], 'weight'))
        members = self.guard._registry()['groups'][0]['members']
        self.assertIn('manual/new', members)
        self.assertNotIn('project/a', members)

    def test_repeated_migration_preserves_disabled_managed_member(self):
        from subtitle_tool.common import atomic_json
        setup = GatewaySetup(self.config, self.parent, manager=self.guard)
        setup._group('transcribe-free', ['project/a', 'project/b'], 'weight')
        raw = self.guard._registry()
        raw['groups'][0]['off'] = ['project/a']
        atomic_json(self.registry, raw)
        for _ in range(2):
            setup._group('transcribe-free', ['project/a', 'project/b'], 'weight')
        group = self.guard._registry()['groups'][0]
        self.assertIn('project/a', group['members'])
        self.assertIn('project/a', group['off'])

    def test_existing_main_group_is_reordered_free_before_commercial(self):
        setup = GatewaySetup(self.config, self.parent, manager=self.guard)
        setup._group('transcribe', ['group/paid', 'group/free'], 'order')
        setup._group('transcribe', ['group/free', 'group/paid'], 'order')
        group = next(g for g in self.guard._registry()['groups'] if g['id'] == 'transcribe')
        self.assertEqual(group['members'], ['group/free', 'group/paid'])

    def legacy_setup(self, *, managed=False, only_trial=False):
        raw = self.guard._registry()
        raw['providers'][0]['off'] = False
        raw['providers'] += [{'id': 'manual'}, {'id': 'transcribe-bailian'}, {'id': 'transcribe-qiniu-free'}]
        raw['groups'][0]['members'] = [TRIAL_PROVIDER + '/trial-a', TRIAL_PROVIDER + '/trial-b']
        if not only_trial:
            raw['groups'][0]['members'].append('manual/keep')
            raw['groups'][0]['off'] = [TRIAL_PROVIDER + '/trial-b']
        atomic_json(self.registry, raw)
        setup = GatewaySetup(self.config, self.parent, manager=self.guard)
        if managed:
            setup.managed['groups']['transcribe-free'] = []  # A previous incomplete v6 migration.
        evaluations = {'transcribe-bailian/' + name: {'roles': [role], 'actual_models': [name]}
                       for name, role in [('draft', 'translate'), ('reviewer', 'review'), ('arbiter', 'judge')]}
        free = []
        if not only_trial:
            evaluations['transcribe-qiniu-free/qualified'] = {'roles': ['translate'], 'actual_models': ['qualified']}
            free.append('transcribe-qiniu-free/qualified')
        return setup, evaluations, free, raw

    def assert_legacy_retired(self, *, managed=False):
        setup, evaluations, free, before = self.legacy_setup(managed=managed)
        first = setup.apply(evaluations, free)
        setup.apply(evaluations, free)
        raw = self.guard._registry()
        template = next(p for p in raw['providers'] if p['id'] == TRIAL_PROVIDER)
        self.assertEqual(template, {**before['providers'][0], 'off': True})
        enabled = group_models(raw, 'group/transcribe')
        self.assertNotIn(TRIAL_PROVIDER, {owner for owner, _, _ in enabled})
        self.assertIn(('manual', 'keep'), {(owner, model) for owner, model, _ in enabled})
        group = next(g for g in raw['groups'] if g['id'] == 'transcribe-free')
        self.assertNotIn(TRIAL_PROVIDER + '/trial-a', group['members'])
        self.assertIn(TRIAL_PROVIDER + '/trial-b', group['off'])
        self.assertEqual(self.commands.count(('provider', 'off', TRIAL_PROVIDER)), 1)
        backup = json.loads((Path(first['backup']) / 'providers.json').read_bytes())
        self.assertFalse(backup['providers'][0]['off'])

    def test_v5_migration_retires_shared_trial_and_preserves_manual_members(self):
        self.assert_legacy_retired()

    def test_repeated_migration_repairs_already_managed_legacy_members(self):
        self.assert_legacy_retired(managed=True)

    def test_migration_without_free_replacement_keeps_commercial_route_usable(self):
        setup, evaluations, free, _ = self.legacy_setup(only_trial=True)
        setup.apply(evaluations, free)
        raw = self.guard._registry()
        self.assertTrue(next(p for p in raw['providers'] if p['id'] == TRIAL_PROVIDER)['off'])
        self.assertEqual({owner for owner, _, _ in group_models(raw, 'group/transcribe')}, {'transcribe-bailian'})

    def test_migration_stops_before_group_changes_if_shared_trial_stays_enabled(self):
        setup, evaluations, free, before = self.legacy_setup()
        command = self.guard._command
        def ignore_retirement(*args):
            if args == ('provider', 'off', TRIAL_PROVIDER):
                return
            command(*args)
        self.guard._command = ignore_retirement
        with self.assertRaises(ToolError) as caught:
            setup.apply(evaluations, free)
        self.assertEqual(caught.exception.code, 'magpie_group')
        self.assertEqual(self.guard._registry(), before)
        self.assertFalse(setup.managed_path.exists())
