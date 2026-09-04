const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const load = require('./load-ts.cjs');

test('workspace creates blank files without overwriting or accepting non-portable names', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'skillz-new-file-'));
  fs.mkdirSync(path.join(root, 'src'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const { WorkspaceService } = load(() => require('../src/main/services/workspace.ts'));
  const workspace = new WorkspaceService(() => {});
  await workspace.open(root);
  t.after(() => workspace.dispose());

  const created = await workspace.createFile('src', 'feature.ts');
  assert.equal(created.path, 'src/feature.ts');
  assert.equal(created.content, '');
  assert.equal(fs.readFileSync(path.join(root, 'src', 'feature.ts'), 'utf8'), '');
  await assert.rejects(workspace.createFile('src', 'feature.ts'), /already exists/);
  for (const name of ['', '..', 'nested/file.ts', 'trailing.', 'NUL.txt']) {
    await assert.rejects(workspace.createFile('src', name));
  }
});

test('workspace entry context menus validate paths and expose file and folder actions', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'skillz-entry-menu-'));
  const source = path.join(root, 'src');
  const file = path.join(source, 'app.ts');
  fs.mkdirSync(source);
  fs.writeFileSync(file, 'export {};\n');
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));

  const handlers = new Map();
  const templates = [];
  const clipboardWrites = [];
  const revealed = [];
  let selectedLabel = '';
  const ipcMain = {
    removeHandler() {}, removeAllListeners() {}, on() {},
    handle: (name, listener) => handlers.set(name, listener),
  };
  const electron = {
    ipcMain,
    clipboard: { writeText: (value) => clipboardWrites.push(value) },
    dialog: {},
    shell: { showItemInFolder: (value) => revealed.push(value) },
    Menu: {
      buildFromTemplate: (template) => {
        templates.push(template);
        return { popup: ({ callback }) => { template.find((item) => item.label === selectedLabel)?.click(); callback(); } };
      },
    },
  };
  const original = Module._load;
  t.mock.method(Module, '_load', function (request, ...rest) {
    return request === 'electron' ? electron : original.call(this, request, ...rest);
  });
  const { registerIpc } = load(() => require('../src/main/ipc.ts'));
  const workspace = {
    requireRoot: () => root,
    resolve: (relative) => path.resolve(root, relative),
  };
  const window = { webContents: { id: 42 } };
  registerIpc(window, { workspace });
  const event = { sender: { id: 42 } };
  const show = handlers.get('workspace:show-entry-menu');

  selectedLabel = 'Open';
  assert.equal(await show(event, { name: 'app.ts', path: 'src/app.ts', kind: 'file' }, false), 'open');
  assert.equal(templates.at(-1)[0].label, 'Open');

  selectedLabel = 'New File…';
  assert.equal(await show(event, { name: 'src', path: 'src', kind: 'directory' }, false), 'new-file');
  selectedLabel = 'Expand';
  assert.equal(await show(event, { name: 'src', path: 'src', kind: 'directory' }, false), 'toggle');
  selectedLabel = 'Collapse';
  assert.equal(await show(event, { name: 'src', path: 'src', kind: 'directory' }, true), 'toggle');

  selectedLabel = 'Copy Relative Path';
  assert.equal(await show(event, { name: 'app.ts', path: 'src/app.ts', kind: 'file' }, false), null);
  selectedLabel = 'Copy Full Path';
  await show(event, { name: 'app.ts', path: 'src/app.ts', kind: 'file' }, false);
  assert.deepEqual(clipboardWrites, ['src/app.ts', fs.realpathSync(file)]);

  selectedLabel = process.platform === 'darwin' ? 'Reveal in Finder' : process.platform === 'win32' ? 'Reveal in File Explorer' : 'Show in File Manager';
  await show(event, { name: 'src', path: 'src', kind: 'directory' }, false);
  assert.deepEqual(revealed, [fs.realpathSync(source)]);

  await assert.rejects(show(event, { name: 'outside', path: '../outside', kind: 'directory' }, false), /outside the active workspace|ENOENT/);
  await assert.rejects(show(event, { name: 'src', path: 'src', kind: 'file' }, false), /has changed/);
});
