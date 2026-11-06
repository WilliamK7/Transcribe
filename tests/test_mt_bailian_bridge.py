"""Exercise the real Node bridge with fictional SDK exports and no network."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
BRIDGE = ROOT / 'subtitle_tool' / 'mt_bailian_bridge.mjs'


@unittest.skipUnless(NODE, 'Node.js is required for the optional Bailian CLI bridge')
class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-test-', dir=ROOT / '.build')
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name)

    def invoke(self, *, token=None, scenario='ok', stdin=None, nested_version=None, nested_runtime=False):
        core = '''
import {writeFileSync} from 'node:fs';
let generated = 0, calls = 0;
const scenario = SCENARIO;
const audit = () => writeFileSync(AUDIT_PATH, JSON.stringify({generated, calls}));
audit();
export async function generateCLIAccessToken(options) {
    generated++;
    audit();
    if (options.accessKeyId !== 'synthetic-ak' || options.accessKeySecret !== 'synthetic-sk' ||
        options.settings.consoleRegion !== 'cn-beijing' || options.settings.consoleSite !== 'domestic' ||
        options.settings.verbose || options.settings.telemetry || generated > 1) throw Error('private-generation');
    if (scenario === 'generation-error') throw Error('private-generation');
    return {cliAccessToken: 'generated-token'};
}
export async function callConsoleGateway(target, timeout, request, settings) {
    calls++;
    audit();
    if (target.region !== 'cn-beijing' || target.site !== 'domestic' || target.switchAgent !== 123 ||
        timeout !== 15 || settings.verbose) throw Error('private-target');
    if (scenario === 'network') throw Error('private-network-error');
    if (target.token === 'expired-token' || scenario === 'always-expired') {
        const error = Error('Console session is not logged in or has expired.');
        error.exitCode = 3;
        throw error;
    }
    return {generated, calls, api: request.api};
}
'''.replace('SCENARIO', json.dumps(scenario)).replace('AUDIT_PATH', json.dumps(str(self.package / 'audit.json')))
        commands = '''
export const usageFree = {async run(context) {
    if (context.flags.all !== true || context.flags.sort !== 'expires' || context.settings.output !== 'json' ||
        context.settings.dryRun || !context.settings.quiet) throw Error('private-command');
    const results = [];
    if (SCENARIO === 'write-attempt') await context.client.console('unapproved-write-api', {});
    results.push(await context.client.console('zeldaEasy.bailian-commerce.freeTrial.queryFreeTierQuota', {}));
    if (SCENARIO === 'multiple') results.push(await context.client.console('zeldaEasy.bailian-commerce.freeTrial.queryFreeTierOnlyStatus', {}));
    if (SCENARIO === 'overflow') process.stdout.write('x'.repeat(4 * 1024 * 1024 + 1));
    else process.stdout.write(JSON.stringify(results));
}};
'''.replace('SCENARIO', json.dumps(scenario))
        for package, script in (('bailian-cli-core', core), ('bailian-cli-commands', commands),
                                ('bailian-cli-runtime', 'export const fixture = true;')):
            modules = self.package / 'node_modules'
            if nested_runtime and package == 'bailian-cli-runtime':
                modules = modules / 'bailian-cli-commands' / 'node_modules'
            folder = modules / package / 'dist'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'index.mjs').write_text(script, encoding='utf-8')
            (folder.parent / 'package.json').write_text(json.dumps(
                {'name': package, 'version': '2.7.2', 'main': 'dist/index.mjs'}))
        if nested_version:
            folder = self.package / 'node_modules/bailian-cli-commands/node_modules/bailian-cli-core'
            (folder / 'dist').mkdir(parents=True, exist_ok=True)
            (folder / 'package.json').write_text(json.dumps(
                {'name': 'bailian-cli-core', 'version': nested_version, 'main': 'dist/index.mjs'}))
            (folder / 'dist/index.mjs').write_text(core, encoding='utf-8')
        if stdin is None:
            stdin = json.dumps({'credentials': {'accessKeyId': 'synthetic-ak', 'accessKeySecret': 'synthetic-sk'},
                                'token': token, 'switchAgent': 123}).encode()
        return subprocess.run([NODE, str(BRIDGE), str(self.package)], input=stdin,
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)

    def test_initial_generation_and_cached_token_reuse(self):
        for token, generated in ((None, 1), ('saved-token', 0)):
            with self.subTest(cached=token is not None):
                result = self.invoke(token=token)
                self.assertEqual((result.returncode, result.stderr), (0, b''))
                body = json.loads(result.stdout)
                self.assertEqual(body['token'], token or 'generated-token')
                self.assertEqual(body['observation'][0]['generated'], generated)
                self.assertEqual(body['observation'][0]['calls'], 1)

    def test_expired_token_refreshes_once_and_continues_read_only_command(self):
        result = self.invoke(token='expired-token', scenario='multiple')
        self.assertEqual(result.returncode, 0)
        body = json.loads(result.stdout)
        self.assertEqual(body['token'], 'generated-token')
        self.assertEqual([r['generated'] for r in body['observation']], [1, 1])
        self.assertEqual([r['calls'] for r in body['observation']], [2, 3])

    def test_network_generation_and_repeated_auth_errors_are_sanitized(self):
        for scenario in ('network', 'generation-error', 'always-expired', 'overflow'):
            with self.subTest(scenario=scenario):
                result = self.invoke(token=None if scenario == 'generation-error' else 'saved-token', scenario=scenario)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b'')
                self.assertEqual(result.stderr, b'Quota query failed.\n')
                expected = {'network': (0, 1), 'generation-error': (1, 0),
                            'always-expired': (1, 2), 'overflow': (0, 1)}[scenario]
                audit = json.loads((self.package / 'audit.json').read_text())
                self.assertEqual((audit['generated'], audit['calls']), expected)

    def test_invalid_and_oversized_private_input_is_never_echoed(self):
        for value in (b'private-not-json', b'x' * (64 * 1024 + 1), b'{"credentials":{}}'):
            with self.subTest(size=len(value)):
                result = self.invoke(stdin=value)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b'')
                self.assertEqual(result.stderr, b'Quota query failed.\n')

    def test_unverified_nested_sdk_version_is_rejected_before_use(self):
        result = self.invoke(nested_version='2.8.0')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'Quota query failed.\n')

    def test_matching_runtime_at_nodes_actual_nested_location_is_supported(self):
        result = self.invoke(nested_runtime=True)
        self.assertEqual((result.returncode, result.stderr), (0, b''))
        self.assertEqual(json.loads(result.stdout)['observation'][0]['calls'], 1)

    def test_non_read_only_api_is_rejected_before_authentication_or_dispatch(self):
        result = self.invoke(scenario='write-attempt')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'Quota query failed.\n')
        audit = json.loads((self.package / 'audit.json').read_text())
        self.assertEqual(audit, {'generated': 0, 'calls': 0})


if __name__ == '__main__':
    unittest.main()
