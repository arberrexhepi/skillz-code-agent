const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { RuntimeSettingsService } = load(() => require('../src/main/services/runtimeSettings.ts'));

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'skillz cli settings '));
  t.after(() => {
    assert.equal(path.dirname(root), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('skillz cli settings '));
    fs.rmSync(root, { recursive: true, force: true });
  });
  const executable = path.join(root, 'codex.exe');
  fs.writeFileSync(executable, 'fixture');
  const settingsPath = path.join(root, 'preferences', 'runtime-settings.json');
  return { root, executable, settingsPath, settings: new RuntimeSettingsService(settingsPath) };
}

function version(t, output = 'codex-cli 1.2.3', error = null) {
  const calls = [];
  t.mock.method(childProcess, 'execFile', (executable, args, options, callback) => {
    calls.push({ executable, args, options });
    callback(error, output, '');
  });
  return calls;
}

test('validated path with spaces persists across restarts and reset restores discovery', async (t) => {
  const { executable, settingsPath, settings } = fixture(t);
  const calls = version(t);
  assert.equal(await settings.codexCliPath(), '');
  await settings.setCodexCliPath(executable);
  assert.equal(await new RuntimeSettingsService(settingsPath).codexCliPath(), executable);
  assert.deepEqual(calls[0].args, ['--version']);
  assert.equal(calls[0].executable, executable);
  assert.equal(calls[0].options.shell, undefined);
  assert.equal(calls[0].options.windowsHide, true);
  await settings.setCodexCliPath(null);
  assert.equal(await new RuntimeSettingsService(settingsPath).codexCliPath(), '');
  assert.equal(calls.length, 1);
});

test('wrong executable does not overwrite the last working selection', async (t) => {
  const { executable, settings } = fixture(t);
  const calls = version(t);
  await settings.setCodexCliPath(executable);
  t.mock.method(childProcess, 'execFile', (_exe, _args, _options, callback) => callback(null, 'node v22', ''));
  await assert.rejects(settings.setCodexCliPath(executable), /does not identify itself as Codex CLI/);
  assert.equal(await settings.codexCliPath(), executable);
  assert.equal(calls.length, 1);
});

test('missing files, directories, and relative paths are rejected before execution', async (t) => {
  const { root, settings } = fixture(t);
  const calls = version(t);
  for (const candidate of [path.join(root, 'missing.exe'), path.join(root, 'directory.exe')]) {
    if (candidate.endsWith('directory.exe')) fs.mkdirSync(candidate);
    await assert.rejects(settings.setCodexCliPath(candidate), /does not exist or is not a file/);
  }
  await assert.rejects(settings.setCodexCliPath('codex.exe'), /full path/);
  assert.equal(calls.length, 0);
});

test('launch failures and timeouts leave settings unsaved', async (t) => {
  const { executable, settings, settingsPath } = fixture(t);
  version(t, '', new Error('operation timed out'));
  await assert.rejects(settings.setCodexCliPath(executable), /Could not run.*timed out/);
  assert.equal(await settings.codexCliPath(), '');
  assert.equal(fs.existsSync(settingsPath), false);
});

test('storage failure preserves previous settings and cleans temporary files', async (t) => {
  const { executable, settings, settingsPath } = fixture(t);
  version(t);
  await settings.setCodexCliPath(executable);
  t.mock.method(fs.promises, 'rename', async () => { throw new Error('disk is read only'); });
  await assert.rejects(settings.setCodexCliPath(null), /disk is read only/);
  assert.equal(await settings.codexCliPath(), executable);
  assert.deepEqual(fs.readdirSync(path.dirname(settingsPath)), ['runtime-settings.json']);
});
