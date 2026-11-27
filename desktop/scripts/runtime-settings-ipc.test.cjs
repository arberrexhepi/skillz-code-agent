const assert = require('node:assert/strict');
const Module = require('node:module');
const { test } = require('node:test');
const load = require('./load-ts.cjs');

test('native picker supports cancellation and validated settings IPC rejects untrusted input', async (t) => {
  const handlers = new Map();
  let selection = { canceled: true, filePaths: [] };
  let dialogOptions;
  const saved = [], savedGemini = [], artifactSelections = [], sourceUpdates = [];
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
  const mainFrame = {};
  registerIpc({ webContents: { id: 42, mainFrame } }, { agent: { setCodexCliPath: (value) => { saved.push(value); return {}; }, setGeminiCliPath: (value) => { savedGemini.push(value); return {}; } }, artifacts: { prebuiltUpdates: { status: () => ({}), review: () => ({}) }, applyPrebuiltUpdate: (id, value) => { sourceUpdates.push({ id, value }); return {}; }, saveRuntime: (id, value) => { artifactSelections.push({ id, value }); return {}; } } });
  const event = { sender: { id: 42 }, senderFrame: mainFrame };
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
  assert.throws(() => save({ sender: { id: 42 }, senderFrame: {} }, null), /Untrusted IPC sender/);
  assert.throws(() => save({ sender: { id: 42 }, senderFrame: null }, null), /Untrusted IPC sender/);
  for (const value of [42, {}, '', 'C:\\codex.exe\0junk']) assert.throws(() => save(event, value));
  await save(event, selection.filePaths[0]);
  await save(event, null);
  assert.deepEqual(saved, [selection.filePaths[0], null]);
  const chooseGemini = handlers.get('agent:choose-gemini-cli');
  selection = { canceled: false, filePaths: ['/opt/homebrew/bin/gemini'] };
  assert.equal(await chooseGemini(event), '/opt/homebrew/bin/gemini');
  const saveGemini = handlers.get('agent:set-gemini-cli-path');
  assert.throws(() => saveGemini({ sender: { id: 99 } }, null), /Untrusted IPC sender/);
  for (const value of [42, {}, '', '/bin/gemini\0junk']) assert.throws(() => saveGemini(event, value));
  await saveGemini(event, '/opt/homebrew/bin/gemini');
  await saveGemini(event, null);
  assert.deepEqual(savedGemini, ['/opt/homebrew/bin/gemini', null]);
  const saveRuntime = handlers.get('artifacts:save-runtime');
  const runtime = { provider: 'codex-subscription', model: 'fixture', backendScript: 'main_v2.py' };
  assert.throws(() => saveRuntime({ sender: { id: 42 }, senderFrame: {} }, 'beta-observatory', runtime), /Untrusted IPC sender/);
  assert.throws(() => saveRuntime(event, '../another-artifact', runtime));
  assert.throws(() => saveRuntime(event, 'beta-observatory', { ...runtime, backendScript: 'arbitrary.py' }));
  await saveRuntime(event, 'beta-observatory', runtime);
  assert.deepEqual(artifactSelections, [{ id: 'beta-observatory', value: runtime }]);
  const applyUpdate = handlers.get('artifacts:apply-prebuilt-update');
  const request = { token: 'a'.repeat(64), resolutions: { 'src/App.tsx': 'incoming' } };
  assert.throws(() => applyUpdate({ sender: { id: 42 }, senderFrame: {} }, 'beta-observatory', request), /Untrusted IPC sender/);
  assert.throws(() => applyUpdate(event, '../other', request));
  assert.throws(() => applyUpdate(event, 'beta-observatory', { ...request, token: 'bad' }));
  assert.throws(() => applyUpdate(event, 'beta-observatory', { ...request, resolutions: { 'src/App.tsx': 'anything' } }));
  await applyUpdate(event, 'beta-observatory', request);
  assert.deepEqual(sourceUpdates, [{ id: 'beta-observatory', value: request }]);
});
