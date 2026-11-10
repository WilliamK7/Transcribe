"""Offline quota schema and account-context boundaries; no real credentials."""
import json
import subprocess
import sys
import threading
from datetime import date
from unittest import mock

from subtitle_tool.bailian_quota import CLIQuotaSource, QuotaSourceError, VerifiedIdentity, parse_observation
from subtitle_tool.common import atomic_json
from subtitle_tool.mt_control import MTControl
from tests.fixtures import TemporaryTest


class QuotaObservationTests(TemporaryTest):
    def row(self, **changes):
        return dict({'model': 'trial-model', 'type': 'Text', 'remaining': 100, 'total': 1000,
                     'remainingPercent': 10, 'expires': '2099-01-01', 'autoStop': True}, **changes)

    def test_ambiguous_or_malformed_quota_is_never_admitted(self):
        for changes in ({'remaining': True}, {'remaining': 1001}, {'remaining': -1}, {'remaining': 0},
                        {'total': 0}, {'autoStop': 'true'}, {'type': 'Audio'},
                        {'expires': '2099-02-30'}, {'remainingPercent': None}):
            with self.subTest(changes=changes):
                value = parse_observation(json.dumps([self.row(**changes)]).encode())['trial-model']
                self.assertFalse(value.usable(date(2026, 11, 10)))
        duplicate = json.dumps([self.row(), self.row(remaining=0)]).encode()
        self.assertEqual(parse_observation(duplicate), {})
        with self.assertRaises(ValueError):
            parse_observation(b'[{"model":"trial-model","autoStop":false,"autoStop":true}]')

    def source(self, version=2):
        self.cli_path = self.path / 'config.json'
        self.context = {'access_key_id': 'synthetic-id', 'access_key_secret': 'synthetic-secret',
                        'console_region': 'cn-beijing', 'console_site': 'domestic',
                        'workspace_id': 'offline-space', 'access_token': 'unrelated-token'}
        atomic_json(self.cli_path, self.context)
        source = CLIQuotaSource(config_dir=self.path)
        proof = VerifiedIdentity('offline-space', 'a' * 64, 'cn-beijing',
                                 source._context(version=version)[2], version)
        return source, proof

    def run_query(self, source, proof, script, *, timeout=3):
        children = []
        popen = subprocess.Popen

        def start(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        command = '_renewal_command' if proof.context_version == 2 else '_command'
        try:
            with mock.patch.object(source, command, return_value=[sys.executable, '-I', '-c', script]), \
                    mock.patch('subtitle_tool.bailian_quota.subprocess.Popen', side_effect=start):
                return source.query(control=MTControl(threading.Event()), timeout=timeout, proof=proof)
        finally:
            for child in children:
                running = child.poll() is None
                if running:
                    child.kill()
                    child.wait()
                self.assertFalse(running, 'Quota query returned before reaping its subprocess')

    def test_query_reads_both_confirmed_contexts_and_reuses_renewed_token(self):
        row = self.row()
        for version in (1, 2):
            with self.subTest(version=version):
                source, proof = self.source(version)
                if version == 1:
                    script = 'print(' + repr(json.dumps([row])) + ')'
                else:
                    script = ('import json, sys\nrequest = json.load(sys.stdin)\n'
                              'assert request["credentials"]["accessKeyId"] == "synthetic-id"\n'
                              'print(' + repr(json.dumps({'token': 'renewed-token', 'observation': [row]})) + ')')
                result = self.run_query(source, proof, script)
                self.assertEqual(json.loads(result), [row])
                if version == 2:
                    self.assertEqual(json.loads(source._renewal_input(proof))['token'], 'renewed-token')

    def test_query_sanitizes_cli_failure_and_reaps_timed_out_process(self):
        source, proof = self.source(1)
        for script, timeout in (
                ('import sys; sys.stderr.write("private" * 65536); sys.exit(3)', 3),
                ('import time; time.sleep(30)', .3)):
            with self.subTest(timeout=timeout), self.assertRaises(QuotaSourceError) as error:
                self.run_query(source, proof, script, timeout=timeout)
            self.assertEqual(str(error.exception), 'Quota query failed')

    def test_renewal_binding_tracks_exact_credentials_and_scope(self):
        source, proof = self.source()
        self.assertTrue(source.bound_to(proof))
        for field, value in (('access_key_secret', 'changed-secret'), ('workspace_id', 'other-space'),
                             ('console_switch_agent', 123), ('console_site', 'international')):
            with self.subTest(field=field):
                atomic_json(self.cli_path, dict(self.context, **{field: value}))
                self.assertFalse(source.bound_to(proof))

    def test_unrelated_browser_token_does_not_rebind_ak_sk_identity(self):
        source, proof = self.source()
        atomic_json(self.cli_path, dict(self.context, access_token='renewed-unrelated-token'))
        self.assertTrue(source.bound_to(proof))
        self.assertNotIn(b'renewed-unrelated-token', source._renewal_input(proof))

    def test_invalid_selected_profile_is_sanitized_and_unbound(self):
        source, proof = self.source()
        atomic_json(self.cli_path, dict(self.context, active_config='missing-private-profile'))
        self.assertFalse(source.bound_to(proof))
        with self.assertRaises(QuotaSourceError) as raised:
            source._renewal_input(proof)
        self.assertNotIn('private', str(raised.exception))
