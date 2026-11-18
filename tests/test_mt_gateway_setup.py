import copy
import io
import json
import runpy
import sys
from unittest import mock
from pathlib import Path

from subtitle_tool.bailian_free import TRIAL_PROVIDER
from subtitle_tool.common import ToolError, atomic_json
from subtitle_tool.magpie import group_models
from subtitle_tool.mt_gateway_setup import GatewaySetup, GroupPlan, choose_roles
from tests.test_bailian_free import BailianFreeFixture


class RoleChoiceTests(BailianFreeFixture):
    def test_role_status_counts_distinct_models_and_shows_group_ownership(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'manage-mt.py'
        raw = {'providers': [{'id': owner, 'models': ['same'], 'key': 'private-key', 'countFree': True,
                              'proxy': 'https://private-proxy', 'listOff': True} for owner in ('a', 'b')],
               'groups': [{'id': 'transcribe', 'members': ['group/middle']},
                          {'id': 'middle', 'members': ['group/transcribe-paid']},
                          {'id': 'transcribe-paid', 'members': ['a/same', 'b/same']}]}
        atomic_json(self.registry, raw)
        atomic_json(self.config.data / 'gateway-managed.json', {'version': 1,
                    'groups': {'transcribe-paid': ['a/same']}, 'excluded': []})
        (self.folder / 'plugin-auth.json').write_bytes(b'invalid private credentials')
        with mock.patch.object(sys, 'path', sys.path.copy()), mock.patch.object(sys, 'argv', [str(script), '--status']), \
                mock.patch('subtitle_tool.config.Config', return_value=self.config), \
                mock.patch.object(self.config, 'get_key', side_effect=AssertionError('unneeded key')), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(runpy.run_path(str(script))['main'](), 0)
        result = json.loads(out.getvalue())['gateway']
        entry = result['roles']['translate']['primary']
        self.assertEqual((entry['channel_count'], entry['model_count']), (2, 1))
        self.assertEqual(entry['billing_counts'], {'commercial': 2})
        self.assertTrue(all(r['magpie_count_free'] for r in entry['members']))
        self.assertEqual([r['paths'][0]['member_owner'] for r in entry['members']], ['project', 'manual'])
        self.assertEqual(entry['members'][0]['paths'][0]['groups'],
                         ['group/transcribe', 'group/middle', 'group/transcribe-paid'])
        self.assertNotIn('private-', out.getvalue())
        self.assertEqual(json.loads(self.registry.read_bytes()), raw)
        self.assertEqual(self.commands, [])

    def test_manual_apply_skips_overpriced_candidate_before_exposure_and_inference(self):
        from subtitle_tool.mt_channel_policy import default_policy
        from subtitle_tool.mt_catalog import Catalog
        from subtitle_tool.mt_maintenance import ChannelMaintenance
        raw = self.guard.admin.registry()
        raw['providers'].append({'id': 'vercel', 'models': ['expensive']})
        raw['groups'] = []
        atomic_json(self.registry, raw)
        policy = default_policy()
        policy['providers']['vercel']['preferred'] = ['expensive']
        atomic_json(self.config.data / 'mt-channels.json', policy)
        discovered = {'identities': {'vercel/expensive': {'models': ['expensive'], 'dynamic': False,
                      'billing': 'commercial'}}, 'models': {'vercel': {'expensive': {
                      'pricing': {'input': '0.0001', 'output': '0.0001'}}}}}
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'configure-mt.py'
        def discover(catalog, *args, **kwargs):
            catalog.pending.add('vercel/expensive')
            return discovered

        with mock.patch.object(sys, 'path', sys.path.copy()), mock.patch.object(sys, 'argv', [str(script), '--apply']), \
                mock.patch('subtitle_tool.config.Config', return_value=self.config), \
                mock.patch.object(Catalog, 'discover', discover), \
                mock.patch.object(GatewaySetup, 'independent_judge', return_value='other/judge'), \
                mock.patch.object(GatewaySetup, 'evaluate', return_value=None) as evaluate, \
                mock.patch.object(ChannelMaintenance, 'expose') as expose, \
                mock.patch.object(GatewaySetup, 'apply', return_value={}), mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(runpy.run_path(str(script))['main'](), 0)
        evaluate.assert_not_called()
        expose.assert_not_called()
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertIn({'model': 'vercel/expensive', 'reason': 'price_limit'},
                      next(event['skipped'] for event in events if 'skipped' in event))

    def test_read_only_setup_defers_trial_state_but_apply_rejects_corruption(self):
        path = self.config.data / 'bailian-trial-state.json'
        path.write_bytes(b'invalid trial state')
        before = self.registry.read_bytes()
        setup = GatewaySetup(self.config, self.parent)
        self.assertEqual(setup.current_members(), {})
        with self.assertRaises(ToolError) as caught:
            setup.apply({})
        self.assertEqual(caught.exception.code, 'mt_config')
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(path.read_bytes(), b'invalid trial state')
        self.assertEqual(self.commands, [])

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

    def test_incremental_update_preserves_manual_member_and_exclusion(self):
        ownership = {'groups': {'transcribe-free': ['manual/keep', 'project/a']}, 'excluded': []}
        current = [{'id': 'transcribe-free', 'members': ['manual/keep', 'manual/new']}]
        plan = GroupPlan(current, ownership)
        self.assertTrue(plan.set_group('transcribe-free', ['manual/keep', 'project/a', 'project/b'], 'weight'))
        members = plan.groups['transcribe-free']['members']
        self.assertIn('manual/new', members)
        self.assertNotIn('project/a', members)
        self.assertIn('project/b', members)
        self.assertIn('transcribe-free:project/a', plan.ownership['excluded'])
        self.assertEqual(ownership['excluded'], [])
        self.assertEqual(current[0]['members'], ['manual/keep', 'manual/new'])
        self.assertEqual(self.commands, [])

    def test_repeated_update_preserves_disabled_managed_member(self):
        members = ['project/a', 'project/b']
        current = [{'id': 'transcribe-free', 'members': members, 'off': ['project/a']}]
        plan = GroupPlan(current, {'groups': {'transcribe-free': members}, 'excluded': []})
        for _ in range(2):
            plan.set_group('transcribe-free', members, 'weight')
        group = plan.groups['transcribe-free']
        self.assertIn('project/a', group['members'])
        self.assertIn('project/a', group['off'])

    def test_existing_main_group_is_reordered_free_before_commercial(self):
        plan = GroupPlan([], {'groups': {}, 'excluded': []})
        plan.set_group('transcribe', ['group/paid', 'group/free'], 'order')
        plan.set_group('transcribe', ['group/free', 'group/paid'], 'order')
        group = plan.groups['transcribe']
        self.assertEqual(group['members'], ['group/free', 'group/paid'])
        for command, expected in plan.changes:
            self.command(*command)
            actual = next(g for g in self.guard.admin.registry()['groups'] if g['id'] == 'transcribe')
            self.assertEqual(actual, expected)

    def gateway_setup(self, *, paid_only=False):
        raw = self.guard.admin.registry()
        raw['providers'] += [{'id': 'manual'}, {'id': 'transcribe-bailian'}, {'id': 'transcribe-qiniu-free'}]
        if paid_only:
            raw['groups'] = []
        atomic_json(self.registry, raw)
        setup = GatewaySetup(self.config, self.parent, admin=self.guard.admin, trial_state=self.guard.trial_state)
        evaluations = {'transcribe-bailian/' + name: {'roles': [role], 'actual_models': [name]}
                       for name, role in [('draft', 'translate'), ('reviewer', 'review'), ('arbiter', 'judge')]}
        free = []
        if not paid_only:
            evaluations['transcribe-qiniu-free/qualified'] = {'roles': ['translate'], 'actual_models': ['qualified']}
            free.append('transcribe-qiniu-free/qualified')
        return setup, evaluations, free, raw

    def test_apply_preserves_manual_members_and_disables_account_template(self):
        setup, evaluations, free, before = self.gateway_setup()
        before['providers'][0]['off'] = False
        atomic_json(self.registry, before)
        first = setup.apply(evaluations, free)
        setup.apply(evaluations, free)
        raw = self.guard.admin.registry()
        template = next(p for p in raw['providers'] if p['id'] == TRIAL_PROVIDER)
        self.assertEqual(template, {**before['providers'][0], 'off': True})
        enabled = group_models(raw, 'group/transcribe')
        self.assertNotIn(TRIAL_PROVIDER, {owner for owner, _, _ in enabled})
        self.assertIn(('manual', 'keep'), {(owner, model) for owner, model, _ in enabled})
        self.assertEqual(self.commands.count(('provider', 'off', TRIAL_PROVIDER)), 1)
        backup = json.loads((Path(first['backup']) / 'providers.json').read_bytes())
        self.assertFalse(backup['providers'][0]['off'])

    def test_apply_without_free_candidates_keeps_commercial_route_usable(self):
        setup, evaluations, free, _ = self.gateway_setup(paid_only=True)
        setup.apply(evaluations, free)
        raw = self.guard.admin.registry()
        self.assertTrue(next(p for p in raw['providers'] if p['id'] == TRIAL_PROVIDER)['off'])
        self.assertEqual({owner for owner, _, _ in group_models(raw, 'group/transcribe')}, {'transcribe-bailian'})

    def test_apply_stops_before_group_changes_if_account_template_stays_enabled(self):
        setup, evaluations, free, before = self.gateway_setup()
        before['providers'][0]['off'] = False
        atomic_json(self.registry, before)
        command = self.guard.admin.command
        def ignore_disable(*args, **kwargs):
            if args == ('provider', 'off', TRIAL_PROVIDER):
                return
            command(*args, **kwargs)
        self.guard.admin.command = ignore_disable
        with self.assertRaises(ToolError) as caught:
            setup.apply(evaluations, free)
        self.assertEqual(caught.exception.code, 'magpie_group')
        self.assertEqual(self.guard.admin.registry(), before)
        self.assertFalse(setup.managed_path.exists())

    def test_repeat_apply_keeps_policy_version_and_issues_no_commands(self):
        setup, evaluations, free, _ = self.gateway_setup()
        setup.apply(evaluations, free)
        version, count = self.config.settings.mt_policy_version, len(self.commands)
        result = setup.apply(evaluations, free)
        self.assertFalse(result['changed'])
        self.assertEqual(self.config.settings.mt_policy_version, version)
        self.assertEqual(len(self.commands), count)

    def test_rejected_old_role_is_not_reassigned_on_identical_repeat(self):
        setup, evaluations, free, _ = self.gateway_setup()
        setup.apply(evaluations, free)
        evaluations['transcribe-qiniu-free/qualified']['roles'] = ['review', 'judge']
        setup.apply(evaluations, free)
        version = self.config.settings.mt_policy_version
        repeated = GatewaySetup(self.config, self.parent, admin=self.guard.admin, trial_state=self.guard.trial_state).apply(evaluations, free)
        self.assertFalse(repeated['changed'])
        self.assertEqual(self.config.settings.mt_policy_version, version)

    def test_failed_projection_does_not_touch_live_groups(self):
        setup, evaluations, free, before = self.gateway_setup()
        with mock.patch('subtitle_tool.mt_gateway_setup.validate_role_pools', side_effect=ToolError('blocked', 'mt_admission')):
            with self.assertRaises(ToolError):
                setup.apply(evaluations, free)
        self.assertEqual(self.guard.admin.registry(), before)
        self.assertEqual(self.commands, [])
        self.assertFalse(setup.plan_path.exists())

    def test_child_write_failure_keeps_parent_and_can_resume(self):
        setup, evaluations, free, _ = self.gateway_setup()
        original_command = self.guard.admin.command
        def fail_second_child(*args, **kwargs):
            if args[:3] == ('group', 'add', 'transcribe-qiniu-free'):
                raise ToolError('simulated write failure', 'magpie_group')
            return original_command(*args, **kwargs)
        self.guard.admin.command = fail_second_child
        with self.assertRaises(ToolError):
            setup.apply(evaluations, free)
        self.assertFalse(any(c[0] == 'group' and c[2] == 'transcribe' for c in self.commands))
        pending = json.loads((self.config.data / 'gateway-apply-pending.json').read_bytes())
        self.assertTrue(Path(pending['backup']).is_dir())
        self.guard.admin.command = original_command
        resumed = GatewaySetup(self.config, self.parent, admin=self.guard.admin, trial_state=self.guard.trial_state)
        resumed.apply(evaluations, free)
        self.assertEqual(json.loads((self.config.data / 'gateway-apply-pending.json').read_bytes())['status'], 'complete')

    def interrupted_existing_groups(self, *, after_write=False):
        setup, original, free, _ = self.gateway_setup()
        setup.apply(original, free)
        desired = copy.deepcopy(original)
        for old, new in [('draft', 'replacement'), ('reviewer', 'new-reviewer')]:
            desired['transcribe-bailian/' + new] = desired.pop('transcribe-bailian/' + old)
            desired['transcribe-bailian/' + new]['actual_models'] = [new]
        command = self.guard.admin.command
        def fail_update(*args, **kwargs):
            target = 'transcribe-bailian' if after_write else 'transcribe-review-bailian'
            if args[:3] == ('group', 'set', target):
                if after_write:
                    command(*args, **kwargs)  # Command committed but the process lost its reply.
                raise ToolError('simulated interruption', 'magpie_group')
            return command(*args, **kwargs)
        self.guard.admin.command = fail_update
        with self.assertRaises(ToolError):
            setup.apply(desired, free)
        self.guard.admin.command = command
        group = next(g for g in self.guard.admin.registry()['groups'] if g['id'] == 'transcribe-bailian')
        self.assertEqual(group['members'], ['transcribe-bailian/replacement'])
        return setup, original, desired, free

    def assert_original_group_restored(self, setup, original, free):
        result = setup.apply(original, free)
        self.assertEqual(result['roles']['translate'], ['transcribe-bailian/draft', 'transcribe-qiniu-free/qualified'])
        group = next(g for g in self.guard.admin.registry()['groups'] if g['id'] == 'transcribe-bailian')
        self.assertEqual(group['members'], ['transcribe-bailian/draft'])
        self.assertNotIn('transcribe-bailian:transcribe-bailian/draft', setup.managed['excluded'])
        version, count = self.config.settings.mt_policy_version, len(self.commands)
        self.assertFalse(setup.apply(original, free)['changed'])
        self.assertEqual(self.config.settings.mt_policy_version, version)
        self.assertEqual(len(self.commands), count)

    def test_retry_after_later_child_failure_restores_managed_ownership(self):
        _, original, _, free = self.interrupted_existing_groups()
        self.assert_original_group_restored(GatewaySetup(self.config, self.parent, admin=self.guard.admin, trial_state=self.guard.trial_state), original, free)

    def test_lost_reply_after_committed_child_does_not_turn_changes_into_manual_edits(self):
        _, original, _, free = self.interrupted_existing_groups(after_write=True)
        self.assert_original_group_restored(GatewaySetup(self.config, self.parent, admin=self.guard.admin, trial_state=self.guard.trial_state), original, free)

    def test_same_manager_retry_does_not_use_unexecuted_target_ownership(self):
        setup, original, desired, free = self.interrupted_existing_groups()
        setup.apply(desired, free)
        review = next(g for g in self.guard.admin.registry()['groups'] if g['id'] == 'transcribe-review-bailian')
        self.assertEqual(review['members'], ['transcribe-bailian/new-reviewer'])
        self.assert_original_group_restored(setup, original, free)

    def test_manual_deletion_after_confirmed_child_update_is_preserved(self):
        _, _, desired, free = self.interrupted_existing_groups()
        self.command('group', 'set', 'transcribe-bailian',
                     'models-=transcribe-bailian/replacement', 'models+=manual/new')
        setup = GatewaySetup(self.config, self.parent, admin=self.guard.admin, trial_state=self.guard.trial_state)
        setup.apply(desired, free)
        group = next(g for g in self.guard.admin.registry()['groups'] if g['id'] == 'transcribe-bailian')
        self.assertEqual(group['members'], ['manual/new'])
        self.assertIn('transcribe-bailian:transcribe-bailian/replacement', setup.managed['excluded'])
        self.assertNotIn('transcribe-bailian:transcribe-bailian/draft', setup.managed['excluded'])

    def test_ambiguous_unconfirmed_write_keeps_registry_and_pending_record(self):
        _, original, _, free = self.interrupted_existing_groups(after_write=True)
        self.command('group', 'set', 'transcribe-bailian', 'models+=manual/new')
        before = self.guard.admin.registry()
        pending = (self.config.data / 'gateway-apply-pending.json').read_bytes()
        count = len(self.commands)
        with self.assertRaises(ToolError) as caught:
            GatewaySetup(self.config, self.parent, admin=self.guard.admin, trial_state=self.guard.trial_state).apply(original, free)
        self.assertEqual(caught.exception.code, 'magpie_group')
        self.assertEqual(self.guard.admin.registry(), before)
        self.assertEqual((self.config.data / 'gateway-apply-pending.json').read_bytes(), pending)
        self.assertEqual(len(self.commands), count)
