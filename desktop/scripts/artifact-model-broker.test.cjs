const assert = require('node:assert/strict');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { ArtifactModelBroker } = load(() => require('../src/main/services/artifactModelBroker.ts'));

test('artifact app model broker requires its scoped token and fixes the selected runtime', async t => {
  const calls = [];
  const broker = new ArtifactModelBroker({ provider: 'openai', model: 'gpt-test' }, async (selection, payload) => { calls.push({ selection, payload }); return { text: 'hello' }; });
  t.after(() => broker.close());
  const connection = await broker.start();
  const url = connection.url.replace('host.docker.internal', '127.0.0.1');
  assert.equal((await fetch(url, { method: 'POST', body: '{}' })).status, 401);
  const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Skillz-Model-Token': connection.token }, body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }] }) });
  assert.equal(response.status, 200);assert.deepEqual(await response.json(), { text: 'hello' });
  assert.deepEqual(calls[0].selection, { provider: 'openai', model: 'gpt-test' });
});
