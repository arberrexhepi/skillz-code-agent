const assert = require('node:assert/strict');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { applyBlueprintOperations, runArtifactBlueprintTurn, artifactApisSchema } = load(() => ({
  ...require('../src/main/services/artifactBlueprintAgent.ts'),
  ...require('../src/shared/artifacts.ts'),
}));

const api = (id, method = 'GET') => artifactApisSchema.parse({
  version: 1,
  apis: [{ id, title: id, description: '', collection: 'default', transport: 'http', url: 'https://example.com/' + id, method }],
}).apis[0];

test('Blueprint operations create, update, rename, and delete validated requests atomically', () => {
  const start = { version: 1, apis: [api('users')] };
  const created = applyBlueprintOperations(start, [{ operation: 'create', api: api('issues') }]);
  assert.deepEqual(created.changes, ['Created issues']);
  assert.deepEqual(created.config.apis.map((item) => item.id), ['users', 'issues']);

  const renamed = applyBlueprintOperations(created.config, [{ operation: 'update', id: 'issues', api: { ...api('tickets'), title: 'Tickets' } }]);
  assert.equal(renamed.config.apis[1].id, 'tickets');
  assert.equal(renamed.config.apis[1].title, 'Tickets');

  const deleted = applyBlueprintOperations(renamed.config, [{ operation: 'delete', id: 'users' }]);
  assert.deepEqual(deleted.config.apis.map((item) => item.id), ['tickets']);
  assert.throws(() => applyBlueprintOperations(deleted.config, [{ operation: 'create', api: api('tickets') }]), /already exists/);
  assert.deepEqual(start.apis.map((item) => item.id), ['users']);
});

test('Blueprint mode supplies current configuration and applies only structured model operations', async () => {
  let payload;
  const result = await runArtifactBlueprintTurn({
    current: { version: 1, apis: [api('users')] },
    message: 'Add issues',
    history: [{ role: 'assistant', content: 'Ready.' }],
    selection: { provider: 'gemini', model: 'fixture' },
    request: async (_selection, value) => {
      payload = value;
      return { text: JSON.stringify({ message: 'Added an issues request.', operations: [{ operation: 'create', api: api('issues') }] }) };
    },
  });
  assert.deepEqual(result.changes, ['Created issues']);
  assert.equal(result.config.apis.length, 2);
  assert.match(payload.system, /constrained configuration mode/);
  assert.match(payload.system, /"id":"users"/);
  assert.match(payload.system, /headerEnv maps a header/);
  assert.deepEqual(payload.messages.at(-1), { role: 'user', content: 'Add issues' });
});

test('Blueprint mode rejects invalid or conflicting model changes', async () => {
  const options = {
    current: { version: 1, apis: [api('users')] },
    message: 'Change it',
    history: [],
    selection: { provider: 'gemini', model: 'fixture' },
  };
  await assert.rejects(runArtifactBlueprintTurn({
    ...options,
    request: async () => ({ text: 'not json' }),
  }), /invalid response/);
  await assert.rejects(runArtifactBlueprintTurn({
    ...options,
    request: async () => ({ text: JSON.stringify({ message: 'Duplicate.', operations: [{ operation: 'create', api: api('users') }] }) }),
  }), /already exists/);
});
