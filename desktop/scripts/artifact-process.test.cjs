const assert = require('node:assert/strict');
const { test } = require('node:test');
const { EventEmitter, once } = require('node:events');
const childProcess = require('node:child_process');
const tools = process.env.SKILLZ_ARTIFACT_PROCESS_BUNDLE
  ? require(process.env.SKILLZ_ARTIFACT_PROCESS_BUNDLE)
  : require('./load-ts.cjs')(() => ({ ...require('../src/main/services/artifactProcess.ts'), ...require('../src/main/services/hostEnvironment.ts') }));
const { hostEnvironment, spawnModelHelper, terminate, command, runLogged } = tools;

function mac(t) {
  const descriptor = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: 'darwin' });
  t.after(() => Object.defineProperty(process, 'platform', descriptor));
}
function fakeChild(pid = 765432) {
  return Object.assign(new EventEmitter(), { pid, exitCode: null, signalCode: null, stdout: new (require('node:stream').PassThrough)(), stderr: new (require('node:stream').PassThrough)(), kill() { return true; } });
}

test('Finder child PATH includes Docker helpers and Intel/Apple Silicon Homebrew without changing custom precedence or parent env', () => {
  const base = { PATH: '/custom/bin:/usr/bin:/bin:/opt/homebrew/bin', DOCKER_CONTEXT: 'desktop-linux', TOKEN: 'fixture' };
  const env = hostEnvironment(base, 'darwin', '/Users/Developer Name');
  assert.equal(base.PATH, '/custom/bin:/usr/bin:/bin:/opt/homebrew/bin');
  assert.ok(env.PATH.startsWith(base.PATH));
  const dirs = env.PATH.split(':');
  for (const dir of ['/opt/homebrew/bin', '/usr/local/bin', '/Users/Developer Name/.docker/bin', '/Applications/Docker.app/Contents/Resources/bin', '/Users/Developer Name/Applications/Docker.app/Contents/Resources/bin']) assert.ok(dirs.includes(dir), dir);
  assert.equal(new Set(dirs).size, dirs.length);
  assert.equal(env.DOCKER_CONTEXT, base.DOCKER_CONTEXT);
  assert.equal(env.TOKEN, base.TOKEN);
  assert.deepEqual(hostEnvironment(base, 'win32'), base);
  assert.deepEqual(hostEnvironment(base, 'linux'), base);
  assert.ok(hostEnvironment({}, 'darwin', '/Users/example').PATH.startsWith('/usr/bin:/bin:'));
});

test('artifact commands and setup installers pass the Finder-compatible PATH to children', async t => {
  mac(t);
  t.mock.method(childProcess, 'execFile', (exe, args, options, callback) => {
    assert.equal(exe, 'docker');
    assert.ok(options.env.PATH.split(':').includes('/Applications/Docker.app/Contents/Resources/bin'));
    assert.equal(options.shell, undefined);
    callback(null, 'ready', '');
  });
  assert.equal(await command('docker', ['--version'], process.cwd()), 'ready');
  t.mock.method(childProcess, 'spawn', (_exe, _args, options) => {
    assert.ok(options.env.PATH.split(':').includes('/opt/homebrew/bin'));
    assert.equal(options.env.ELECTRON_RUN_AS_NODE, '1');
    assert.equal(options.shell, undefined);
    const child = fakeChild();
    queueMicrotask(() => child.emit('exit', 0));
    return child;
  });
  await runLogged('python3', ['-m', 'venv', '/Users/example/Library/Application Support/skillz/python'], process.cwd(), () => {});
});

test('macOS cancellation kills only explicitly isolated model-helper process groups, including descendants after helper exit', async t => {
  mac(t);
  const sent = [], child = fakeChild();
  t.mock.method(childProcess, 'spawn', (_exe, _args, options) => {
    assert.equal(options.detached, true);
    assert.equal(options.stdio, 'pipe');
    assert.equal(options.windowsHide, true);
    return child;
  });
  t.mock.method(process, 'kill', (pid, signal) => { sent.push([pid, signal]); return true; });
  const helper = spawnModelHelper('python3', ['artifact_model_host.py'], { cwd: process.cwd() });
  // The direct helper has exited, but a Codex descendant may still hold its pipes.
  child.exitCode = 0;
  child.emit('exit', 0);
  await terminate(helper);
  assert.deepEqual(sent, [[-child.pid, 'SIGKILL']]);
  const ordinary = fakeChild(999999);
  let directSignal;
  ordinary.kill = signal => { directSignal = signal; return true; };
  await terminate(ordinary);
  assert.equal(directSignal, 'SIGTERM');
  assert.equal(sent.length, 1, 'an ordinary child never receives a process-group signal');
});

for (const exitParent of [false, true]) {
  test(`POSIX model-helper cleanup closes descendant pipes when ${exitParent ? 'the helper exits naturally' : 'the user stops it'}`, { skip: process.platform === 'win32', timeout: 10000 }, async t => {
    const descendant = "process.on('SIGTERM', () => {}); console.log('descendant-ready'); setInterval(() => {}, 1000);";
    const parent = `const child = require('node:child_process').spawn(process.execPath, ['-e', ${JSON.stringify(descendant)}], {stdio: ['ignore', 'pipe', 'inherit']}); child.stdout.once('data', data => { process.stdout.write(data); ${exitParent ? 'process.exit(0)' : ''} });`;
    const helper = spawnModelHelper(process.execPath, ['-e', parent], { env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' } });
    t.after(() => terminate(helper));
    const closed = once(helper, 'close');
    await once(helper.stdout, 'data');
    if (!exitParent) await terminate(helper);
    const [code, signal] = await closed;
    assert.equal(exitParent ? code : signal, exitParent ? 0 : 'SIGKILL');
  });
}
