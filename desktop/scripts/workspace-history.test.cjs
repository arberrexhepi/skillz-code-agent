const assert = require('node:assert/strict');
const fs = require('node:fs');
const { EventEmitter } = require('node:events');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { WorkspaceHistoryService, WorkspaceService } = load(() => ({
  ...require('../src/main/services/workspaceHistory.ts'),
  ...require('../src/main/services/workspace.ts'),
}));

test('recent repositories persist in local Markdown, deduplicate, and retain unavailable entries', async (t) => {
  const temp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz recent ë ')));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true, maxRetries: 5 }));
  const file = path.join(temp, '.recent_repositories.md');
  const first = path.join(temp, 'first repo ë');
  const second = path.join(temp, 'second repo');
  fs.mkdirSync(first); fs.mkdirSync(second);
  const history = new WorkspaceHistoryService(file);
  await history.record(first); await history.record(second); await history.record(first);
  let recent = await history.recent();
  assert.deepEqual(recent.map(item => item.root), [fs.realpathSync(first), fs.realpathSync(second)]);
  assert.deepEqual(recent.map(item => item.available), [true, true]);
  const markdown = fs.readFileSync(file, 'utf8');
  assert.match(markdown, /^# Recent repositories/);
  assert.match(markdown, /```json/);
  assert.match(markdown, /first repo ë/);
  fs.rmSync(second, { recursive: true });
  recent = await history.recent();
  assert.equal(recent[1].available, false);
});

test('workspace open records canonical roots, close returns home, and malformed history is harmless', async (t) => {
  const temp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz history service ')));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true, maxRetries: 5 }));
  const file = path.join(temp, '.recent_repositories.md');
  const repository = path.join(temp, 'repository'); fs.mkdirSync(repository);
  const history = new WorkspaceHistoryService(file);
  const workspace = new WorkspaceService(() => {}, history);
  const opened = await workspace.open(repository);
  assert.equal(opened.root, fs.realpathSync(repository));
  assert.equal((await workspace.recent())[0].root, opened.root);
  workspace.close(); assert.equal(workspace.current(), null);
  fs.writeFileSync(file, '# Recent repositories\n\n```json\n{broken}\n```\n');
  assert.deepEqual(await history.recent(), []);
  workspace.dispose();
});

test('workspace watcher exhaustion degrades file refresh without crashing the terminal host', async (t) => {
  const temp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz watcher recovery ')));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true, maxRetries: 5 }));
  const watcher = new EventEmitter();
  let closes = 0;
  watcher.close = () => { closes++; };
  t.mock.method(fs, 'watch', () => watcher);
  const workspace = new WorkspaceService(() => {});
  await workspace.open(temp);

  assert.doesNotThrow(() => watcher.emit('error', Object.assign(new Error('too many open files, watch'), { code: 'EMFILE' })));
  assert.equal(closes, 1);
  assert.equal(workspace.current().root, temp);
  workspace.dispose();
  assert.equal(closes, 1);
});
