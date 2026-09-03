const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { once } = require('node:events');
const { test } = require('node:test');
const loadTypeScript = require('./load-ts.cjs');
const { AgentService, RuntimeSettingsService } = loadTypeScript(() => ({
  ...require('../src/main/services/agent.ts'), ...require('../src/main/services/runtimeSettings.ts'),
}));

// Real child processes verify launcher arguments, spaced paths, and UTF-8 pipes.
// Fixtures do not contact providers or read or modify real authentication state.
test('Python runtime discovery, status/login, and the agent bridge work end to end', { timeout: 30_000 }, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'skillz runtime test '));
  const events = [];
  const service = new AgentService({ requireRoot: () => root }, (event) => events.push(event), new RuntimeSettingsService(path.join(root, 'runtime-settings.json')));
  service.agentRoot = () => root;
  t.after(async () => {
    const child = service.process;
    const closed = child ? once(child, 'close') : Promise.resolve();
    await service.stop();
    await closed;
    assert.equal(path.dirname(root), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('skillz runtime test '));
    fs.rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  });
  fs.writeFileSync(path.join(root, 'agent_tools.py'), '');
  fs.writeFileSync(path.join(root, 'runtime_catalog.py'), `
def runtime_options_payload(current_provider, current_model):
    return {'current_provider': current_provider, 'current_model': current_model}
`);
  fs.writeFileSync(path.join(root, 'codex_subscription.py'), `
import json, sys
print(json.dumps({'seen_cli_path': __import__('os').environ.get('CODEX_CLI_PATH', ''), 'command': sys.argv[1], 'status_text': '\\u2713 caf\\u00e9'}, ensure_ascii=False))
`);
  fs.writeFileSync(path.join(root, 'main.py'), `
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    response = {'cli_path': __import__('os').environ.get('CODEX_CLI_PATH', ''), 'id': request['id'], 'ok': True, 'text': request.get('text', ''), 'argv': sys.argv[1:]}
    if request['type'] == 'runtime_options':
        response['runtime_options'] = {'current_provider': 'bridge'}
    print(json.dumps(response, ensure_ascii=False), flush=True)
`);
  assert.deepEqual(await service.runtimeOptions('provider with spaces', 'modèle'), {
    current_provider: 'provider with spaces', current_model: 'modèle',
  });
  assert.equal((await service.codexSubscriptionStatus()).command, 'status');
  assert.equal((await service.codexSubscriptionLogin()).status_text, '✓ café');
  const started = await service.start({ provider: 'fixture', model: 'model with spaces' });
  assert.equal(started.ok, true);
  assert.deepEqual(started.argv, [
    '--provider', 'fixture', '--model', 'model with spaces', '--root', root,
    '--tools', path.join(root, 'agent_tools.py'), '--extension-bridge',
  ]);
  assert.equal((await service.submit('héllo 世界 🚀')).text, 'héllo 世界 🚀');
  assert.deepEqual(await service.runtimeOptions(), { current_provider: 'bridge' });
  const childProcess = require('node:child_process');
  const originalExecFile = childProcess.execFile;
  t.mock.method(childProcess, 'execFile', (executable, args, options, callback) => {
    if (args[0] === '--version') return callback(null, 'codex-cli fixture', '');
    return originalExecFile(executable, args, options, callback);
  });
  const cliPath = path.join(root, 'chosen codex.exe');
  fs.writeFileSync(cliPath, 'fixture');
  const configured = await service.setCodexCliPath(cliPath);
  assert.equal(configured.configured_cli_path, cliPath);
  assert.equal(configured.seen_cli_path, cliPath);
  assert.equal(configured.restart_required, true);
  assert.equal(configured.cli_path_source, 'settings');
  assert.equal((await service.submit('existing process')).cli_path, started.cli_path);
  const previousChild = service.process;
  const closed = once(previousChild, 'close');
  await service.stop();
  await closed;
  const restarted = await service.start({ provider: 'fixture', model: 'fixture' });
  assert.equal(restarted.cli_path, cliPath);
  assert.equal((await service.codexSubscriptionStatus()).restart_required, false);
  assert.equal((await service.codexSubscriptionLogin()).configured_cli_path, cliPath);
  assert.ok(events.some((event) => event.type === 'status' && event.status === 'running'));
  assert.ok(!events.some((event) => event.type === 'stderr' || event.status === 'error'));
});
