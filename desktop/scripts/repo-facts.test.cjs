const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const load = require('./load-ts.cjs');
const { parseRepoFacts, filterRepoFacts, readRepoFacts, RepoFactsView, repoFactsMarkdown, repoFactsPayload } = load(() => ({
  ...require('../src/shared/repoFacts.ts'), ...require('../src/main/services/repoFacts.ts'),
  ...require('../src/renderer/src/components/RepoFactsPanel.tsx'), ...require('./fixtures/repo-facts.ts'),
}));
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'skillz facts ë '));
  t.after(() => {
    assert.equal(fs.realpathSync(path.dirname(root)), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('skillz facts ë '));
    fs.rmSync(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  });
  return { root, file: path.join(root, 'repo_facts.md') };
}

test('schema 2 preserves facts, Unicode, provenance, checkpoints, and scope metadata', () => {
  const ledger = parseRepoFacts(repoFactsMarkdown);
  assert.equal(ledger.schemaVersion, 2);
  assert.equal(ledger.activeIssueId, 'issue-042');
  assert.equal(ledger.issues.length, 4);
  const active = ledger.issues.find((item) => item.id === ledger.activeIssueId);
  assert.equal(active.facts[1].kind, 'goal');
  assert.equal(active.facts[1].run, 12);
  assert.equal(active.facts[1].step, 6);
  assert.equal(active.parentId, 'issue-039');
  assert.equal(active.checkpoints[0].signature, 'goal-reader-42');
  assert.match(ledger.issues[0].facts[0].value, /kërkesë.ts.*🧭/);
  assert.deepEqual(ledger.warnings, []);
  assert.deepEqual(parseRepoFacts('\uFEFF' + repoFactsMarkdown.replaceAll('\n', '\r\n')), ledger);
});

test('legacy lists and facts wrappers remain readable without synthesizing new persisted state', () => {
  for (const payload of [[{ key: 'layout', value: 'src/app.ts', updated_step: 3 }], { facts: [{ key: 'layout', value: 'src/app.ts', updated_step: 3 }] }]) {
    const serialized = JSON.stringify(payload);
    const ledger = parseRepoFacts(serialized);
    assert.equal(ledger.legacy, true);
    assert.equal(ledger.issues[0].repositoryScope, true);
    assert.equal(ledger.issues[0].facts[0].kind, 'architecture');
    assert.equal(ledger.issues[0].facts[0].step, 3);
    assert.equal(JSON.stringify(payload), serialized);
  }
});

test('empty, malformed, unsupported, and partially invalid records are distinguished', () => {
  assert.deepEqual(parseRepoFacts('  ').issues, []);
  assert.deepEqual(parseRepoFacts('[]').issues, []);
  assert.deepEqual(parseRepoFacts('{"facts":[]}').issues, []);
  assert.throws(() => parseRepoFacts('# Repo Facts\n```json\n{bad}\n```'), /valid JSON/);
  assert.throws(() => parseRepoFacts('{"schema_version":999}'), /not supported/);
  assert.throws(() => parseRepoFacts('{"schema_version":2,"issues":{}}'), /must be a list/);
  assert.throws(() => parseRepoFacts(JSON.stringify({ schema_version: 2, issues: [{ issue_id: 'x', facts: {} }] })), /must be lists/);
  assert.throws(() => parseRepoFacts(JSON.stringify({ schema_version: 2, issues: [{ issue_id: 'x' }, { issue_id: 'x' }] })), /Duplicate/);
  const ledger = parseRepoFacts(JSON.stringify({ schema_version: 2, active_issue_id: 'missing', issues: [null, { issue_id: 'x', facts: [null, { key: 'valid', value: 'retained' }, { key: 'bad' }] }] }));
  assert.equal(ledger.issues[0].facts.length, 1);
  assert.match(ledger.warnings[0], /3 invalid record/);
  assert.match(ledger.warnings[1], /active issue.*missing/);
  assert.equal(ledger.activeIssueId, '');
});

test('search and type/scope filters match fact values and checkpoint validation without mutating the ledger', () => {
  const ledger = parseRepoFacts(repoFactsMarkdown);
  const original = JSON.stringify(ledger);
  const all = filterRepoFacts(ledger, { query: '', kind: 'all', scope: 'all' });
  assert.equal(all[0].id, ledger.activeIssueId);
  assert.equal(all[1].id, 'global-architecture');
  const goals = filterRepoFacts(ledger, { query: 'stopped', kind: 'goal', scope: 'all' });
  assert.equal(goals.length, 1);
  assert.equal(goals[0].facts[0].key, 'facts.offline');
  assert.equal(goals[0].checkpoints.length, 0);
  const checkpoints = filterRepoFacts(ledger, { query: 'Unicode and missing', kind: 'checkpoints', scope: 'issue-042' });
  assert.equal(checkpoints[0].checkpoints.length, 1);
  assert.equal(checkpoints[0].facts.length, 0);
  assert.equal(filterRepoFacts(ledger, { query: 'no such record', kind: 'all', scope: 'all' }).length, 0);
  assert.equal(filterRepoFacts(ledger, { query: '', kind: 'architecture', scope: 'issue-039' })[0].facts.length, 1);
  assert.equal(JSON.stringify(ledger), original);
});

