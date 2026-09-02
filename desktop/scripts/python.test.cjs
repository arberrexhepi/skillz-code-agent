const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const { test } = require('node:test');
const loadTypeScript = require('./load-ts.cjs');
const { resolvePythonCommand } = loadTypeScript(() => require('../src/main/services/python.ts'));

function probes(t, available, exists = () => false) {
  const calls = [];
  t.mock.method(fs, 'existsSync', exists);
  t.mock.method(childProcess, 'execFile', (executable, args, options, callback) => {
    calls.push({ executable, args, options });
    const failure = available(executable, args);
    callback(failure ? new Error(failure) : null, '', '');
  });
  return calls;
}

test('Windows falls back to py -3 when python is not on PATH', async (t) => {
  const calls = probes(t, (exe) => exe === 'py' ? null : 'spawn python ENOENT');
  assert.deepEqual(await resolvePythonCommand('C:\\repo', 'win32', {}), { executable: 'py', args: ['-3'] });
  assert.deepEqual(calls.map((call) => call.executable), ['python', 'py']);
  assert.equal(calls[1].args[0], '-3');
  assert.equal(calls[1].options.windowsHide, true);
  assert.equal(calls[1].options.shell, undefined);
});

test('Windows preserves a working python from PATH', async (t) => {
  const calls = probes(t, () => null);
  assert.deepEqual(await resolvePythonCommand('C:\\repo', 'win32', {}), { executable: 'python', args: [] });
  assert.equal(calls.length, 1);
});

test('Windows skips broken aliases, Python 2, and an unavailable launcher', async (t) => {
  const calls = probes(t, (exe) => exe === 'python3' ? null : 'Command failed with code 1');
  assert.deepEqual(await resolvePythonCommand('C:\\repo', 'win32', {}), { executable: 'python3', args: [] });
  assert.deepEqual(calls.map((call) => call.executable), ['python', 'py', 'python3']);
  assert.match(calls[0].args.at(-1), /sys.version_info\[0\] == 3/);
});

test('an explicit executable path with spaces takes precedence and stays one argument', async (t) => {
  const calls = probes(t, () => null, () => true);
  const executable = 'C:\\Custom Python\\python.exe';
  const env = { PYTHON_AGENT_PYTHON: `  ${executable}  ` };
  assert.deepEqual(await resolvePythonCommand('C:\\repo', 'win32', env), { executable, args: [] });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].executable, executable);
  assert.equal(calls[0].options.env, env);
});

for (const [platform, root, executable] of [
  ['win32', 'C:\\Repo With Spaces', 'C:\\Repo With Spaces\\.venv\\Scripts\\python.exe'],
  ['darwin', '/repo', '/repo/.venv/bin/python'],
  ['linux', '/repo', '/repo/.venv/bin/python'],
]) {
  test(`${platform} prefers its repository virtual environment`, async (t) => {
    const calls = probes(t, () => null, (candidate) => candidate === executable);
    assert.deepEqual(await resolvePythonCommand(root, platform, {}), { executable, args: [] });
    assert.equal(calls.length, 1);
  });
}

for (const platform of ['darwin', 'linux']) {
  test(`${platform} continues to prefer python3 on PATH`, async (t) => {
    probes(t, () => null);
    assert.deepEqual(await resolvePythonCommand('/repo', platform, {}), { executable: 'python3', args: [] });
  });
}

test('a broken explicit override reports the problem without changing environments', async (t) => {
  const calls = probes(t, () => 'access denied');
  await assert.rejects(resolvePythonCommand('C:\\repo', 'win32', { PYTHON_AGENT_PYTHON: 'missing.exe' }), /PYTHON_AGENT_PYTHON.*missing.exe: access denied/);
  assert.equal(calls.length, 1);
});

test('a broken virtual environment is not silently replaced', async (t) => {
  const calls = probes(t, () => 'base interpreter missing', () => true);
  await assert.rejects(resolvePythonCommand('C:\\repo', 'win32', {}), /repository interpreter must be usable/);
  assert.equal(calls.length, 1);
});

test('missing Python gives Windows setup instructions and attempted commands', async (t) => {
  probes(t, () => 'ENOENT');
  await assert.rejects(resolvePythonCommand('C:\\repo', 'win32', {}), (error) => {
    assert.match(error.message, /py -3 -m venv \.venv/);
    assert.match(error.message, /PYTHON_AGENT_PYTHON/);
    assert.match(error.message, /python: ENOENT; py -3: ENOENT; python3: ENOENT/);
    return true;
  });
});
