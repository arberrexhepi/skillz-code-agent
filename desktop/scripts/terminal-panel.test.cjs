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
  const calls = { writes: [], sizes: [], disposed: [], copied: [], sent: [] };
  const api = {
    create: () => new Promise((resolve, reject) => requests.push({ resolve, reject })),
    copy: async (text) => { calls.copied.push(text); },
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
  let refIndex = 0;
  const fakeReact = {
    ...React,
    useContext: () => null,
    useRef: (initial) => ({ current: refIndex++ === 0 ? {} : initial }),
    useState: (initial) => [initial, (value) => labels.push(value)],
    useEffect: (effect) => effects.push(effect),
  };
  const fakeXterm = { Terminal: class {
    constructor() { this.cols = 80; this.rows = 24; this.output = []; this.selection = ''; terminals.push(this); }
    loadAddon(addon) { addon.terminal = this; }
    open() {}
    write(data) { assert.ok(!this.disposed); this.output.push(data); }
    onData(callback) { this.input = callback; return { dispose: () => { this.inputDisposed = true; } }; }
    onSelectionChange(callback) { this.selectionChange = callback; return { dispose: () => { this.selectionDisposed = true; } }; }
    hasSelection() { return Boolean(this.selection); }
    getSelection() { return this.selection; }
    clear() { this.clearCount = (this.clearCount || 0) + 1; }
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
  const { TerminalPanel, cleanTerminalOutput } = load(() => require(filename));
  const tree = TerminalPanel({ embedded: true, onSendToAgent: async (output) => { calls.sent.push(output); return true; } });
  return { setup: effects[0], labels, terminals, observers, requests, calls, events, tree, cleanTerminalOutput,
    emit: (event) => { for (const listener of events) listener(event); },
  };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

function findButton(node, label) {
  if (!node || typeof node !== 'object') return undefined;
  if (node.type === 'button' && textOf(node.props.children) === label) return node;
  for (const child of React.Children.toArray(node.props?.children)) {
    const found = findButton(child, label);
    if (found) return found;
  }
}

function textOf(value) {
  return React.Children.toArray(value).map((child) => typeof child === 'string' ? child : textOf(child?.props?.children)).join('');
}

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

test('terminal actions copy selections, send clean recent output, and clear the display', async (t) => {
  const f = fixture(t);
  const cleanup = f.setup();
  f.requests[0].resolve('session');
  await flush();
  f.emit({ type: 'data', sessionId: 'session', data: '\x1b[31mError: café failed\x1b[0m\r\n' });
  await findButton(f.tree, 'Ask agent').props.onClick();
  assert.deepEqual(f.calls.sent, ['Error: café failed']);
  f.terminals[0].selection = 'selected error';
  f.terminals[0].selectionChange();
  await findButton(f.tree, 'Copy').props.onClick();
  assert.deepEqual(f.calls.copied, ['selected error']);
  findButton(f.tree, 'Clear').props.onClick();
  assert.equal(f.terminals[0].clearCount, 1);
  cleanup();
  assert.equal(f.terminals[0].selectionDisposed, true);
});
