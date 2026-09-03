const assert = require('node:assert/strict');
const Module = require('node:module');
const { test } = require('node:test');
const load = require('./load-ts.cjs');

test('native picker supports cancellation and validated settings IPC rejects untrusted input', async (t) => {
  const handlers = new Map();
  let selection = { canceled: true, filePaths: [] };
  let dialogOptions;
  const saved = [];
  const electron = {
    ipcMain: { removeHandler: () => {}, removeAllListeners: () => {}, on: () => {}, handle: (channel, listener) => handlers.set(channel, listener) },
    dialog: { showOpenDialog: async (_parent, options) => { dialogOptions = options; return selection; } },
    shell: {},
  };
  const original = Module._load;
  t.mock.method(Module, '_load', function (request, ...rest) {
    return request === 'electron' ? electron : original.call(this, request, ...rest);
  });
  const { registerIpc } = load(() => require('../src/main/ipc.ts'));
  registerIpc({ webContents: { id: 42 } }, { agent: { setCodexCliPath: (value) => { saved.push(value); return {}; } } });
  const event = { sender: { id: 42 } };
  const choose = handlers.get('agent:choose-codex-cli');
  assert.equal(await choose(event), null);
  assert.deepEqual(saved, []);
  selection = { canceled: false, filePaths: ['C:\\Codex With Spaces\\codex.exe'] };
  assert.equal(await choose(event), selection.filePaths[0]);
  assert.deepEqual(saved, []); // Browsing alone never changes the selection.
  assert.ok(dialogOptions.properties.includes('openFile'));
  assert.ok(dialogOptions.properties.includes('showHiddenFiles'));
  const save = handlers.get('agent:set-codex-cli-path');
  assert.throws(() => save({ sender: { id: 99 } }, null), /Untrusted IPC sender/);
  for (const value of [42, {}, '', 'C:\\codex.exe\0junk']) assert.throws(() => save(event, value));
  await save(event, selection.filePaths[0]);
  await save(event, null);
  assert.deepEqual(saved, [selection.filePaths[0], null]);
});
