const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const load = require('./load-ts.cjs');
const { ArtifactLibraryService, ArtifactsService, ArtifactSandbox, AgentService, RuntimeSettingsService, terminate } = load(() => ({
  ...require('../src/main/services/artifactLibrary.ts'), ...require('../src/main/services/artifacts.ts'),
  ...require('../src/main/services/artifactSandbox.ts'), ...require('../src/main/services/agent.ts'),
  ...require('../src/main/services/runtimeSettings.ts'), ...require('../src/main/services/artifactProcess.ts'),
}));
const selection = { provider: 'gemini', model: 'fixture-model', backendScript: 'main.py' };
const response = status => ({ ok: true, state: { planner: { status }, transcript: [] } });

async function fixture(t, { grant = true, runtime = selection, chatbot = false, workspace = () => '' } = {}) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'skillz-run-service-')));
  const library = new ArtifactLibraryService(path.join(root, 'library.json'), path.resolve(__dirname, '../artifact-template'), path.join(root, 'contexts'));
  const events = [];
  const service = new ArtifactsService(library, new RuntimeSettingsService(path.join(root, 'runtime.json')), event => events.push(event), workspace);
  t.mock.method(service.capabilities, 'modelReadiness', async () => ({ ready: true }));
  t.after(async () => { await service.dispose(); await fs.rm(root, { recursive: true, force: true }); });
  await library.configure(path.join(root, 'library'));
  const record = await library.installPrebuilt('beta-observatory', { directories: [], allowWorkspaceRead: false, allowAgentRuntime: grant }, runtime);
  if (chatbot) await library.saveApp(record.id, { version: 1, chatbot: { enabled: true }, database: { enabled: false } });
  const launches = [], children = [];
  t.mock.method(ArtifactSandbox.prototype, 'prepare', async () => ({ docker: 'fixture', args: ['fixture-image'] }));
  t.mock.method(ArtifactSandbox.prototype, 'spawn', function (_docker, _args, command, options) {
    const env = Object.fromEntries(options.filter((_, i) => options[i - 1] === '--env').map(value => { const index = value.indexOf('='); return [value.slice(0, index), value.slice(index + 1)]; }));
    launches.push({ env, command, broker: this.modelBroker });
    const child = spawn(process.execPath, ['-e', `console.log('SKILLZ_ARTIFACT_READY '+JSON.stringify({url:'http://127.0.0.1:43123'}));setInterval(()=>{},1000)`], { stdio: 'pipe' });
    children.push(child); return child;
  });
  t.mock.method(ArtifactSandbox.prototype, 'port', async () => 'http://127.0.0.1:43123');
  t.mock.method(ArtifactSandbox.prototype, 'stop', async () => { await Promise.all(children.map(terminate)); });
  return { root, library, service, record, launches, events };
}
async function bridgeRequest(connection, route, body) {
  const url = connection.SKILLZ_RUNTIME_URL.replace('host.docker.internal', '127.0.0.1');
  const result = await fetch(url + '/v1/' + route, { method: body === undefined ? 'GET' : 'POST', headers: { 'x-skillz-runtime-token': connection.SKILLZ_RUNTIME_TOKEN, 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const value = await result.json(); assert.equal(result.ok, true, JSON.stringify(value)); return value;
}
async function settled(connection) {
  for (let attempt = 0; attempt < 100; attempt++) { const status = await bridgeRequest(connection, 'runtime'); if (!status.busy) return status; await new Promise(resolve => setTimeout(resolve, 10)); }
  throw new Error('Recording did not settle.');
}
test('preview connects recording and chatbot capabilities independently, preserves approval, and restores saved runs', async t => {
  const { service, record, launches } = await fixture(t, { chatbot: true });
  const agents = [], approvals = [];
  t.mock.method(AgentService.prototype, 'start', async function (options) { agents.push(this); assert.equal(options.backendScript, 'main_v2.py'); return response('idle'); });
  t.mock.method(AgentService.prototype, 'submit', async function () { this.emit({ type: 'progress', payload: { action_type: 'model_call_start', step: 1 } }); return { ...response('awaiting_plan_approval'), state: { planner: { status: 'awaiting_plan_approval', pending_plan: { summary: 'Read evidence', goals: [] }, suggested_next_actions: [{ type: 'approve_plan' }] }, transcript: [] } }; });
  t.mock.method(AgentService.prototype, 'plannerAction', async function (action, extras) { approvals.push({ action, extras }); return response('completed'); });
  t.mock.method(AgentService.prototype, 'stop', async () => {});
  t.mock.method(service.capabilities, 'modelRequest', async () => ({ text: 'Chatbot broker stays available' }));
  await service.start(record.id);
  const { env, broker, command } = launches[0];
  assert.match(command[2], /prepare-dependencies/);
  assert.ok(broker && env.SKILLZ_RUNTIME_TOKEN);
  const { id } = await bridgeRequest(env, 'runs', { text: 'Inspect evidence' });
  const status = await settled(env);
  assert.equal(status.activeRunId, id); assert.equal(status.selection.backendScript, 'main_v2.py');
  assert.equal((await bridgeRequest(env, 'runs/' + id)).status, 'awaiting-input');
  assert.equal(approvals.length, 0);
  const editing = await service.agent(record.id); assert.notEqual(editing, agents[0]);
  const chatbot = await fetch(broker.url.replace('host.docker.internal', '127.0.0.1'), { method: 'POST', headers: { 'content-type': 'application/json', 'X-Skillz-Model-Token': broker.token }, body: JSON.stringify({ messages: [] }) });
  assert.equal(chatbot.status, 200);
  await bridgeRequest(env, `runs/${id}/continue`, { action: 'approve_plan' }); await settled(env);
  assert.equal((await bridgeRequest(env, 'runs/' + id)).status, 'completed');
  assert.deepEqual(approvals, [{ action: 'approve_plan', extras: { expected_plan: { summary: 'Read evidence', goals: [] } } }]);
  await service.stop(record.id); await assert.rejects(bridgeRequest(env, 'runtime'));
  await service.start(record.id);
  assert.notEqual(launches[1].env.SKILLZ_RUNTIME_TOKEN, env.SKILLZ_RUNTIME_TOKEN);
  const restored = await bridgeRequest(launches[1].env, 'runs/' + id);
  assert.equal(restored.status, 'completed'); assert.equal(agents.length, 1, 'Replaying a saved run must not start an agent.');
});
test('preview without the explicit runtime grant never exposes a recording capability', async t => {
  const { service, record, launches } = await fixture(t, { grant: false });
  await service.start(record.id);
  assert.equal(launches[0].env.SKILLZ_RUNTIME_URL, undefined); assert.equal(launches[0].env.SKILLZ_RUNTIME_TOKEN, undefined);
});
test('an explicit runtime grant requires a saved provider/model and remains outside editable metadata', async t => {
  const { service, record, library } = await fixture(t, { runtime: undefined });
  // Remove the fixture default selection explicitly to represent an older install.
  const localFile = path.join(path.dirname(library.settingsFile), 'artifact-records.json');
  const local = JSON.parse(await fs.readFile(localFile, 'utf8')); delete local[record.root].runtime;
  await fs.writeFile(localFile, JSON.stringify(local));
  const metadata = JSON.parse(await fs.readFile(path.join(record.root, 'artifact.json'), 'utf8'));
  assert.equal(metadata.access, undefined);
  await assert.rejects(service.start(record.id), /Choose a provider and model/);
});

test('runtime setup targets the owning artifact and saving a selection restarts only its preview', async t => {
  const { service, record, library, launches, events } = await fixture(t);
  const editing = await service.agent(record.id);
  let editingStopped = false;
  t.mock.method(editing, 'stop', async () => { editingStopped = true; });
  await service.start(record.id);
  await bridgeRequest(launches[0].env, 'configure', {});
  assert.ok(events.some(event => event.type === 'runtime-setup' && event.id === record.id));
  const selection = { provider: 'codex-subscription', model: 'fixture', backendScript: 'main_v2.py' };
  await service.saveRuntime(record.id, selection);
  assert.deepEqual((await library.find(record.id)).runtime, selection);
  assert.equal(editingStopped, false);
  assert.ok(events.some(event => event.type === 'runtime-selection' && event.record.id === record.id));
  await assert.rejects(bridgeRequest(launches[0].env, 'runtime'));
  await service.start(record.id);
  assert.deepEqual((await bridgeRequest(launches[1].env, 'runtime')).selection, selection);
});

test('shared run scopes retain File Access permissions and separate the worker from the editing repository', async t => {
  const { root, service, record, library, launches } = await fixture(t);
  const shared = path.join(root, 'shared'), sibling = path.join(root, 'sibling');
  await fs.mkdir(shared); await fs.mkdir(sibling);
  await library.saveAccess(record.id, { directories: [{ id: 'project', label: 'Granted project', path: shared, access: 'read' }, { id: 'sibling', label: 'Sibling', path: sibling, access: 'write' }], allowWorkspaceRead: false, allowAgentRuntime: true });
  const { scopeRunRepository, runRepositories, ArtifactAgentExecution } = load(() => ({ ...require('../src/main/services/artifactRunRepositories.ts'), ...require('../src/main/services/artifactAgent.ts') }));
  const base = await service.sandbox(record.id);
  assert.deepEqual(runRepositories(base).map(item => item.id), ['repo', 'project', 'sibling']);
  assert.throws(() => scopeRunRepository(base, 'ungranted'), /not granted/);
  const scoped = scopeRunRepository(base, 'project');
  assert.equal(scoped.root, shared);
  assert.equal(scoped.sandbox.root, record.root, 'Container ownership remains the artifact for revocation/cleanup.');
  assert.deepEqual(scoped.sandbox.reads.map(item => item.id), ['project']);
  assert.equal(scoped.sandbox.repository.access, 'read');
  const agents = [];
  t.mock.method(AgentService.prototype, 'start', async function () {
    agents.push(this);
    assert.equal(this.workspace.requireRoot(), shared);
    assert.ok(this.execution instanceof ArtifactAgentExecution);
    const worker = await this.execution.createSandbox();
    assert.equal(worker.repository.path, shared); assert.equal(worker.repository.access, 'read');
    assert.deepEqual(worker.reads.map(item => item.id), ['project']);
    return response('idle');
  });
  t.mock.method(AgentService.prototype, 'submit', async text => { assert.match(text, /read-only access/); return response('completed'); });
  t.mock.method(AgentService.prototype, 'stop', async () => {});
  await service.start(record.id);
  const connection = launches[0].env;
  assert.deepEqual((await bridgeRequest(connection, 'runtime')).repositories.map(item => item.id), ['repo', 'project', 'sibling']);
  const { id } = await bridgeRequest(connection, 'runs', { text: 'Inspect this project', repositoryId: 'project' });
  await settled(connection);
  assert.deepEqual((await bridgeRequest(connection, 'runs/' + id)).repository, { id: 'project', label: 'Granted project', access: 'read' });
  assert.equal(agents.length, 1);
  const editing = await service.agent(record.id); assert.equal(editing.workspace.requireRoot(), record.root);
});


test('the Workbench repository choice stays pinned to the preview snapshot when the open workspace changes', async t => {
  let activeRoot = '';
  const { root, service, record, library, launches } = await fixture(t, { workspace: () => activeRoot });
  const first = path.join(root, 'first-project'), second = path.join(root, 'second-project');
  await fs.mkdir(first); await fs.mkdir(second); activeRoot = first;
  await library.saveAccess(record.id, { directories: [], allowWorkspaceRead: true, allowAgentRuntime: true });
  t.mock.method(AgentService.prototype, 'start', async function () {
    assert.equal(this.workspace.requireRoot(), first);
    const sandbox = await this.execution.createSandbox();
    assert.equal(sandbox.repository.path, first); assert.equal(sandbox.repository.access, 'read');
    return response('idle');
  });
  t.mock.method(AgentService.prototype, 'submit', async () => response('completed'));
  t.mock.method(AgentService.prototype, 'stop', async () => {});
  await service.start(record.id);
  activeRoot = second;
  const connection = launches[0].env;
  const status = await bridgeRequest(connection, 'runtime');
  assert.deepEqual(status.repositories.find(item => item.id === 'workspace'), { id: 'workspace', label: 'Workbench repository: first-project', access: 'read' });
  const { id } = await bridgeRequest(connection, 'runs', { text: 'Inspect the displayed Workbench repository', repositoryId: 'workspace' });
  await settled(connection);
  assert.equal((await bridgeRequest(connection, 'runs/' + id)).repository.label, 'Workbench repository: first-project');
});
