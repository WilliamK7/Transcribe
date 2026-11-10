import copy
import json
from unittest import mock
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

    def test_repeat_apply_keeps_policy_version_and_issues_no_commands(self):
        setup, evaluations, free, _ = self.legacy_setup()
        setup.apply(evaluations, free)
        version, count = self.config.settings.mt_policy_version, len(self.commands)
        result = setup.apply(evaluations, free)
        self.assertFalse(result['changed'])
        self.assertEqual(self.config.settings.mt_policy_version, version)
        self.assertEqual(len(self.commands), count)

    def test_rejected_old_role_is_not_reassigned_on_identical_repeat(self):
        setup, evaluations, free, _ = self.legacy_setup()
        setup.apply(evaluations, free)
        evaluations['transcribe-qiniu-free/qualified']['roles'] = ['review', 'judge']
        setup.apply(evaluations, free)
        version = self.config.settings.mt_policy_version
        repeated = GatewaySetup(self.config, self.parent, manager=self.guard).apply(evaluations, free)
        self.assertFalse(repeated['changed'])
        self.assertEqual(self.config.settings.mt_policy_version, version)

    def test_failed_projection_does_not_touch_live_groups(self):
        setup, evaluations, free, before = self.legacy_setup()
        with mock.patch('subtitle_tool.mt_gateway_setup.validate_role_pools', side_effect=ToolError('blocked', 'mt_admission')):
            with self.assertRaises(ToolError):
                setup.apply(evaluations, free)
        self.assertEqual(self.guard._registry(), before)
        self.assertEqual(self.commands, [])
        self.assertIsNone(setup._staged)

    def test_child_write_failure_keeps_parent_and_can_resume(self):
        setup, evaluations, free, _ = self.legacy_setup()
        original_command = self.guard._command
        def fail_second_child(*args):
            if args[:3] == ('group', 'add', 'transcribe-qiniu-free'):
                raise ToolError('simulated write failure', 'magpie_group')
            return original_command(*args)
        self.guard._command = fail_second_child
        with self.assertRaises(ToolError):
            setup.apply(evaluations, free)
        self.assertFalse(any(c[0] == 'group' and c[2] == 'transcribe' for c in self.commands))
        pending = json.loads((self.config.data / 'gateway-apply-pending.json').read_bytes())
        self.assertTrue(Path(pending['backup']).is_dir())
        self.guard._command = original_command
        resumed = GatewaySetup(self.config, self.parent, manager=self.guard)
        resumed.apply(evaluations, free)
        self.assertEqual(json.loads((self.config.data / 'gateway-apply-pending.json').read_bytes())['status'], 'complete')

    def interrupted_existing_groups(self, *, after_write=False):
        setup, original, free, _ = self.legacy_setup()
        setup.apply(original, free)
        desired = copy.deepcopy(original)
        for old, new in [('draft', 'replacement'), ('reviewer', 'new-reviewer')]:
            desired['transcribe-bailian/' + new] = desired.pop('transcribe-bailian/' + old)
            desired['transcribe-bailian/' + new]['actual_models'] = [new]
        command = self.guard._command
        def fail_update(*args):
            target = 'transcribe-bailian' if after_write else 'transcribe-review-bailian'
            if args[:3] == ('group', 'set', target):
                if after_write:
                    command(*args)  # Command committed but the process lost its reply.
                raise ToolError('simulated interruption', 'magpie_group')
            return command(*args)
        self.guard._command = fail_update
        with self.assertRaises(ToolError):
            setup.apply(desired, free)
        self.guard._command = command
        group = next(g for g in self.guard._registry()['groups'] if g['id'] == 'transcribe-bailian')
        self.assertEqual(group['members'], ['transcribe-bailian/replacement'])
        return setup, original, desired, free

    def assert_original_group_restored(self, setup, original, free):
        result = setup.apply(original, free)
        self.assertEqual(result['roles']['translate'], ['transcribe-bailian/draft', 'transcribe-qiniu-free/qualified'])
        group = next(g for g in self.guard._registry()['groups'] if g['id'] == 'transcribe-bailian')
        self.assertEqual(group['members'], ['transcribe-bailian/draft'])
        self.assertNotIn('transcribe-bailian:transcribe-bailian/draft', setup.managed['excluded'])
        version, count = self.config.settings.mt_policy_version, len(self.commands)
        self.assertFalse(setup.apply(original, free)['changed'])
        self.assertEqual(self.config.settings.mt_policy_version, version)
        self.assertEqual(len(self.commands), count)

    def test_retry_after_later_child_failure_restores_managed_ownership(self):
        _, original, _, free = self.interrupted_existing_groups()
        self.assert_original_group_restored(GatewaySetup(self.config, self.parent, manager=self.guard), original, free)

    def test_lost_reply_after_committed_child_does_not_turn_changes_into_manual_edits(self):
        _, original, _, free = self.interrupted_existing_groups(after_write=True)
        self.assert_original_group_restored(GatewaySetup(self.config, self.parent, manager=self.guard), original, free)

    def test_same_manager_retry_does_not_use_unexecuted_target_ownership(self):
        setup, original, desired, free = self.interrupted_existing_groups()
        setup.apply(desired, free)
        review = next(g for g in self.guard._registry()['groups'] if g['id'] == 'transcribe-review-bailian')
        self.assertEqual(review['members'], ['transcribe-bailian/new-reviewer'])
        self.assert_original_group_restored(setup, original, free)

    def test_manual_deletion_after_confirmed_child_update_is_preserved(self):
        _, _, desired, free = self.interrupted_existing_groups()
        self.command('group', 'set', 'transcribe-bailian',
                     'models-=transcribe-bailian/replacement', 'models+=manual/new')
        setup = GatewaySetup(self.config, self.parent, manager=self.guard)
        setup.apply(desired, free)
        group = next(g for g in self.guard._registry()['groups'] if g['id'] == 'transcribe-bailian')
        self.assertEqual(group['members'], ['manual/new'])
        self.assertIn('transcribe-bailian:transcribe-bailian/replacement', setup.managed['excluded'])
        self.assertNotIn('transcribe-bailian:transcribe-bailian/draft', setup.managed['excluded'])

    def test_ambiguous_unconfirmed_write_keeps_registry_and_pending_record(self):
        _, original, _, free = self.interrupted_existing_groups(after_write=True)
        self.command('group', 'set', 'transcribe-bailian', 'models+=manual/new')
        before = self.guard._registry()
        pending = (self.config.data / 'gateway-apply-pending.json').read_bytes()
        count = len(self.commands)
        with self.assertRaises(ToolError) as caught:
            GatewaySetup(self.config, self.parent, manager=self.guard).apply(original, free)
        self.assertEqual(caught.exception.code, 'magpie_group')
        self.assertEqual(self.guard._registry(), before)
        self.assertEqual((self.config.data / 'gateway-apply-pending.json').read_bytes(), pending)
        self.assertEqual(len(self.commands), count)

    def test_legacy_pending_record_recovers_confirmed_child_ownership(self):
        _, original, _, free = self.interrupted_existing_groups()
        path = self.config.data / 'gateway-apply-pending.json'
        pending = json.loads(path.read_bytes())
        atomic_json(path, {k: pending[k] for k in ('backup', 'groups', 'completed')})
        self.assert_original_group_restored(GatewaySetup(self.config, self.parent, manager=self.guard), original, free)
