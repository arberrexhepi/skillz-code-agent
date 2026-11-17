const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const load = require('./load-ts.cjs');
const { ArtifactRunSession, ArtifactRuntimeBridge } = load(() => ({ ...require('../src/main/services/artifactRunSession.ts'), ...require('../src/main/services/artifactRuntimeBridge.ts') }));
const selection = { provider: 'test', model: 'fixture', backendScript: 'main_v2.py' };
const state = status => ({ planner: { status }, transcript: [] });
const response = status => ({ ok: true, message: status, state: state(status) });
async function fixture(t, create) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'observatory-test-'));
  const session = new ArtifactRunSession(root, selection, create);
  t.after(async () => { await session.close(); await fs.rm(root, { recursive: true, force: true }); });
  await session.load(); return { session, root };
}
async function settle(session) { for (let i = 0; i < 100 && session.summary().busy; i++) await new Promise(resolve => setTimeout(resolve, 5)); assert.equal(session.summary().busy, false); }
test('planner approval remains a pause; continuation is explicit, recorded, and persisted for replay', async t => {
  const calls = [];
  const { session, root } = await fixture(t, async emit => ({
    start: async options => { calls.push(options); return response('idle'); },
    submit: async () => { emit({ type: 'progress', payload: { type: 'progress', action_type: 'model_call_start', step: 1 } }); return { ...response('awaiting_plan_approval'), state: { planner: { status: 'awaiting_plan_approval', pending_plan: { summary: 'Inspect source', goals: [] }, suggested_next_actions: [{ type: 'approve_plan' }] }, transcript: [] } }; },
    plannerAction: async (action, extras) => { calls.push({ action, extras }); emit({ type: 'progress', payload: { type: 'goal_finish', ok: true, summary: 'Goal done' } }); return response('completed'); }, stop: async () => {},
  }));
  const id = await session.start('Inspect the source'); await settle(session);
  assert.equal(session.get(id).status, 'awaiting-input'); assert.equal(calls.length, 1);
  await assert.rejects(session.start('another'), /Finish or stop/);
  await assert.rejects(session.continue(id, { action: 'start_continuous' }), /not currently offered/);
  await session.continue(id, { action: 'approve_plan' }); await settle(session);
  assert.equal(calls[1].action, 'approve_plan'); assert.deepEqual(calls[1].extras.expected_plan, { summary: 'Inspect source', goals: [] });
  const run = session.get(id); assert.equal(run.status, 'completed'); assert.ok(run.endedAt);
  assert.deepEqual(run.events.map(event => event.seq), run.events.map((_, i) => i + 1));
  assert.ok(run.events.some(event => event.data.action_type === 'model_call_start'));
  assert.ok(session.get(id, 2).events.every(event => event.seq > 2));
  await session.close();
  const restored = new ArtifactRunSession(root, selection, async () => { throw Error('Replay must not create an agent'); }); await restored.load();
  assert.equal(restored.get(id).status, 'completed'); assert.deepEqual(restored.get(id).events, run.events); await restored.close();
});
test('stop during initialization cannot submit a task or record a false completion', async t => {
  let release; let submitted = false;
  const gate = new Promise(resolve => { release = resolve; });
  const { session } = await fixture(t, async () => ({ start: async () => { await gate; return response('idle'); }, submit: async () => { submitted = true; return response('completed'); }, plannerAction: async () => response('completed'), stop: async () => { release(); } }));
  const id = await session.start('Cancel me');
  await new Promise(resolve => setTimeout(resolve, 5));
  await session.stop();
  assert.equal(submitted, false); assert.equal(session.get(id).status, 'interrupted');
  assert.equal(session.get(id).events.at(-1).type, 'interrupted');
});
test('a failed request is retained as failed and cannot be mistaken for completion', async t => {
  const { session } = await fixture(t, async () => ({ start: async () => response('idle'), submit: async () => { throw Error('Provider unavailable'); }, plannerAction: async () => response('idle'), stop: async () => {} }));
  const id = await session.start('Run me'); await settle(session);
  assert.equal(session.get(id).status, 'failed'); assert.match(session.get(id).message, /Provider unavailable/);
});
test('a damaged archive does not hide valid recordings or start an agent during replay', async t => {
  const { root } = await fixture(t, async () => { throw Error('Replay must not create an agent'); });
  const id = '00000000-0000-4000-8000-000000000001';
  const run = { id, prompt: 'Saved task', startedAt: '2026-10-05T12:00:00.000Z', status: 'completed', selection, events: [] };
  await fs.writeFile(path.join(root, id + '.json'), JSON.stringify(run));
  await fs.writeFile(path.join(root, '00000000-0000-4000-8000-000000000002.json'), JSON.stringify({ id: '00000000-0000-4000-8000-000000000002', events: [] }));
  await fs.writeFile(path.join(root, '00000000-0000-4000-8000-000000000003.json'), '{broken');
  const restored = new ArtifactRunSession(root, selection, async () => { throw Error('Replay must not create an agent'); });
  t.after(() => restored.close()); await restored.load();
  assert.deepEqual(restored.summary().runs.map(run => run.id), [id]);
  assert.equal(restored.get(id).status, 'completed');
});
test('host bridge requires its artifact capability, rejects browser origins and arbitrary operations', async t => {
  const { session } = await fixture(t, async () => ({ start: async () => response('idle'), submit: async () => response('completed'), plannerAction: async () => response('completed'), stop: async () => {} }));
  const bridge = new ArtifactRuntimeBridge(session); const connection = await bridge.start(); t.after(() => bridge.close());
  const origin = connection.url.replace('host.docker.internal', '127.0.0.1');
  const headers = { 'x-skillz-runtime-token': connection.token, 'content-type': 'application/json' };
  assert.equal((await fetch(origin + '/v1/runtime')).status, 401);
  assert.equal((await fetch(origin + '/v1/runtime', { headers: { ...headers, origin: 'http://other-artifact' } })).status, 403);
  assert.equal((await fetch(origin + '/v1/shell', { method: 'POST', headers, body: '{}' })).status, 404);
  assert.equal((await fetch(origin + '/v1/runs', { method: 'POST', headers, body: JSON.stringify({ text: '' }) })).status, 400);
  const accepted = await fetch(origin + '/v1/runs', { method: 'POST', headers, body: JSON.stringify({ text: 'Inspect locally' }) });
  assert.equal(accepted.status, 202); const { id } = await accepted.json(); await settle(session);
  const run = await (await fetch(origin + '/v1/runs/' + id, { headers })).json(); assert.equal(run.status, 'completed');
});

