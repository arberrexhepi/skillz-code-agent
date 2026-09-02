const assert = require('node:assert/strict');
const { test } = require('node:test');
const pty = require('node-pty');
const loadTypeScript = require('./load-ts.cjs');
const { TerminalService } = loadTypeScript(() => require('../src/main/services/terminal.ts'));

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
    onData('café');
    assert.deepEqual(events[0], { type: 'data', sessionId, data: 'café' });
    service.disposeAll();
    assert.equal(killed, true);
    onExit({ exitCode: 0 });
    assert.deepEqual(events[1], { type: 'exit', sessionId, exitCode: 0 });
  });
}
