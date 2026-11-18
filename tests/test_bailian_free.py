"""Model-scoped admission and management regressions; no cloud calls."""
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock

from subtitle_tool.magpie_admin import MagpieAdmin
from subtitle_tool.bailian_free import BailianFreeGuard, TRIAL_PROVIDER, provider_id
from subtitle_tool.bailian_quota import beijing_today
from subtitle_tool.common import Cancelled, atomic_json
from subtitle_tool.config import Config
from subtitle_tool.mt_backend import key_fingerprint
from subtitle_tool.mt_control import MTControl
from tests.fixtures import TemporaryTest


class BailianFreeFixture(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.config = Config(self.path)
        self.config.settings.workspace_id = 'offline-space'
        self.config.set_key('trial-key')
        self.folder = self.path / 'magpie' / 'data'
        self.folder.mkdir(parents=True)
        (self.folder.parent / '.portable').touch()
        (self.folder.parent / 'magpie.com').touch()
        self.config.settings.magpie_data_dir = str(self.folder)
        self.registry = self.folder / 'providers.json'
        atomic_json(self.config.data / 'bailian-quota-binding.json', {
            'version': 2, 'source': 'user_confirmed', 'region': 'cn-beijing',
            'workspace': 'offline-space', 'key_fingerprint': key_fingerprint('trial-key'),
            'cli_fingerprint': 'a' * 64, 'confirmed_models': ['trial-a', 'trial-b'], 'invalidated': False})
        atomic_json(self.registry, {'providers': [{'id': TRIAL_PROVIDER,
            'chat': 'https://offline-space.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
            'key': 'trial-key', 'models': ['trial-a', 'trial-b'], 'countFree': True, 'off': True, 'maxRPM': 60}],
            'groups': [{'id': 'transcribe-free', 'members': ['manual/keep']}]})
        self.rows = [{'model': m, 'type': 'Text', 'remaining': 10000, 'total': 1000000,
            'expires': str(beijing_today() + timedelta(days=3)), 'remainingPercent': 1}
            for m in ['trial-a', 'trial-b', 'trial-c', 'trial-d']]
        self.source = mock.Mock()
        self.source.bound_to.return_value = True
        self.source.query.side_effect = lambda **kw: json.dumps(self.rows).encode()
        self.access = mock.Mock()
        self.access.inspect.return_value = {'rpm': 60, 'tpm': 1000000}
        self.evaluator = mock.Mock()
        self.evaluator.evaluate.side_effect = lambda model: {'roles': ['translate', 'review', 'judge'],
            'actual_models': [model.rsplit('/', 1)[-1]], 'non_thinking': True, 'duration_ms': 10, 'suite': 1}
        self.parent = MTControl(threading.Event())
        self.guard = BailianFreeGuard(self.config, self.parent, source=self.source,
                                     access=self.access, evaluator=self.evaluator)
        self.addCleanup(self.guard.close)
        self.commands = []
        self.guard.admin.command = self.command

    def command(self, *args, control=None):
        (control or self.guard.control).check_start()
        self.commands.append(args)
        raw = json.loads(self.registry.read_bytes())
        if args[0] == 'provider':
            if args[1] == 'add':
                fields = dict(a.split('=', 1) for a in args[3:])
                raw['providers'].append({'id': args[2], 'chat': fields['chat'], 'key': fields['key'],
                                         'models': [fields['models']]})
            else:
                row = next(p for p in raw['providers'] if p['id'] == args[2])
                if args[1] in ('off', 'on'):
                    row['off'] = args[1] == 'off'
                elif args[1] == 'rpm':
                    row['maxRPM'] = int(args[3])
        else:
            row = next((g for g in raw['groups'] if g['id'] == args[2]), None)
            if row is None:
                row = {'id': args[2], 'members': []}
                raw['groups'].append(row)
            for arg in args[3:]:
                field, value = arg.split('=', 1)
                if field == 'models':
                    row['members'] = value.split(',')
                elif field == 'models+':
                    row['members'] += [m for m in value.split(',') if m not in row['members']]
                elif field == 'models-':
                    row['members'] = [m for m in row['members'] if m not in value.split(',')]
                elif field == 'routing':
                    row['routing'] = value
                elif field == 'stays':
                    row['affinity'] = value
                elif field == 'weight':
                    row['weights'] = {m: float(w) for m, w in (item.rsplit('=', 1) for item in value.split(','))}
        atomic_json(self.registry, raw)

    def active(self):
        return {m for m, row in self.guard.state['models'].items() if row['status'] == 'active'}


class BailianFreeTests(BailianFreeFixture):
    def test_suite_upgrade_requalifies_full_pool_without_serving_stale_lanes(self):
        from subtitle_tool.mt_admission import SUITE_VERSION
        self.rows = self.rows[:3]
        with mock.patch('subtitle_tool.mt_admission.SUITE_VERSION', SUITE_VERSION - 1):
            self.guard.refresh()
        self.assertEqual(len(self.active()), 3)
        evaluate = self.evaluator.evaluate.side_effect
        checked = []

        def requalify(member):
            checked.append(member)
            registry = self.guard.admin.registry()
            self.assertFalse(any(member in group['members'] and member not in group.get('off', [])
                                 for group in registry['groups'] if group['id'].startswith('transcribe-trial')))
            result = evaluate(member)
            if member.endswith('/trial-b'):
                result['roles'] = []
            return result

        self.evaluator.evaluate.side_effect = requalify
        for _ in range(4):
            before = len(checked)
            self.guard.refresh()
            self.assertLessEqual(len(checked) - before, 1)
        self.assertCountEqual(checked, [provider_id(row['model']) + '/' + row['model'] for row in self.rows])
        self.assertEqual(self.active(), {'trial-a', 'trial-c'})
        self.assertEqual(self.guard.state['models']['trial-b']['status'], 'admission_rejected')

    def test_interrupted_stale_provider_off_resumes_without_becoming_a_manual_hold(self):
        self.rows = self.rows[:1]
        self.guard.refresh()
        self.guard.state['evaluations'].clear()
        self.guard.trial_state.save(self.guard.control)

        def interrupt(*args):
            self.command(*args)
            if args[:2] == ('provider', 'off'):
                raise OSError('Interruption after the registry commit')

        with mock.patch.object(self.guard.admin, 'command', side_effect=interrupt), self.assertRaises(OSError):
            self.guard.refresh()
        resumed = BailianFreeGuard(self.config, self.parent, source=self.source, access=self.access,
                                   evaluator=self.evaluator, job_id='resume')
        self.addCleanup(resumed.close)
        resumed.admin.command = self.command
        resumed.refresh()
        self.assertEqual(resumed.state['models']['trial-a']['status'], 'active')
        self.assertEqual(self.evaluator.evaluate.call_count, 2)

    def test_cached_new_qualification_cannot_reenable_the_previous_role(self):
        from subtitle_tool.magpie import group_models
        self.rows = self.rows[:1]
        self.guard.refresh()
        evaluation, = self.guard.state['evaluations'].values()
        evaluation['roles'] = ['review']
        self.guard.state['reserved_models'] = {'trial-a': 'review'}
        self.guard.trial_state.save(self.guard.control)
        self.guard.refresh()
        self.assertEqual(self.guard.state['models']['trial-a']['role'], 'review')
        raw = self.guard.admin.registry()
        self.assertEqual(group_models(raw, 'group/transcribe-trial'), [])
        self.assertEqual([leaf[1] for leaf in group_models(raw, 'group/transcribe-trial-review')], ['trial-a'])
        self.assertEqual(self.evaluator.evaluate.call_count, 1)

    def test_retiring_an_old_qualification_preserves_external_group_membership(self):
        self.rows = self.rows[:1]
        self.guard.refresh()
        member = provider_id('trial-a') + '/trial-a'
        self.command('group', 'add', 'transcribe-personal', 'models=manual/keep,' + member)
        self.guard.state['evaluations'].clear()
        self.guard.trial_state.save(self.guard.control)
        self.guard.refresh()
        groups = {group['id']: group for group in self.guard.admin.registry()['groups']}
        self.assertIn(member, groups['transcribe-personal']['members'])
        self.assertFalse(self.active())
        self.assertEqual(self.evaluator.evaluate.call_count, 1)

    def test_stale_qualification_is_retired_before_quota_discovery_and_preserves_manual_off(self):
        self.guard.refresh()
        raw = self.guard.admin.registry()
        next(p for p in raw['providers'] if p['id'] == provider_id('trial-b'))['off'] = True
        atomic_json(self.registry, raw)
        self.guard.state['evaluations'].clear()
        self.guard.trial_state.save(self.guard.control)
        with mock.patch.object(self.guard, '_run'):
            self.guard.prepare(self.config.settings.mt_base_url)
        self.assertFalse(self.active())
        self.source.query.side_effect = RuntimeError('temporary outage')
        self.guard.refresh()
        self.assertFalse(self.active())
        self.assertTrue(self.guard.state['models']['trial-b'].get('manual_hold'))
        self.assertFalse(any(not p.get('off') for p in self.guard.admin.registry()['providers']))

    def test_first_admission_uses_policy_judge_and_skips_same_underlying_model(self):
        from subtitle_tool.mt_channel_policy import default_policy
        self.rows = self.rows[:1]
        policy = default_policy()
        policy['judges'] = ['configured/trial-a', 'configured/independent']
        atomic_json(self.config.data / 'mt-channels.json', policy)
        raw = self.guard.admin.registry()
        raw['providers'].append({'id': 'configured', 'models': ['trial-a', 'independent']})
        atomic_json(self.registry, raw)
        self.guard.state['admission_judge'] = 'retired/old-judge'
        self.guard.evaluator = None
        with mock.patch('subtitle_tool.bailian_free.ModelEvaluator', return_value=self.evaluator) as create:
            self.guard.refresh()
        self.assertEqual(create.call_args.kwargs['judge'], 'configured/independent')
        self.assertEqual(self.active(), {'trial-a'})

    def test_missing_independent_judge_defers_before_creating_or_querying_lane(self):
        from subtitle_tool.mt_channel_policy import default_policy
        self.rows = self.rows[:1]
        policy = default_policy()
        policy['judges'] = ['configured/trial-a', 'disabled/independent']
        atomic_json(self.config.data / 'mt-channels.json', policy)
        raw = self.guard.admin.registry()
        raw['providers'] += [{'id': 'configured'}, {'id': 'disabled', 'off': True}]
        atomic_json(self.registry, raw)
        self.guard.evaluator = None
        with mock.patch('subtitle_tool.bailian_free.ModelEvaluator', return_value=self.evaluator) as create:
            self.guard.refresh()
        create.assert_not_called()
        self.access.inspect.assert_not_called()
        self.assertEqual(self.commands, [])
        self.assertFalse(self.active())

    def test_third_unlisted_model_replaces_exhausted_candidates(self):
        self.rows[0]['remaining'] = self.rows[1]['remaining'] = 0
        self.guard.refresh()
        self.assertEqual(self.active(), {'trial-c', 'trial-d'})
        self.assertNotIn(('provider', 'on', TRIAL_PROVIDER), self.commands)

    def test_single_model_lanes_have_distinct_provider_identity(self):
        self.guard.refresh()
        additions = [c for c in self.commands if c[:2] == ('provider', 'add')]
        self.assertEqual(len(additions), 3)
        for command in additions:
            fields = dict(a.split('=', 1) for a in command[3:])
            self.assertEqual(fields.get('header.X-Transcribe-Trial-Model'), fields['models'])

    def test_bounded_admission_stops_at_one_active_and_two_reserves_and_reuses_cache(self):
        self.guard.refresh()
        self.assertEqual(len(self.active()), 3)
        self.assertEqual(self.evaluator.evaluate.call_count, 3)
        self.guard.refresh()
        self.assertEqual(self.evaluator.evaluate.call_count, 3)
        self.assertEqual(len({r['provider'] for r in self.guard.state['models'].values()}), 3)

    def test_review_only_candidates_cannot_fill_the_last_missing_translation_slot(self):
        evaluate = self.evaluator.evaluate.side_effect
        def roles(model):
            value = evaluate(model)
            if model.endswith(('trial-a', 'trial-b', 'trial-c')):
                value['roles'] = ['review', 'judge']
            return value
        self.evaluator.evaluate.side_effect = roles
        self.guard.refresh()
        self.assertIn('trial-d', self.active())
        self.assertEqual(self.guard.state['models']['trial-d']['role'], 'translate')

    def test_http_200_with_failed_attempt_removes_only_exhausted_model(self):
        self.guard.refresh()
        self.guard.observe_route({'status': 200, 'tries': [
            {'id': provider_id('trial-a'), 'model': 'trial-a', 'status': 403,
             'fail': 'credit', 'error': 'Free quota exhausted. private-upstream-error'},
            {'id': provider_id('trial-b'), 'model': 'trial-b', 'status': 200}]})
        self.guard.refresh()  # Deliberately stale, positive quota observation.
        self.assertNotIn('trial-a', self.active())
        self.assertIn('trial-b', self.active())
        self.assertIn('trial-d', self.active())
        audit = (self.path / '.build/mt-diagnostics/trial-management.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('private-upstream-error', audit)
        self.assertNotIn('trial-key', audit)
        self.assertIn('quota_exhausted', audit)

    def test_exhaustion_between_admission_and_join_cannot_reenable_lane(self):
        from subtitle_tool.common import ToolError
        self.guard.refresh()
        self.guard.observe_route({'tries': [{'id': provider_id('trial-a'), 'model': 'trial-a',
                                            'status': 403, 'error': 'Free quota exhausted'}]})
        template = next(p for p in self.guard.admin.registry()['providers'] if p['id'] == TRIAL_PROVIDER)
        with self.assertRaises(ToolError):
            self.guard._lane(template, 'trial-a', {})
        lane = next(p for p in self.guard.admin.registry()['providers'] if p['id'] == provider_id('trial-a'))
        self.assertTrue(lane['off'])

    def test_query_failure_preserves_known_candidates_and_does_not_raise(self):
        self.guard.refresh()
        before = self.active()
        self.source.query.side_effect = RuntimeError('private')
        self.guard.refresh()
        self.assertEqual(self.active(), before)
        self.assertEqual(self.guard.state['quota_status'], 'unknown')

    def test_manual_pause_and_unrelated_group_members_survive_refresh(self):
        self.guard.refresh()
        raw = json.loads(self.registry.read_bytes())
        next(p for p in raw['providers'] if p['id'] == provider_id('trial-a'))['off'] = True
        raw['groups'][0]['members'].append('manual/concurrent-edit')
        atomic_json(self.registry, raw)
        self.guard.refresh()
        self.assertNotIn('trial-a', self.active())
        current = json.loads(self.registry.read_bytes())['groups'][0]['members']
        self.assertIn('manual/concurrent-edit', current)
        self.assertIn('manual/keep', current)
        self.assertNotIn(('provider', 'on', provider_id('trial-a')), self.commands)

    def test_removed_or_disabled_group_member_is_not_reintroduced(self):
        self.guard.refresh()
        raw = json.loads(self.registry.read_bytes())
        group = next(g for g in raw['groups'] if g['id'] == 'transcribe-trial')
        group['off'] = [provider_id('trial-a') + '/trial-a']
        atomic_json(self.registry, raw)
        self.guard.refresh()
        self.assertNotIn('trial-a', self.active())
        self.assertEqual(self.guard.state['models']['trial-a']['status'], 'user_paused')

    def test_pause_disables_last_model_even_when_group_cannot_be_emptied(self):
        self.guard.refresh()
        state = json.loads(self.guard.trial_state.path.read_bytes())
        state['paused'] = True
        atomic_json(self.guard.trial_state.path, state)
        self.guard.refresh()
        from subtitle_tool.magpie import group_models
        raw = json.loads(self.registry.read_bytes())
        self.assertEqual(group_models(raw, 'group/transcribe-trial'), [])

    def test_shared_budget_includes_existing_commercial_provider(self):
        raw = json.loads(self.registry.read_bytes())
        raw['providers'].append({**raw['providers'][0], 'id': 'transcribe-bailian', 'off': False})
        atomic_json(self.registry, raw)
        self.guard.refresh()
        raw = json.loads(self.registry.read_bytes())
        enabled = [p for p in raw['providers'] if not p.get('off')]
        self.assertLessEqual(sum(p['maxRPM'] for p in enabled), 60)

    def test_insufficient_shared_budget_does_not_create_extra_capacity(self):
        self.access.inspect.return_value['workspace_rpm'] = 3
        self.guard.refresh()
        self.assertFalse(self.active())

    def test_removed_parent_group_is_not_reintroduced_by_replenishment(self):
        self.guard.refresh()
        self.command('group', 'set', 'transcribe-free', 'models-=group/transcribe-trial')
        self.rows[0]['remaining'] = 0
        self.guard.refresh()
        parent = next(g for g in self.guard.admin.registry()['groups'] if g['id'] == 'transcribe-free')
        self.assertNotIn('group/transcribe-trial', parent['members'])

    def test_manual_member_removal_is_preserved_in_a_new_task(self):
        self.guard.refresh()
        self.command('group', 'set', 'transcribe-trial', 'models+=manual/fallback',
                     'models-=' + provider_id('trial-a') + '/trial-a')
        self.guard.refresh()
        for row in self.rows[1:]:
            row['remaining'] = 0
        next_task = BailianFreeGuard(self.config, self.parent, source=self.source,
                                    access=self.access, evaluator=self.evaluator, job_id='new-task')
        next_task.admin.command = self.command
        next_task.refresh()
        self.assertNotEqual(next_task.state['models']['trial-a']['status'], 'active')

    def test_interrupted_admission_lane_is_tracked_for_restart(self):
        self.evaluator.evaluate.side_effect = Cancelled()
        with self.assertRaises(Cancelled):
            self.guard.refresh()
        state = json.loads(self.guard.trial_state.path.read_bytes())
        self.assertEqual(state['models']['trial-a']['status'], 'evaluating')

    def test_exhaustion_is_processed_while_new_candidate_is_being_evaluated(self):
        self.guard.refresh()
        self.rows[2]['remaining'] = 0
        self.guard.next_refresh = 0
        entered, release = threading.Event(), threading.Event()
        evaluate = self.evaluator.evaluate.side_effect
        def slow(model):
            entered.set()
            release.wait(3)
            return evaluate(model)
        self.evaluator.evaluate.side_effect = slow
        self.guard.prepare(self.config.settings.mt_base_url)
        self.assertTrue(entered.wait(2))
        folder = self.folder / 'routing'
        folder.mkdir(exist_ok=True)
        (folder / 'test.jsonl').write_text(json.dumps({'status': 200, 'tries': [
            {'id': provider_id('trial-a'), 'model': 'trial-a', 'status': 403,
             'error': 'Free quota exhausted.'}]}) + '\n', encoding='utf-8')
        deadline = time.monotonic() + 2
        while 'trial-a' in self.active() and time.monotonic() < deadline:
            time.sleep(.05)
        release.set()
        self.guard.close()
        self.assertNotIn('trial-a', self.active())

    def test_account_change_removes_all_trial_routes(self):
        self.guard.refresh()
        self.config.set_key('different-account')
        self.guard.refresh()
        self.assertFalse(self.active())

    def test_changed_binding_is_removed_before_background_query_or_dispatch(self):
        self.guard.refresh()
        self.config.set_key('other-account')
        self.guard.prepare(self.config.settings.mt_base_url)
        self.assertFalse(self.active())
        self.assertIsNone(self.guard.thread)

    def test_changed_lane_credentials_stop_only_that_model_before_dispatch(self):
        self.guard.refresh()
        raw = json.loads(self.registry.read_bytes())
        next(p for p in raw['providers'] if p['id'] == provider_id('trial-a'))['key'] = 'changed-key'
        atomic_json(self.registry, raw)
        self.guard._run = mock.Mock()
        self.guard.prepare(self.config.settings.mt_base_url)
        self.assertEqual(self.active(), {'trial-b', 'trial-c'})
        self.assertEqual(self.guard.state['models']['trial-a']['status'], 'identity_changed')

    def test_missing_template_stops_managed_lanes_before_dispatch(self):
        self.guard.refresh()
        raw = json.loads(self.registry.read_bytes())
        raw['providers'] = [p for p in raw['providers'] if p['id'] != TRIAL_PROVIDER]
        atomic_json(self.registry, raw)
        self.guard.prepare(self.config.settings.mt_base_url)
        self.assertFalse(self.active())
        self.assertIsNone(self.guard.thread)

    def test_all_exhausted_keeps_registry_but_no_live_trial_candidates(self):
        self.guard.refresh()
        for row in self.rows:
            row['remaining'] = 0
        self.guard.refresh()
        self.assertFalse(self.active())
        self.assertEqual(self.guard.state['pool_status'], 'no_candidates')

    def test_prepare_is_single_flight_and_does_not_wait_for_cloud(self):
        reached, release = threading.Event(), threading.Event()
        def query(**kw):
            reached.set()
            release.wait(3)
            return b'[]'
        self.source.query.side_effect = query
        start = time.monotonic()
        for _ in range(16):
            self.guard.prepare(self.config.settings.mt_base_url)
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(reached.wait(2))
        release.set()
        self.guard.close()
        self.source.query.assert_called_once()

    def test_fragmented_log_is_retried_from_last_complete_line(self):
        self.guard.refresh()
        folder = self.folder / 'routing'
        folder.mkdir()
        path = folder / 'synthetic.jsonl'
        route = json.dumps({'tries': [{'id': provider_id('trial-a'), 'model': 'trial-a',
                                       'error': 'Free quota exhausted', 'status': 403}]}).encode()
        path.write_bytes(route[:20])
        self.guard.poll_routes()
        self.assertIn('trial-a', self.active())
        path.write_bytes(route + b'\n')
        self.guard.poll_routes()
        self.assertNotIn('trial-a', self.active())

    def test_hanging_cli_cancel_kills_and_reaps_child(self):
        real_start = subprocess.Popen
        started, children = threading.Event(), []
        def start(args, **kwargs):
            child = real_start([sys.executable, '-I', '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(child)
            started.set()
            return child
        with mock.patch('subtitle_tool.magpie_admin.subprocess.Popen', side_effect=start), ThreadPoolExecutor(1) as pool:
            future = pool.submit(MagpieAdmin.command, self.guard.admin, 'provider', 'on', TRIAL_PROVIDER)
            self.assertTrue(started.wait(3))
            self.parent.cancel_user()
            with self.assertRaises(Cancelled):
                future.result(2)
            self.assertIsNotNone(children[0].returncode)

    def test_stopped_run_never_starts_cli(self):
        self.parent.cancel_user()
        with mock.patch('subtitle_tool.magpie_admin.subprocess.Popen') as launch:
            with self.assertRaises(Cancelled):
                MagpieAdmin.command(self.guard.admin, 'provider', 'on', TRIAL_PROVIDER)
        launch.assert_not_called()