test('late events from a previous agent cannot contaminate the next run', async t => {
  let emitOld; let count = 0;
  const { session } = await fixture(t, async emit => {
    count++; if (count === 1) emitOld = emit;
    return { start: async () => response('idle'), submit: async () => response('completed'), plannerAction: async () => response('completed'), stop: async () => { emit({ type: 'status', status: 'stopped' }); } };
  });
  await session.start('First run'); await settle(session);
  const second = await session.start('Second run'); await settle(session);
  emitOld({ type: 'progress', payload: { type: 'progress', action_type: 'patch_file', summary: 'Stale event' } });
  assert.equal(session.get(second).status, 'completed');
  assert.ok(session.get(second).events.every(event => event.data.summary !== 'Stale event' && event.data.status !== 'stopped'));
});

test('readiness gates new runs, refreshes before execution, and leaves setup and replay usable', async t => {
  let ready = false, configured = 0, agents = 0;
  const { session } = await fixture(t, async () => {
    agents++;
    return { start: async () => response('idle'), submit: async () => response('completed'), plannerAction: async () => response('completed'), stop: async () => {} };
  });
  const bridge = new ArtifactRuntimeBridge(session, { readiness: async () => ({ ready, detail: ready ? undefined : 'Sign in to Codex' }), configure: () => { configured++; } });
  const connection = await bridge.start(); t.after(() => bridge.close());
  const url = connection.url.replace('host.docker.internal', '127.0.0.1');
  async function request(route, body, token = connection.token) {
    return fetch(url + '/v1/' + route, { method: body === undefined ? 'GET' : 'POST', headers: { 'x-skillz-runtime-token': token, 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  }
  const status = await (await request('runtime')).json();
  assert.equal(status.available, true); assert.equal(status.ready, false); assert.equal(status.setupAvailable, true);
  assert.match(status.setupMessage, /Sign in/);
  assert.equal((await request('runs', { text: 'Task' })).status, 409); assert.equal(agents, 0);
  assert.equal((await request('configure', {}, 'wrong')).status, 401); assert.equal(configured, 0);
  assert.equal((await request('configure', {})).status, 200); assert.equal(configured, 1);
  ready = true;
  const started = await request('runs', { text: 'Task' }); assert.equal(started.status, 202);
  const { id } = await started.json(); await settle(session); assert.equal(agents, 1);
  ready = false;
  assert.equal((await request('runs', { text: 'Second task' })).status, 409);
  assert.equal((await (await request('runs/' + id)).json()).status, 'completed'); assert.equal(agents, 1);
});

test('repository choices are host-owned, pinned per run, and retained for replay', async t => {
  const scopes = [];
  const { session, root } = await fixture(t, async (_emit, repository) => {
    scopes.push(repository);
    return { start: async () => response('idle'), submit: async () => ({ ...response('awaiting_plan_approval'), state: { planner: { status: 'awaiting_plan_approval', pending_plan: { goals: [] }, suggested_next_actions: [{ type: 'approve_plan' }] }, transcript: [] } }), plannerAction: async () => response('completed'), stop: async () => {} };
  });
  const choices = [{ id: 'repo', label: 'Observatory artifact', access: 'write' }, { id: 'project', label: 'Granted project', access: 'read' }];
  const bridge = new ArtifactRuntimeBridge(session, { repositories: async () => choices });
  const connection = await bridge.start(); t.after(() => bridge.close());
  const origin = connection.url.replace('host.docker.internal', '127.0.0.1');
  async function request(route, body) {
    return fetch(origin + '/v1/' + route, { method: body === undefined ? 'GET' : 'POST', headers: { 'x-skillz-runtime-token': connection.token, 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  }
  assert.deepEqual((await (await request('runtime')).json()).repositories, choices);
  for (const repositoryId of ['/Users/ungranted', '../other', 'missing', {}, '']) {
    assert.equal((await request('runs', { text: 'Task', repositoryId })).status, 400);
  }
  assert.equal(scopes.length, 0);
  const started = await request('runs', { text: 'Inspect selected project', repositoryId: 'project', repository: { label: 'Spoofed', access: 'write' } });
  assert.equal(started.status, 202); const { id } = await started.json(); await settle(session);
  assert.deepEqual(scopes, [choices[1]]);
  assert.deepEqual(session.get(id).repository, choices[1]);
  assert.equal((await request(`runs/${id}/continue`, { action: 'approve_plan', repositoryId: 'repo' })).status, 400);
  assert.equal(session.get(id).status, 'awaiting-input');
  assert.equal((await request(`runs/${id}/continue`, { action: 'approve_plan' })).status, 202); await settle(session);
  await session.stop();
  const saved = JSON.parse(await fs.readFile(path.join(root, id + '.json'), 'utf8'));
  assert.deepEqual(saved.repository, choices[1]);
  const restored = new ArtifactRunSession(root, selection, async () => { throw Error('Replay must not execute'); });
  await restored.load(); assert.deepEqual(restored.get(id).repository, choices[1]); await restored.close();
});
