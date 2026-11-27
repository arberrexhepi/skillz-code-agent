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

test('managed browser supports viewport, reduced motion, input steps, and isolated page evaluate', { timeout: 30000 }, async t => {
  const { ArtifactPreviewService, artifactPreviewActionSchema } = load(() => ({
    ...require('../src/main/services/artifactPreview.ts'),
    ...require('../src/shared/artifactAppMcp.ts'),
  }));
  const preview = new ArtifactPreviewService();
  const server = createServer((_request, response) => {
    response.setHeader('content-type', 'text/html');
    response.end(`<!doctype html>
      <style>#canvas{width:200px;height:120px;border:1px solid #333;}</style>
      <input id="query" type="text" />
      <button id="go">Go</button>
      <div id="status">idle</div>
      <div id="canvas">canvas</div>
      <script>
        let drags = 0, wheel = 0;
        const canvas = document.querySelector('#canvas');
        canvas.addEventListener('pointerdown', () => { drags += 1; });
        canvas.addEventListener('wheel', (event) => { wheel += event.deltaY; });
        document.querySelector('#go').addEventListener('click', () => {
          document.querySelector('#status').textContent = 'ran:' + document.querySelector('#query').value;
        });
        window.__probe = () => ({
          drags,
          wheel,
          width: window.innerWidth,
          height: window.innerHeight,
          reduced: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
          status: document.querySelector('#status').textContent,
        });
      </script>`);
  });
  t.after(async () => {
    await preview.dispose();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const url = `http://127.0.0.1:${server.address().port}`;
  const parsed = artifactPreviewActionSchema.parse({
    operation: 'browser-check',
    input: {
      route: '/',
      viewport: { width: 960, height: 640 },
      reducedMotion: 'reduce',
      timeoutMs: 20000,
      steps: [
        { type: 'fill', selector: '#query', value: 'arber', label: 'Fill search query' },
        { type: 'press', key: 'Tab', label: 'Tab to button' },
        { type: 'assert-focused', selector: '#go', label: 'Button receives focus' },
        { type: 'press', key: 'Enter', label: 'Activate button via keyboard' },
        { type: 'assert-text', selector: '#status', text: 'ran:arber', exact: true, label: 'Status updated' },
        { type: 'drag', selector: '#canvas', dx: 40, dy: 20, steps: 4, label: 'Drag canvas' },
        { type: 'wheel', selector: '#canvas', deltaY: -120, label: 'Zoom canvas' },
        {
          type: 'evaluate',
          label: 'Read measurements',
          script: `() => {
            const p = window.__probe();
            const ok = p.width === 960 && p.height === 640 && p.reduced && p.drags === 1 && p.wheel === -120 && p.status === 'ran:arber';
            return {
              probe: p,
              verification_results: {
                'check-1': { status: ok ? 'pass' : 'fail', evidence: JSON.stringify(p) },
              },
            };
          }`,
        },
      ],
    },
  });
  const observed = await preview.checkRoute(url, parsed.input.route, parsed.input.steps, {
    viewport: parsed.input.viewport,
    reducedMotion: parsed.input.reducedMotion,
    timeoutMs: parsed.input.timeoutMs,
  });
  assert.equal(observed.ok, true, observed.error);
  assert.equal(observed.verification_results['check-1'].status, 'pass');
  assert.equal(observed.measurements.length, 1);
  assert.equal(observed.measurements[0].value.probe.reduced, true);
  assert.equal(observed.measurements[0].value.probe.width, 960);

  const unlabeled = artifactPreviewActionSchema.parse({
    operation: 'browser-check',
    input: {
      route: '/',
      timeoutMs: 60000,
      steps: [
        { type: 'click', selector: '#go' },
        { type: 'evaluate', timeoutMs: 45000, script: '() => ({ ok: true })' },
      ],
    },
  });
  const ranUnlabeled = await preview.checkRoute(url, unlabeled.input.route, unlabeled.input.steps, { timeoutMs: unlabeled.input.timeoutMs });
  assert.equal(ranUnlabeled.ok, true, ranUnlabeled.error);
  assert.deepEqual(ranUnlabeled.observations, [
    { type: 'click', label: 'click #go', selector: '#go' },
    { type: 'evaluate', label: 'evaluate' },
  ]);

  const strict = await preview.checkRoute(url, '/', [
    { type: 'click', selector: 'div', label: 'Ambiguous selector' },
    { type: 'assert-visible', selector: '#status', label: 'Status' },
  ]);
  assert.equal(strict.ok, false);
  assert.equal(strict.verifierError, true);
  assert.match(strict.error, /strict mode violation/i);
});
