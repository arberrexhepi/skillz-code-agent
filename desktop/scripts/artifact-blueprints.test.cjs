const assert = require('node:assert/strict');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { artifactApiBlueprints, openApiToArtifactBlueprint, artifactApisSchema } = load(() => ({ ...require('../src/shared/artifactBlueprints.ts'), ...require('../src/shared/artifacts.ts') }));

test('built-in API Blueprints expose valid discovery commands and collection scripts', () => {
  for (const blueprint of artifactApiBlueprints) {
    const parsed = artifactApisSchema.parse({ version: 1, apis: blueprint.apis });
    assert.ok(parsed.apis.length > 0);
    assert.ok(parsed.apis.every(api => (api.commandKind || (api.method === 'GET' ? 'discovery' : 'mutation')) === 'discovery'));
  }
});

test('OpenAPI conversion produces typed requests with path inputs and mutation classification', () => {
  const blueprint = openApiToArtifactBlueprint({ openapi: '3.1.0', info: { title: 'Tasks' }, servers: [{ url: 'https://api.example.com/v1' }], paths: { '/tasks/{taskId}': { patch: { operationId: 'updateTask', parameters: [{ name: 'taskId', required: true, schema: { type: 'string' } }], requestBody: { content: { 'application/json': { schema: { type: 'object', properties: { title: { type: 'string' } }, required: ['title'] } } } }, responses: { 200: { content: { 'application/json': { schema: { type: 'object' } } } } } } } } });
  assert.equal(blueprint.apis[0].id, 'updatetask');
  assert.equal(blueprint.apis[0].method, 'PATCH');
  assert.deepEqual(new Set(blueprint.apis[0].requestSchema.required), new Set(['taskId', 'title']));
  assert.match(blueprint.apis[0].url, /\{\{taskId\}\}/);
});