test('renderer uses human-readable cards and escapes stored values as text', () => {
  const ledger = parseRepoFacts(repoFactsMarkdown);
  ledger.issues[1].facts[0].value = '<script>unsafe()</script>\nLong finding preserved.';
  const html = renderToStaticMarkup(React.createElement(RepoFactsView, { ledger }));
  for (const text of ['Architecture', 'Goal facts', 'Search records', 'Record type', 'Scope', 'facts.offline', 'Run 12', 'Step 6', 'File metadata', 'kërkesë.ts']) assert.ok(html.includes(text), text);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test('reader works without Python, preserves the original file, and reports deletion as missing', async (t) => {
  const { root, file } = fixture(t);
  assert.equal((await readRepoFacts(root)).status, 'missing');
  assert.equal(fs.existsSync(file), false);
  fs.writeFileSync(file, repoFactsMarkdown, 'utf8');
  const before = fs.readFileSync(file);
  const snapshot = await readRepoFacts(root);
  assert.equal(snapshot.status, 'ready');
  assert.equal(snapshot.workspaceRoot, root);
  assert.equal(snapshot.path, 'repo_facts.md');
  assert.equal(snapshot.ledger.issues.length, 4);
  assert.ok(snapshot.modifiedAt > 0);
  assert.deepEqual(fs.readFileSync(file), before);
  fs.unlinkSync(file);
  assert.equal((await readRepoFacts(root)).status, 'missing');
});

test('reader exposes malformed and incompatible files without treating them as empty or overwriting them', async (t) => {
  const { root, file } = fixture(t);
  for (const content of ['broken', '{"schema_version":3}']) {
    fs.writeFileSync(file, content);
    const snapshot = await readRepoFacts(root);
    assert.equal(snapshot.status, 'invalid');
    assert.ok(snapshot.error);
    assert.equal(snapshot.ledger, undefined);
    assert.equal(fs.readFileSync(file, 'utf8'), content);
  }
});

test('reader rejects binary, invalid UTF-8, directories, and files beyond the viewer limit', async (t) => {
  const { root, file } = fixture(t);
  fs.writeFileSync(file, Buffer.from([0, 1, 2]));
  await assert.rejects(readRepoFacts(root), /binary/);
  fs.writeFileSync(file, Buffer.from([0xff]));
  await assert.rejects(readRepoFacts(root), /UTF-8/);
  fs.writeFileSync(file, Buffer.alloc(5 * 1024 * 1024 + 1, 32));
  await assert.rejects(readRepoFacts(root), /5 MB/);
  fs.unlinkSync(file);
  fs.mkdirSync(file);
  await assert.rejects(readRepoFacts(root), /regular file/);
});

test('workspace reads reject stale roots before and after asynchronous file reads', async (t) => {
  const { root, file } = fixture(t);
  fs.writeFileSync(file, repoFactsMarkdown);
  const { WorkspaceService } = load(() => require('../src/main/services/workspace.ts'));
  const service = new WorkspaceService(() => {});
  let current = root;
  service.requireRoot = () => current;
  await assert.rejects(service.repoFacts(root + '-old'), /Workspace changed/);
  let release, began;
  const entered = new Promise((resolve) => { began = resolve; });
  const original = fs.promises.readFile;
  t.mock.method(fs.promises, 'readFile', async (...args) => { began(); await new Promise((resolve) => { release = resolve; }); return original(...args); });
  const pending = service.repoFacts(root);
  const rejected = assert.rejects(pending, /Workspace changed/);
  await entered;
  current = root + '-new';
  release();
  await rejected;
});

test('facts IPC only accepts a trusted sender with an explicit workspace root', async (t) => {
  const handlers = new Map();
  const electron = { ipcMain: { removeHandler() {}, removeAllListeners() {}, on() {}, handle: (name, listener) => handlers.set(name, listener) }, dialog: {}, shell: {} };
  const original = Module._load;
  t.mock.method(Module, '_load', function (request, ...rest) { return request === 'electron' ? electron : original.call(this, request, ...rest); });
  const { registerIpc } = load(() => require('../src/main/ipc.ts'));
  const calls = [];
  registerIpc({ webContents: { id: 42 } }, { workspace: { repoFacts: (root) => { calls.push(root); return { status: 'missing' }; } } });
  const read = handlers.get('workspace:repo-facts');
  assert.throws(() => read({ sender: { id: 99 } }, 'C:/repo'), /Untrusted/);
  for (const value of ['', null, 42, {}]) assert.throws(() => read({ sender: { id: 42 } }, value));
  await read({ sender: { id: 42 } }, 'C:/repo');
  assert.deepEqual(calls, ['C:/repo']);
});
