const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

test('live artifact iframe interactions and Electron security boundary', { timeout: 90000 }, async (t) => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz iframe ')));
  t.after(async () => {
    assert.equal(fs.realpathSync(path.dirname(root)), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('skillz iframe '));
    await fs.promises.rm(root, { recursive: true, force: true, maxRetries: 5 });
  });
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const output = await new Promise((resolve, reject) => {
    execFile(require('electron'), [path.join(__dirname, 'fixtures/artifact-frame-electron.cjs'), root],
      { env, windowsHide: true, timeout: 80000, maxBuffer: 1024 * 1024 },
      (error, stdout, stderr) => error ? reject(new Error(`${error.message}\n${stdout}\n${stderr}`)) : resolve(stdout));
  });
  assert.match(output, /Live iframe checks passed/);
});
