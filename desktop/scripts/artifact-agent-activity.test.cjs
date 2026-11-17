const assert = require('node:assert/strict');
const { before, after, test } = require('node:test');
const { createServer } = require('node:http');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { build } = require('esbuild');
const { chromium } = require('playwright');

let server, browser, url;
before(async () => {
  const bundle = await build({ entryPoints: [path.join(__dirname, 'fixtures/artifact-runtime-selection.tsx')], bundle: true,
    write: false, format: 'esm', platform: 'browser', jsx: 'automatic', define: { 'process.env.NODE_ENV': '"development"' } });
  const css = ['styles', 'artifacts', 'pathChips'].map(name => readFileSync(path.join(__dirname, `../src/renderer/src/${name}.css`), 'utf8')).join('\n');
  server = createServer((request, response) => {
    if (request.url === '/fixture.js') { response.setHeader('content-type', 'text/javascript'); response.end(bundle.outputFiles[0].contents); }
    else if (request.url === '/fixture.css') { response.setHeader('content-type', 'text/css'); response.end(css); }
    else if (request.url === '/preview') { response.setHeader('content-type', 'text/html'); response.end('<!doctype html><body style="background:#14191e;color:#aab8c3;font:18px system-ui;padding:32px"><h1>Studio preview</h1><p>Preview stays mounted while inspecting agent activity.</p>'); }
    else { response.setHeader('content-type', 'text/html'); response.end('<!doctype html><link rel="stylesheet" href="/fixture.css"><div id="root"></div><script type="module" src="/fixture.js"></script>'); }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  url = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});

async function fixture(t) {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(async () => { await page.close(); assert.deepEqual(errors, []); });
  await page.goto(url);
  await page.locator('.artifacts-library').getByRole('button', { name: 'Studio one', exact: false }).click();
  return { page, session: page.locator('.artifact-session:not([hidden])') };
}
async function emit(page, id, event) {
  await page.evaluate(({ id, event }) => window.artifactRuntimeFixture.emitArtifact({ type: 'agent', id, event }), { id, event });
}
const progress = (summary, step = 1) => ({ type: 'progress', payload: { type: 'progress', action_type: 'read_file', summary, path: 'src/motion/export.ts', command: 'read-line-range /repo/src/motion/export.ts 81-280', step, ok: true } });

test('artifact activity streams model state, commands, failures and goal transitions without starting another agent', async t => {
  const { page, session } = await fixture(t);
  const panel = session.getByRole('region', { name: 'Artifact agent activity' });
  const toggle = panel.getByRole('button', { name: /^Agent activity/ });
  assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
  await toggle.click();
  await panel.getByText('No activity yet', { exact: true }).waitFor();
  await emit(page, 'one', { type: 'status', status: 'running' });
  await emit(page, 'one', { type: 'state', state: { planner: { worker_state: { llm_activity: { in_flight: true, last_event: 'model_call_start', turn: 3, elapsed_s: 4 } } }, transcript: [] } });
  await panel.getByText('Model working', { exact: true }).waitFor();
  assert.equal(await panel.getByText('No activity yet', { exact: true }).count(), 0);
  await emit(page, 'one', progress('Read export declarations'));
  await emit(page, 'one', { type: 'progress', payload: { type: 'goal_start', summary: 'Wire MP4 controls', step: 2 } });
  await emit(page, 'one', { type: 'progress', payload: { type: 'progress', action_type: 'run_check', summary: 'Typecheck failed', command: 'npx tsc --noEmit', step: 3, ok: false } });
  await panel.locator('.activity-row.bad').getByText('Typecheck failed').waitFor();
  assert.deepEqual(await panel.locator('.activity-row strong').allTextContents(), ['Typecheck failed', 'Started Wire MP4 controls', 'Read export declarations']);
  assert.equal(await panel.locator('.count-badge').textContent(), '3');
  assert.ok((await panel.textContent()).includes('src/motion/'));
  await toggle.click();
  await emit(page, 'one', progress('Finished while collapsed', 4));
  await toggle.click();
  await panel.getByText('Finished while collapsed', { exact: true }).waitFor();
  assert.equal(await page.evaluate(() => window.artifactRuntimeFixture.requests.length), 0);
});

test('activity stays isolated by artifact and continues collecting in a hidden session', async t => {
  const { page, session } = await fixture(t);
  await session.getByRole('button', { name: /^Agent activity/ }).click();
  await emit(page, 'one', progress('Studio one command'));
  await page.locator('.artifacts-library').getByRole('button', { name: 'Studio two', exact: false }).click();
  const panel = page.locator('.artifact-session:not([hidden]) .artifact-agent-activity');
  await panel.getByRole('button', { name: /^Agent activity/ }).click();
  await emit(page, 'two', progress('Studio two command'));
  await emit(page, 'one', progress('Studio one hidden command', 2));
  await page.evaluate(event => window.artifactRuntimeFixture.emitSourceAgent(event), progress('Source workspace command'));
  await panel.getByText('Studio two command', { exact: true }).waitFor();
  assert.equal(await panel.getByText('Studio one command', { exact: true }).count(), 0);
  assert.equal(await panel.getByText('Source workspace command', { exact: true }).count(), 0);
  await page.locator('.artifacts-library').getByRole('button', { name: 'Studio one', exact: false }).click();
  await panel.getByText('Studio one hidden command', { exact: true }).waitFor();
  assert.equal(await panel.getByRole('button', { name: /^Agent activity/ }).getAttribute('aria-expanded'), 'true');
  assert.equal(await panel.getByText('Studio two command', { exact: true }).count(), 0);
  assert.equal(await panel.getByText('Source workspace command', { exact: true }).count(), 0);
});

test('activity sits below the preview beside the conversation, preserves the iframe, and fits a narrow window', async t => {
  const { page, session } = await fixture(t);
  await page.evaluate(previewUrl => window.artifactRuntimeFixture.emitArtifact({ type: 'runtime', runtime: { id: 'one', status: 'running', url: previewUrl, logs: '' } }), `${url}/preview`);
  const iframe = session.locator('iframe');
  await iframe.waitFor();
  const frame = await (await iframe.elementHandle()).contentFrame();
  await frame.waitForSelector('h1');
  await frame.evaluate(() => { window.previewMarker = 'preserved'; });
  const panel = session.getByRole('region', { name: 'Artifact agent activity' });
  const toggle = panel.getByRole('button', { name: /^Agent activity/ });
  const collapsed = await panel.boundingBox();
  await toggle.click();
  await emit(page, 'one', progress('Read export declarations'));
  await emit(page, 'one', { type: 'progress', payload: { type: 'goal_start', summary: 'Integrate export controls', step: 2 } });
  await panel.getByText('Started Integrate export controls', { exact: true }).waitFor();
  const expanded = await panel.boundingBox(), canvas = await session.locator('.artifact-canvas').boundingBox(), agent = await session.locator('.artifact-agent').boundingBox();
  assert.ok(expanded.height > collapsed.height + 100);
  assert.ok(Math.abs(canvas.x - expanded.x) <= 1);
  assert.ok(Math.abs(canvas.width - expanded.width) <= 1);
  assert.ok(canvas.y + canvas.height <= expanded.y + 1);
  assert.ok(agent.x >= expanded.x + expanded.width - 1);
  assert.ok(agent.y + agent.height >= expanded.y + expanded.height - 1);
  assert.equal(await frame.evaluate(() => window.previewMarker), 'preserved');
  await page.screenshot({ path: '/tmp/skillz-artifact-activity.png' });
  await toggle.click();
  assert.equal(await frame.evaluate(() => window.previewMarker), 'preserved');
  await page.setViewportSize({ width: 820, height: 820 });
  await toggle.click();
  const narrow = await panel.boundingBox();
  assert.ok(narrow.width > 300);
  assert.ok(narrow.x + narrow.width <= 821);
  assert.ok(narrow.y + narrow.height <= 821);
});
