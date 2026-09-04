const assert = require('node:assert/strict');
const { test } = require('node:test');
const pty = require('node-pty');
const loadTypeScript = require('./load-ts.cjs');
const { TerminalService, terminalEnvironment } = loadTypeScript(() => require('../src/main/services/terminal.ts'));

test('terminal npm defaults avoid indefinite progress and audit steps without overriding user settings', () => {
  assert.deepEqual(terminalEnvironment({ PATH: '/bin' }), {
    PATH: '/bin',
    NPM_CONFIG_PROGRESS: 'false',
    NPM_CONFIG_AUDIT: 'false',
    NPM_CONFIG_FUND: 'false',
    TERM: 'xterm-256color',
    COLORTERM: 'truecolor',
  });
  const configured = terminalEnvironment({
    npm_config_progress: 'true',
    NPM_CONFIG_AUDIT: 'true',
    Npm_Config_Fund: 'true',
  });
  assert.equal(configured.npm_config_progress, 'true');
  assert.equal(configured.NPM_CONFIG_AUDIT, 'true');
  assert.equal(configured.Npm_Config_Fund, 'true');
  assert.equal(Object.hasOwn(configured, 'NPM_CONFIG_PROGRESS'), false);
  assert.equal(Object.keys(configured).filter((key) => key.toLowerCase() === 'npm_config_audit').length, 1);
  assert.equal(Object.keys(configured).filter((key) => key.toLowerCase() === 'npm_config_fund').length, 1);
});

for (const platform of ['win32', 'darwin', 'linux']) {
  test(`${platform} terminal uses supported encoding options and forwards output`, (t) => {
    const originalPlatform = Object.getOwnPropertyDescriptor(process, 'platform');
    Object.defineProperty(process, 'platform', { value: platform });
    t.after(() => Object.defineProperty(process, 'platform', originalPlatform));
    let onData;
    let onExit;
    let spawned;
    let killed = false;
    const fakePty = {
      onData: (callback) => { onData = callback; },
      onExit: (callback) => { onExit = callback; },
      kill: () => { killed = true; },
    };
    t.mock.method(pty, 'spawn', (shell, args, options) => {
      spawned = { shell, args, options };
      return fakePty;
    });
    const events = [];
    const service = new TerminalService({ requireRoot: () => process.cwd() }, (event) => events.push(event));
    const sessionId = service.create({ cols: 80, rows: 24 });
    if (platform === 'win32') assert.equal(Object.hasOwn(spawned.options, 'encoding'), false);
    else assert.equal(spawned.options.encoding, 'utf8');
    assert.equal(spawned.options.cwd, process.cwd());
    assert.equal(spawned.options.cols, 80);
    assert.equal(spawned.options.env.NPM_CONFIG_PROGRESS, 'false');
    assert.equal(spawned.options.env.NPM_CONFIG_AUDIT, 'false');
    assert.equal(spawned.options.env.NPM_CONFIG_FUND, 'false');
    onData('café');
    assert.deepEqual(events[0], { type: 'data', sessionId, data: 'café' });
    service.disposeAll();
    assert.equal(killed, true);
    onExit({ exitCode: 0 });
    assert.deepEqual(events[1], { type: 'exit', sessionId, exitCode: 0 });
  });
}
const Module = require('node:module');
const { EventEmitter } = require('node:events');

function terminalFixture(t) {
  const children = [];
  let root = '/repo/alpha';
  const workspace = { requireRoot: () => root, current: () => ({ root, name: root.split('/').at(-1) }) };
  t.mock.method(pty, 'spawn', (_shell, _args, options) => {
    const child = { writes: [], sizes: [], kills: 0, options,
      onData(callback) { this.data = callback; },
      onExit(callback) { this.exit = callback; },
      write(data) { this.writes.push(data); },
      resize(cols, rows) { this.sizes.push([cols, rows]); },
      kill() { this.kills++; },
    };
    children.push(child);
    return child;
  });
  const service = new TerminalService(workspace, () => {});
  return { service, workspace, children, switchRoot: (next) => { root = next; } };
}

