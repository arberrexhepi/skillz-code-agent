const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const { createServer } = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const load = require('./load-ts.cjs');

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

test('managed browser observes the project workflow and reports interaction errors', { timeout: 30000 }, async t => {
  const { ArtifactPreviewService, artifactPreviewActionSchema } = load(() => ({
    ...require('../src/main/services/artifactPreview.ts'),
    ...require('../src/shared/artifactAppMcp.ts'),
  }));
  const preview = new ArtifactPreviewService();
  const server = createServer((request, response) => {
    const broken = request.url === '/broken';
    response.setHeader('content-type', 'text/html');
    response.end(`<!doctype html><h1 id="home">Project Home</h1>
      <button id="open">Open project</button><main id="viewport" hidden>Editor Viewport</main>
      <script>document.querySelector('#open').onclick = () => {
        document.querySelector('#viewport').hidden = false;
        ${broken ? 'console.error("Editor initialization failed");' : ''}
      };</script>`);
  });
  t.after(async () => {
    await preview.dispose();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const url = `http://127.0.0.1:${server.address().port}`;
  const steps = [
    { type: 'assert-visible', selector: '#home', label: 'Project Home' },
    { type: 'click', selector: '#open', label: 'Open project' },
    { type: 'assert-visible', selector: '#viewport', label: 'Editor Viewport' },
  ];
  const request = { operation: 'browser-check', input: { route: '/', steps } };
  const { input } = artifactPreviewActionSchema.parse(request);
  const observed = await preview.checkRoute(url, input.route, input.steps);
  assert.equal(observed.ok, true, observed.error);
  assert.deepEqual(observed.observations, steps);

  const broken = await preview.checkRoute(url, '/broken', steps);
  assert.equal(broken.ok, false);
  assert.match(broken.consoleMessages[0].text, /Editor initialization failed/);
  const incomplete = await preview.checkRoute(url, '/', [steps[0], steps[2]]);
  assert.equal(incomplete.ok, false);
  assert.match(incomplete.error, /^Editor Viewport:/);
  assert.deepEqual(incomplete.observations, [steps[0]]);

  for (const extra of [{ type: 'goto', route: '//example.test', label: 'Leave preview' }, { type: 'evaluate', code: '1' }]) {
    assert.equal(artifactPreviewActionSchema.safeParse({ ...request, input: { route: '/', steps: [...steps, extra] } }).success, false);
  }
});
