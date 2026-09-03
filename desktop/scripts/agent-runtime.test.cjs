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
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz runtime test ')));
  const events = [];
  const service = new AgentService({ requireRoot: () => root, current: () => ({ root }) }, (event) => events.push(event), new RuntimeSettingsService(path.join(root, 'runtime-settings.json')));
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
    response = {'observability_path': __import__('os').environ.get('SKILLZ_OBSERVABILITY_PATH', ''), 'cli_path': __import__('os').environ.get('CODEX_CLI_PATH', ''), 'id': request['id'], 'ok': True, 'text': request.get('text', ''), 'argv': sys.argv[1:]}
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
  assert.equal(started.observability_path, path.join(root, 'memory_observability.md'));
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


test('artifact provider setup failure is shown before starting Docker or reporting the agent running', async(t)=>{
  const {ArtifactAgentExecution}=loadTypeScript(()=>require('../src/main/services/artifactAgent.ts'));
  const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz artifact setup ')));
  const events=[];
  const execution=new ArtifactAgentExecution(async()=>{assert.fail('Docker must not start before provider setup succeeds');},()=>{});
  const service=new AgentService({requireRoot:()=>root,current:()=>({root})},event=>events.push(event),undefined,execution);
  service.agentRoot=()=>root;
  t.after(async()=>{await service.stop();assert.equal(fs.realpathSync(path.dirname(root)),fs.realpathSync(os.tmpdir()));assert.ok(path.basename(root).startsWith('skillz artifact setup '));await fs.promises.rm(root,{recursive:true,force:true,maxRetries:5});});
  fs.writeFileSync(path.join(root,'agent_tools.py'),'');fs.writeFileSync(path.join(root,'main.py'),'');
  fs.writeFileSync(path.join(root,'artifact_model_host.py'),`import json,sys
request=json.load(sys.stdin)
assert '--check' in sys.argv
assert request['provider']=='gemini'
print(json.dumps({'error':'Gemini setup requires google-genai in the local Python environment.'}))
`);
  await assert.rejects(service.start({provider:'gemini',model:'gemini-3-flash-preview'}),/Gemini setup requires google-genai/);
  assert.equal(events.at(-1).status,'error');assert.match(events.at(-1).message,/local Python/);
  assert.equal(events.some(event=>event.type==='status'&&event.status==='running'),false);
  assert.equal(service.process,null);
});

test('artifact runtime switches validate host setup and commit selection and credentials only after bridge acceptance', { timeout: 30_000 }, async (t) => {
  const { ArtifactAgentExecution } = loadTypeScript(() => require('../src/main/services/artifactAgent.ts'));
  const { spawn } = require('node:child_process');
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz runtime switch ')));
  const events = [];
  let launchContext;
  const execution = new ArtifactAgentExecution(async () => ({
    prepare: async () => ({ docker: 'fixture', args: [] }),
    spawn: (_docker, _args, command) => {
      assert.equal(command[3], 'main_v2.py');
      assert.deepEqual(command.slice(4, 8), ['--provider', 'codex-subscription', '--model', 'gpt-5.4']);
      return spawn(launchContext.python.executable, [...launchContext.python.args, path.join(root, 'bridge.py')], { cwd: root, env: launchContext.env, windowsHide: true });
    },
    stop: async () => {},
  }), () => {}, async (context) => {
    assert.equal(context.env.FIXTURE_PROVIDER, undefined, 'each switch starts with the original environment');
    launchContext = { ...context, env: { ...context.env, FIXTURE_PROVIDER: context.options.provider } };
    return launchContext;
  });
  const service = new AgentService({ requireRoot: () => root, current: () => ({ root }) }, event => events.push(event), undefined, execution);
  service.agentRoot = () => root;
  t.after(async () => {
    const child = service.process;
    const closed = child ? once(child, 'close') : Promise.resolve();
    await service.stop(); await closed;
    assert.equal(fs.realpathSync(path.dirname(root)), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('skillz runtime switch '));
    await fs.promises.rm(root, { recursive: true, force: true, maxRetries: 5 });
  });
  fs.writeFileSync(path.join(root, 'agent_tools.py'), '');
  fs.writeFileSync(path.join(root, 'main_v2.py'), '');
  fs.writeFileSync(path.join(root, 'artifact_model_host.py'), `import json, sys, os
request = json.load(sys.stdin)
if '--check' in sys.argv:
    result = {'error': 'Provider SDK missing'} if request['model'] == 'missing-sdk' else {'ready': True}
else:
    result = {'text': '|'.join([request['provider'], request['model'], os.environ['FIXTURE_PROVIDER']])}
print(json.dumps(result), flush=True)
`);
  fs.writeFileSync(path.join(root, 'bridge.py'), `import json, sys
provider, model = 'codex-subscription', 'gpt-5.4'
for line in sys.stdin:
    request = json.loads(line)
    result = {'id': request['id'], 'ok': True, 'state': {'planner': {}, 'transcript': []}}
    if request['type'] == 'reconfigure_runtime':
        if request['model'] == 'reject-bridge':
            result.update(ok=False, message='Runtime rejected by bridge')
        else:
            provider, model = request['provider'], request['model']
    elif request['type'] == 'submit':
        result = {'id': request['id'], 'type': 'artifact_model_request', 'provider': provider, 'model': model, 'system': 'fixture', 'messages': []}
    elif request['type'] == 'artifact_model_response':
        result.update(ok='error' not in request, text=request.get('text'), message=request.get('error', ''))
    print(json.dumps(result), flush=True)
`);
  assert.equal((await service.start({ provider: 'codex-subscription', model: 'gpt-5.4', backendScript: 'main_v2.py' })).ok, true);
  assert.equal((await service.submit('original prompt')).text, 'codex-subscription|gpt-5.4|codex-subscription');
  assert.equal((await service.reconfigureRuntime('gemini', 'reject-bridge')).ok, false);
  assert.equal((await service.submit('after failed apply')).text, 'codex-subscription|gpt-5.4|codex-subscription');
  await assert.rejects(service.reconfigureRuntime('gemini', 'missing-sdk'), /Provider SDK missing/);
  assert.equal((await service.submit('after failed preflight')).text, 'codex-subscription|gpt-5.4|codex-subscription');
  assert.equal((await service.reconfigureRuntime('gemini', 'gemini-3-flash-preview')).ok, true);
  assert.equal((await service.submit('new provider')).text, 'gemini|gemini-3-flash-preview|gemini');
  assert.equal(events.some(event => event.type === 'stderr' || event.status === 'error'), false);
});