test('queued input and resize after disposal or exit never touch a replacement terminal', (t) => {
  const { service, children, switchRoot } = terminalFixture(t);
  const oldId = service.create({ cols: 80, rows: 24 });
  service.disposeAll();
  switchRoot('/repo/beta');
  const newId = service.create({ cols: 100, rows: 30 });
  assert.doesNotThrow(() => {
    service.resize(oldId, 90, 25);
    service.write(oldId, 'old command');
    service.dispose(oldId);
  });
  assert.equal(children[0].kills, 1);
  assert.deepEqual(children[0].sizes, []);
  assert.deepEqual(children[0].writes, []);
  assert.deepEqual(children[1].writes, []);
  assert.equal(children[1].options.cwd, '/repo/beta');
  service.resize(newId, 120, 40);
  service.write(newId, 'new command');
  assert.deepEqual(children[1].sizes, [[120, 40]]);
  assert.deepEqual(children[1].writes, ['new command']);
  children[1].exit({ exitCode: 0 });
  assert.doesNotThrow(() => { service.resize(newId, 90, 25); service.write(newId, 'after exit'); service.disposeAll(); });
  assert.deepEqual(children[1].writes, ['new command']);
  assert.equal(children[1].kills, 0);
});

for (const channel of ['workspace:choose', 'workspace:open']) {
  test(`${channel} tolerates terminal events while waiting for the agent to stop`, async (t) => {
    const { service, workspace, children, switchRoot } = terminalFixture(t);
    const ipcMain = new EventEmitter();
    const handlers = new Map();
    const clipboardWrites = [];
    ipcMain.removeHandler = (name) => handlers.delete(name);
    ipcMain.handle = (name, handler) => handlers.set(name, handler);
    const original = Module._load;
    t.mock.method(Module, '_load', function (request, ...rest) {
      return request === 'electron' ? { ipcMain, clipboard: { writeText: (text) => clipboardWrites.push(text) }, dialog: {}, shell: {} } : original.call(this, request, ...rest);
    });
    const filename = require.resolve('../src/main/ipc.ts');
    delete require.cache[filename];
    t.after(() => { delete require.cache[filename]; });
    const { registerIpc } = loadTypeScript(() => require(filename));
    let finishStop;
    let startedStop;
    let stopCalls = 0;
    const stopping = new Promise((resolve) => { startedStop = resolve; });
    const info = { root: '/repo/beta', name: 'beta' };
    workspace.open = async () => { switchRoot(info.root); return info; };
    workspace.choose = workspace.open;
    registerIpc({ webContents: { id: 42 } }, {
      terminal: service, workspace,
      git: { fileDiff: async (path, staged) => ({ path, staged }) },
      agent: { stop: () => { stopCalls++; startedStop(); return new Promise((resolve) => { finishStop = resolve; }); } },
    });
    const event = { sender: { id: 42 } };
    await handlers.get('terminal:copy')(event, 'selected failure');
    assert.deepEqual(clipboardWrites, ['selected failure']);
    assert.deepEqual(await handlers.get('git:file-diff')(event, 'src/app.ts', false), { path: 'src/app.ts', staged: false });
    assert.equal(stopCalls, 0);
    if (channel === 'workspace:open') {
      assert.deepEqual(await handlers.get(channel)(event, '/repo/alpha'), { root: '/repo/alpha', name: 'alpha' });
      assert.equal(children.length, 0);
      assert.equal(stopCalls, 0);
    }
    const oldId = service.create({ cols: 80, rows: 24 });
    const switching = handlers.get(channel)(event, info.root);
    await stopping;
    assert.equal(stopCalls, 1);
    assert.equal(children[0].kills, 1);
    assert.doesNotThrow(() => {
      ipcMain.emit('terminal:resize', event, oldId, 100, 30);
      ipcMain.emit('terminal:write', event, oldId, 'queued input');
      ipcMain.emit('terminal:dispose', event, oldId);
    });
    finishStop();
    assert.deepEqual(await switching, info);
    const newId = handlers.get('terminal:create')(event, { cols: 80, rows: 24 });
    ipcMain.emit('terminal:resize', event, newId, 100, 30);
    ipcMain.emit('terminal:write', event, newId, 'active input');
    assert.deepEqual(children[1].sizes, [[100, 30]]);
    assert.deepEqual(children[1].writes, ['active input']);
    assert.equal(children[1].options.cwd, info.root);
    service.disposeAll();
  });
}
