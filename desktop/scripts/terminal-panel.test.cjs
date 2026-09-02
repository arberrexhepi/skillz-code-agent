const assert = require('node:assert/strict');
const { test } = require('node:test');
const Module = require('node:module');
const React = require('react');
const load = require('./load-ts.cjs');

// Execute the actual component's effects with controllable IPC, xterm, and
// observer lifecycles; no timers, browser, or native shell are needed.
function fixture(t) {
  const effects = [];
  const labels = [];
  const terminals = [];
  const observers = [];
  const requests = [];
  const events = new Set();
  const calls = { writes: [], sizes: [], disposed: [] };
  const api = {
    create: () => new Promise((resolve, reject) => requests.push({ resolve, reject })),
    write: (...args) => calls.writes.push(args),
    resize: (...args) => calls.sizes.push(args),
    dispose: (id) => calls.disposed.push(id),
    onEvent: (listener) => { events.add(listener); return () => events.delete(listener); },
  };
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  const previousObserver = Object.getOwnPropertyDescriptor(globalThis, 'ResizeObserver');
  globalThis.window = { workbench: { terminal: api } };
  globalThis.ResizeObserver = class {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe() {}
    disconnect() { this.disconnected = true; }
  };
  t.after(() => {
    for (const [key, previous] of [['window', previousWindow], ['ResizeObserver', previousObserver]]) {
      if (previous) Object.defineProperty(globalThis, key, previous);
      else delete globalThis[key];
    }
  });
  const fakeReact = {
    ...React,
    useRef: () => ({ current: {} }),
    useState: (initial) => [initial, (value) => labels.push(value)],
    useEffect: (effect) => effects.push(effect),
  };
  const fakeXterm = { Terminal: class {
    constructor() { this.cols = 80; this.rows = 24; this.output = []; terminals.push(this); }
    loadAddon(addon) { addon.terminal = this; }
    open() {}
    write(data) { assert.ok(!this.disposed); this.output.push(data); }
    onData(callback) { this.input = callback; return { dispose: () => { this.inputDisposed = true; } }; }
    dispose() { this.disposed = true; }
  } };
  const fakeFit = { FitAddon: class { fit() { assert.ok(!this.terminal.disposed); } } };
  const original = Module._load;
  t.mock.method(Module, '_load', function (request, ...rest) {
    if (request === 'react') return fakeReact;
    if (request === '@xterm/xterm') return fakeXterm;
    if (request === '@xterm/addon-fit') return fakeFit;
    return original.call(this, request, ...rest);
  });
  const filename = require.resolve('../src/renderer/src/components/TerminalPanel.tsx');
  delete require.cache[filename];
  t.after(() => { delete require.cache[filename]; });
  const { TerminalPanel } = load(() => require(filename));
  TerminalPanel({ embedded: true });
  return { setup: effects[0], labels, terminals, observers, requests, calls, events,
    emit: (event) => { for (const listener of events) listener(event); },
  };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

test('a delayed create reply from an old effect is disposed without replacing the new session', async (t) => {
  const f = fixture(t);
  const cleanupOld = f.setup();
  cleanupOld();
  const cleanupNew = f.setup(); // Strict Mode replay or reopen after collapse.
  f.requests[1].resolve('new-session');
  await flush();
  f.requests[0].resolve('old-session');
  await flush();
  assert.deepEqual(f.calls.disposed, ['old-session']);
  f.terminals[1].input('new input');
  f.observers[1].callback();
  assert.deepEqual(f.calls.writes, [['new-session', 'new input']]);
  assert.ok(f.calls.sizes.every(([id]) => id === 'new-session'));
  assert.doesNotThrow(() => f.observers[0].callback());
  assert.equal(f.terminals[0].inputDisposed, true);
  cleanupNew();
  assert.deepEqual(f.calls.disposed, ['old-session', 'new-session']);
  assert.equal(f.events.size, 0);
});

test('early prompt output belongs only to its session and exit stops input and resize', async (t) => {
  const f = fixture(t);
  const cleanup = f.setup();
  f.emit({ type: 'data', sessionId: 'unrelated', data: 'wrong repository' });
  f.emit({ type: 'data', sessionId: 'session', data: 'shell prompt' });
  f.requests[0].resolve('session');
  await flush();
  assert.deepEqual(f.terminals[0].output, ['shell prompt']);
  f.emit({ type: 'exit', sessionId: 'session', exitCode: 0 });
  f.terminals[0].input('after exit');
  const count = f.calls.sizes.length;
  f.observers[0].callback();
  f.emit({ type: 'data', sessionId: 'other', data: 'another shell' });
  assert.equal(f.calls.sizes.length, count);
  assert.deepEqual(f.calls.writes, []);
  assert.deepEqual(f.terminals[0].output, ['shell prompt']);
  assert.equal(f.labels.at(-1), 'Exited 0');
  cleanup();
  assert.deepEqual(f.calls.disposed, []);
});

test('exit before create reply does not reactivate a closed terminal', async (t) => {
  const f = fixture(t);
  const cleanup = f.setup();
  f.emit({ type: 'exit', sessionId: 'session', exitCode: 1 });
  f.requests[0].resolve('session');
  await flush();
  f.observers[0].callback();
  f.terminals[0].input('input');
  assert.deepEqual(f.calls.sizes, []);
  assert.deepEqual(f.calls.writes, []);
  assert.equal(f.labels.at(-1), 'Exited 1');
  cleanup();
});

test('creation failures are reported only while the panel is mounted', async (t) => {
  const f = fixture(t);
  const cleanupOld = f.setup();
  cleanupOld();
  const cleanupNew = f.setup();
  f.requests[0].reject(new Error('old failure'));
  await flush();
  assert.ok(f.labels.every((label) => !label.includes('old failure')));
  f.requests[1].reject(new Error('shell missing'));
  await flush();
  assert.match(f.labels.at(-1), /Could not start terminal:.*shell missing/);
  cleanupNew();
});
