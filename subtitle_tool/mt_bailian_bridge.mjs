// Private, version-pinned adapter for read-only Bailian quota queries.
// Credentials and generated tokens travel only through the parent pipes.
import {pathToFileURL} from 'node:url';
import {dirname, join} from 'node:path';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';

const inputLimit = 64 * 1024;
const outputLimit = 4 * 1024 * 1024;
const emit = process.stdout.write.bind(process.stdout);
const report = process.stderr.write.bind(process.stderr);
const quotaApis = new Set([
    'zeldaHttp.dashscopeModel./zelda/api/v1/modelCenter/listFoundationModels',
    'zeldaEasy.bailian-commerce.freeTrial.queryFreeTierQuota',
    'zeldaEasy.bailian-commerce.freeTrial.queryFreeTierOnlyStatus',
]);

function secret(value) {
    return typeof value === 'string' && value.length > 0 && value.length <= 16384 && !/[\s\u0000-\u001f]/u.test(value);
}

async function main() {
    const chunks = [];
    let length = 0;
    for await (const chunk of process.stdin) {
        length += chunk.length;
        if (length > inputLimit) throw new Error('input limit');
        chunks.push(chunk);
    }
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const credentials = input.credentials;
    if (!credentials || !secret(credentials.accessKeyId) || !secret(credentials.accessKeySecret) ||
        (credentials.securityToken !== undefined && !secret(credentials.securityToken)) ||
        (input.token !== null && !secret(input.token)) ||
        (input.switchAgent !== null && (!Number.isSafeInteger(input.switchAgent) || input.switchAgent <= 0))) {
        throw new Error('input shape');
    }

    // Capture the official command's JSON, including any accidental stdout;
    // never forward module diagnostics or exceptions to the parent verbatim.
    const output = [];
    let outputBytes = 0;
    process.stdout.write = (chunk, encoding, callback) => {
        const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, typeof encoding === 'string' ? encoding : 'utf8');
        outputBytes += data.length;
        if (outputBytes > outputLimit) throw new Error('output limit');
        output.push(data);
        (typeof encoding === 'function' ? encoding : callback)?.();
        return true;
    };
    process.stderr.write = (chunk, encoding, callback) => {
        (typeof encoding === 'function' ? encoding : callback)?.();
        return true;
    };
    const packageDir = process.argv[2];
    function checkedModule(file, name) {
        const info = JSON.parse(readFileSync(join(dirname(dirname(file)), 'package.json'), 'utf8'));
        if (info.name !== name || info.version !== '2.7.2') throw new Error('SDK version');
        return file;
    }
    const coreFile = checkedModule(join(packageDir, 'node_modules/bailian-cli-core/dist/index.mjs'), 'bailian-cli-core');
    const commandFile = checkedModule(join(packageDir, 'node_modules/bailian-cli-commands/dist/index.mjs'), 'bailian-cli-commands');
    const fromCommands = createRequire(commandFile);
    checkedModule(fromCommands.resolve('bailian-cli-core'), 'bailian-cli-core');
    const runtimeFile = checkedModule(fromCommands.resolve('bailian-cli-runtime'), 'bailian-cli-runtime');
    checkedModule(createRequire(runtimeFile).resolve('bailian-cli-core'), 'bailian-cli-core');
    const core = await import(pathToFileURL(coreFile).href);
    const commands = await import(pathToFileURL(commandFile).href);
    const identity = {binName: 'bl', version: '2.7.2', npmPackage: 'bailian-cli', clientName: 'bailian-cli'};
    const settings = {output: 'json', outputExplicit: true, outputSource: 'flag', timeout: 15,
        consoleRegion: 'cn-beijing', consoleSite: 'domestic', consoleSwitchAgent: input.switchAgent ?? undefined,
        verbose: false, quiet: true, dryRun: false, telemetry: false, watermark: false};
    let token = input.token;
    let generatedToken = null;
    let renewal;
    function renew() {
        renewal ??= (async () => {
            const result = await core.generateCLIAccessToken({identity, settings,
                baseUrl: 'https://dashscope.aliyuncs.com', ...credentials});
            if (!secret(result.cliAccessToken)) throw new Error('token shape');
            token = generatedToken = result.cliAccessToken;
        })();
        return renewal;
    }
    const request = (api, data) => core.callConsoleGateway({region: 'cn-beijing', site: 'domestic',
        switchAgent: input.switchAgent ?? undefined, token}, 15, {api, data}, {verbose: false});
    const client = {async console(api, data) {
        if (!quotaApis.has(api)) throw new Error('unsupported quota API');
        if (!token) await renew();
        const used = token;
        try {
            return await request(api, data);
        } catch (error) {
            if (error?.exitCode !== 3 || error?.message !== 'Console session is not logged in or has expired.' ||
                used === generatedToken) throw error;
            await renew();
            return request(api, data);
        }
    }};
    await commands.usageFree.run({identity, settings, flags: {all: true, sort: 'expires'}, client});
    const observation = JSON.parse(Buffer.concat(output).toString('utf8'));
    if (!Array.isArray(observation) || !secret(token)) throw new Error('response shape');
    const result = JSON.stringify({token, observation});
    if (Buffer.byteLength(result) > outputLimit) throw new Error('output limit');
    emit(result);
}

main().catch(() => {
    report('Quota query failed.\n');
    process.exitCode = 1;
});
