import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock

from subtitle_tool.bailian_free import BailianFreeGuard, TRIAL_PROVIDER
from subtitle_tool.bailian_quota import beijing_today
from subtitle_tool.common import Cancelled, ToolError, atomic_json
from subtitle_tool.config import Config
from subtitle_tool.magpie import validate_group
from subtitle_tool.mt_backend import key_fingerprint
from subtitle_tool.mt_control import MTControl
from tests.fixtures import TemporaryTest


class BailianFreeTests(TemporaryTest):
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
        self.path_registry = self.folder / 'providers.json'
        self.binding = {'version': 2, 'source': 'user_confirmed', 'region': 'cn-beijing',
            'workspace': 'offline-space', 'key_fingerprint': key_fingerprint('trial-key'),
            'cli_fingerprint': 'a' * 64, 'confirmed_models': ['trial-a', 'trial-b'], 'invalidated': False}
        atomic_json(self.config.data / 'bailian-quota-binding.json', self.binding)
        atomic_json(self.path_registry, {'providers': [
            {'id': 'free', 'chat': 'https://openrouter.ai/api/v1'},
            {'id': TRIAL_PROVIDER, 'chat': 'https://offline-space.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
             'key': 'trial-key', 'models': ['trial-a', 'trial-b'], 'countFree': True, 'off': True}],
            'groups': [{'id': 'transcribe-free', 'members': ['free/openrouter/free']},
                       {'id': 'transcribe', 'members': ['group/transcribe-free']}]})
        self.source = mock.Mock()
        self.source.bound_to.return_value = True
        self.rows = [{'model': m, 'type': 'Text', 'remaining': 10000, 'total': 1000000,
                      'expires': str(beijing_today()+timedelta(days=3)), 'autoStop': True, 'remainingPercent': 1}
                     for m in ['trial-a', 'trial-b']]
        self.source.query.side_effect = lambda **kw: json.dumps(self.rows).encode()
        self.now = 0
        self.guard = BailianFreeGuard(self.config, MTControl(threading.Event()), source=self.source,
                                     clock=lambda: self.now)
        self.commands = []
        self.guard._command = self.command

    def command(self, *args):
        self.commands.append(args)
        raw = json.loads(self.path_registry.read_bytes())
        if args[0] == 'provider':
            raw['providers'][1]['off'] = args[1] == 'off'
        else:
            raw['groups'][0]['members'] = args[3].removeprefix('models=').split(',')
        atomic_json(self.path_registry, raw)

    def prepare(self):
        self.guard.prepare(self.config.settings, self.config.settings.mt_base_url)
        validate_group(self.config.settings, 'group/transcribe-free', free_only=True,
                       verified_free=self.guard.allowed)

    def test_valid_models_join_free_group_and_observation_is_shared(self):
        self.prepare()
        self.assertEqual(self.guard.allowed, {(TRIAL_PROVIDER, 'trial-a'), (TRIAL_PROVIDER, 'trial-b')})
        self.prepare()
        self.source.query.assert_called_once()
        self.assertEqual(len(self.commands), 2)

    def test_magpie_cannot_resolve_models_while_provider_is_disabled(self):
        def cli(*args):
            raw = json.loads(self.path_registry.read_bytes())
            if args[0] == 'group' and TRIAL_PROVIDER in args[3] and raw['providers'][1]['off']:
                raise ToolError('magpie knows no model', 'magpie_group')
            self.command(*args)
        self.guard._command = cli
        self.prepare()
        self.assertEqual(len(self.guard.allowed), 2)

    def test_failed_member_update_disables_trial_again(self):
        def cli(*args):
            if args[0] == 'group':
                raise ToolError('group write failed', 'magpie_group')
            self.command(*args)
        self.guard._command = cli
        with self.assertRaises(ToolError):
            self.prepare()
        self.assertTrue(json.loads(self.path_registry.read_bytes())['providers'][1]['off'])

    def test_unreadable_registry_is_a_sanitized_local_failure(self):
        self.path_registry.write_text('private-invalid-registry', encoding='utf-8')
        with self.assertRaises(ToolError) as raised:
            self.prepare()
        self.assertNotIn('private', str(raised.exception))

    def test_each_expired_exhausted_or_unprotected_model_is_removed(self):
        self.prepare()
        self.rows[0]['remaining'] = 0
        self.now += 21
        self.prepare()
        self.assertEqual(self.guard.allowed, {(TRIAL_PROVIDER, 'trial-b')})
        self.rows[1]['autoStop'] = False
        self.now += 21
        self.prepare()
        self.assertFalse(self.guard.allowed)
        raw = json.loads(self.path_registry.read_bytes())
        self.assertTrue(raw['providers'][1]['off'])
        self.assertEqual(raw['groups'][0]['members'], ['free/openrouter/free'])

    def test_query_or_binding_failure_disables_trial_but_keeps_other_free_routes(self):
        self.prepare()
        self.source.bound_to.return_value = False
        self.prepare()
        self.assertFalse(self.guard.allowed)
        self.source.bound_to.return_value = True
        self.source.query.side_effect = RuntimeError('private')
        self.prepare()
        self.assertFalse(self.guard.allowed)

    def test_today_expiry_is_not_accepted(self):
        self.rows[0]['expires'] = str(beijing_today())
        self.rows[1]['autoStop'] = None
        self.prepare()
        self.assertFalse(self.guard.allowed)

    def test_observed_auto_stop_off_revokes_confirmation_across_runs(self):
        self.rows[0]['autoStop'] = False
        self.prepare()
        self.assertEqual(self.guard.allowed, {(TRIAL_PROVIDER, 'trial-b')})
        self.rows[0]['autoStop'] = True
        self.guard = BailianFreeGuard(self.config, MTControl(threading.Event()), source=self.source)
        self.guard._command = self.command
        self.prepare()
        self.assertEqual(self.guard.allowed, {(TRIAL_PROVIDER, 'trial-b')})

    def test_paid_group_cannot_include_trial_and_main_requires_verified_trial(self):
        self.prepare()
        with self.assertRaises(ToolError):
            validate_group(self.config.settings, 'group/transcribe-free', free_only=False, paid_only=True,
                           verified_free=self.guard.allowed)
        with self.assertRaises(ToolError):
            validate_group(self.config.settings, 'group/transcribe', free_only=False)

    def test_changed_provider_key_fails_closed(self):
        self.prepare()
        raw = json.loads(self.path_registry.read_bytes())
        raw['providers'][1]['key'] = 'different'
        atomic_json(self.path_registry, raw)
        self.prepare()
        self.assertFalse(self.guard.allowed)

    def test_changed_local_account_cannot_reuse_old_confirmation(self):
        self.prepare()
        self.config.set_key('another-account')
        self.prepare()
        self.assertFalse(self.guard.allowed)

    def test_failed_observation_has_short_cache_and_is_retried_after_ttl(self):
        self.source.query.side_effect = RuntimeError('private')
        self.prepare()
        self.prepare()
        self.assertEqual(self.source.query.call_count, 1)
        self.now += 21
        self.source.query.side_effect = lambda **kw: json.dumps(self.rows).encode()
        self.prepare()
        self.assertEqual(len(self.guard.allowed), 2)

    def test_failed_disable_aborts_before_sending_request(self):
        self.prepare()
        self.source.bound_to.return_value = False
        self.guard._command = mock.Mock(side_effect=ToolError('failed', 'magpie_group'))
        with self.assertRaises(ToolError):
            self.prepare()

    def test_cancel_after_enable_skips_group_update_and_resume_rechecks(self):
        self.guard._command = lambda *args: BailianFreeGuard._command(self.guard, *args)
        def start(args, **kwargs):
            self.command(*args[1:])
            self.guard.control.cancel_user()
            child = mock.MagicMock(returncode=0)
            child.__enter__.return_value = child
            child.communicate.return_value = (None, None)
            child.poll.return_value = child.wait.return_value = 0
            return child
        with mock.patch('subtitle_tool.bailian_free.subprocess.Popen', side_effect=start) as launch:
            with self.assertRaises(Cancelled):
                self.prepare()
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.commands, [('provider', 'on', TRIAL_PROVIDER)])
        self.assertFalse(self.guard.allowed)
        self.guard.control.cancel.clear()
        self.guard.control.begin()
        self.guard._command = self.command
        self.prepare()
        self.assertEqual(len(self.guard.allowed), 2)
        self.assertEqual(self.commands[-1][0], 'group')

    def test_cancel_during_group_update_does_not_start_a_rollback_command(self):
        self.guard._command = lambda *args: BailianFreeGuard._command(self.guard, *args)
        def start(args, **kwargs):
            self.command(*args[1:])
            if args[1] == 'group':
                self.guard.control.cancel_user(force=True)
            child = mock.MagicMock(returncode=0)
            child.__enter__.return_value = child
            child.communicate.return_value = (None, None)
            child.poll.return_value = child.wait.return_value = 0
            return child
        with mock.patch('subtitle_tool.bailian_free.subprocess.Popen', side_effect=start) as launch:
            with self.assertRaises(Cancelled):
                self.prepare()
        self.assertEqual(launch.call_count, 2)
        self.assertFalse(self.guard.allowed)

    def test_hanging_cli_uses_shared_drain_and_force_kills_and_reaps_it(self):
        real_start = subprocess.Popen
        for mode in ('drain', 'force', 'failure'):
            with self.subTest(mode=mode):
                self.guard.control = MTControl(threading.Event(), drain_seconds=.2 if mode == 'drain' else 5)
                started, children = threading.Event(), []
                failure = ToolError('synthetic sibling failure', 'local_io')
                def start(args, **kwargs):
                    child = real_start([sys.executable, '-I', '-c', 'import time; time.sleep(30)'], **kwargs)
                    children.append(child)
                    started.set()
                    return child
                with mock.patch('subtitle_tool.bailian_free.subprocess.Popen', side_effect=start), ThreadPoolExecutor(1) as pool:
                    future = pool.submit(BailianFreeGuard._command, self.guard, 'provider', 'on', TRIAL_PROVIDER)
                    try:
                        self.assertTrue(started.wait(3))
                        before = time.monotonic()
                        if mode == 'failure':
                            self.guard.control.stop(failure, force=True)
                        else:
                            self.guard.control.cancel_user()
                            if mode == 'force':
                                self.guard.control.cancel_user(force=True)
                        with self.assertRaises(ToolError if mode == 'failure' else Cancelled) as caught:
                            future.result(1.5)
                        if mode == 'failure':
                            self.assertIs(caught.exception, failure)
                        self.assertLess(time.monotonic() - before, 1.2)
                        if mode == 'drain':
                            self.assertGreaterEqual(time.monotonic() - before, .18)
                        self.assertIsNotNone(children[0].returncode)
                    finally:
                        for child in children:
                            if child.poll() is None:
                                child.kill()
                            child.wait(timeout=3)

    def test_stopped_run_does_not_spawn_a_cli_command(self):
        self.guard.control.cancel_user(force=True)
        with mock.patch('subtitle_tool.bailian_free.subprocess.Popen') as launch:
            with self.assertRaises(Cancelled):
                BailianFreeGuard._command(self.guard, 'provider', 'on', TRIAL_PROVIDER)
        launch.assert_not_called()
