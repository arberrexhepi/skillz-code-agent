const assert = require('node:assert/strict');
const { before, after, test } = require('node:test');
const { createServer } = require('node:http');
const path = require('node:path');
const { build } = require('esbuild');
const { chromium } = require('playwright');

let server, browser, url;
before(async () => {
  const bundle = await build({ entryPoints: [path.join(__dirname, 'fixtures/artifact-runtime-selection.tsx')], bundle: true,
    outdir: 'fixture', write: false, format: 'esm', platform: 'browser', jsx: 'automatic', define: { 'process.env.NODE_ENV': '"development"' } });
  const script = bundle.outputFiles.find(file => file.path.endsWith('.js')).contents;
  const styles = bundle.outputFiles.find(file => file.path.endsWith('.css')).contents;
  server = createServer((request, response) => {
    if (request.url === '/fixture.js') { response.setHeader('content-type', 'text/javascript'); response.end(script); }
    else if (request.url === '/fixture.css') { response.setHeader('content-type', 'text/css'); response.end(styles); }
    else { response.setHeader('content-type', 'text/html'); response.end('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"><div id="root"></div><script type="module" src="/fixture.js"></script>'); }
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
  page.on('pageerror', error => console.error('Fixture renderer error:', error.message));
  t.after(() => page.close());
  await page.goto(url);
  await page.getByRole('button', { name: 'Studio one', exact: false }).click();
  const session = page.locator('.artifact-session:not([hidden])');
  return { page, session, requests: () => page.evaluate(() => window.artifactRuntimeFixture.requests) };
}
async function save(session, provider, model) {
  await session.getByRole('button', { name: 'Setup', exact: true }).click();
  const config = session.locator('.artifact-setup-panel');
  await config.getByRole('combobox', { name: /^Provider/ }).selectOption(provider);
  await config.getByRole('combobox', { name: /^Model/ }).selectOption(model);
  await config.getByRole('button', { name: 'Save runtime', exact: true }).click();
  await config.getByText('Runtime saved.', { exact: false }).waitFor();
}
async function openRuntime(session) {
  await session.getByTitle('Runtime controls').click();
  return session.locator('.runtime-drawer');
}
const starts = rows => rows.filter(row => row.type === 'start');

test('reasoning choices save immediately and follow the selected model and artifact', async t => {
  const { page, session, requests } = await fixture(t);
  const runtime = await openRuntime(session);
  const all = runtime.getByRole('combobox', { name: 'All', exact: true });
  const verification = runtime.getByRole('combobox', { name: 'Verification worker', exact: true });
  const model = runtime.getByRole('combobox', { name: /^Model/ });
  await all.selectOption('low');
  await verification.selectOption('high');
  assert.equal(await verification.inputValue(), 'high');
  await model.selectOption('fixture-new');
  await all.selectOption('medium');
  assert.equal(await all.locator('option[value=high]').count(), 0);
  await model.selectOption('fixture-old');
  await all.locator('option:checked').filter({ hasText: 'Low' }).waitFor({ state: 'attached' });
  assert.equal(await verification.inputValue(), 'high');
  assert.equal(await runtime.getByRole('combobox', { name: 'Discovery', exact: true }).inputValue(), '');
  await page.getByRole('button', { name: 'Studio two', exact: false }).click();
  const otherRuntime = await openRuntime(page.locator('.artifact-session:not([hidden])'));
  assert.equal(await otherRuntime.getByRole('combobox', { name: 'All', exact: true }).inputValue(), '');
  assert.deepEqual(await requests(), [
    { id: 'one', type: 'reasoning:fixture-old:all:low' },
    { id: 'one', type: 'reasoning:fixture-old:verification:high' },
    { id: 'one', type: 'reasoning:fixture-new:all:medium' },
  ]);
});

test('Config save reaches the existing Agent panel start and stays isolated from other artifacts and the source', async t => {
  const { page, session, requests } = await fixture(t);
  await save(session, 'gemini', 'fixture-new');
  const runtime = await openRuntime(session);
  assert.equal(await runtime.getByRole('combobox', { name: /^Model/ }).inputValue(), 'fixture-new');
  await runtime.getByRole('button', { name: 'Start agent', exact: true }).click();
  await session.getByText('Preserved conversation one', { exact: true }).waitFor();
  assert.deepEqual(starts(await requests()), [{ id: 'one', type: 'start', selection: { provider: 'gemini', model: 'fixture-new', backendScript: 'main_v2.py' } }]);
  await page.getByRole('button', { name: 'Studio two', exact: false }).click();
  const other = page.locator('.artifact-session:not([hidden])');
  const otherRuntime = await openRuntime(other);
  assert.equal(await otherRuntime.getByRole('combobox', { name: /^Model/ }).inputValue(), 'fixture-old');
  await otherRuntime.getByRole('button', { name: 'Start agent', exact: true }).click();
  await other.getByText('Preserved conversation two', { exact: true }).waitFor();
  assert.equal(starts(await requests()).at(-1).selection.model, 'fixture-old');
  assert.equal(starts(await requests()).some(row => row.id === 'source'), false);
});

test('saving while the editing agent runs preserves its model and conversation, then uses the save after stop/start', async t => {
  const { session, requests } = await fixture(t);
  const runtime = await openRuntime(session);
  await runtime.getByRole('button', { name: 'Start agent', exact: true }).click();
  await session.getByText('Preserved conversation one', { exact: true }).waitFor();
  await save(session, 'openai', 'fixture-openai');
  assert.equal(await runtime.getByRole('combobox', { name: /^Provider/ }).inputValue(), 'gemini');
  assert.equal(await runtime.getByRole('combobox', { name: /^Model/ }).inputValue(), 'fixture-old');
  assert.equal((await requests()).some(row => ['stop', 'reconfigure'].includes(row.type)), false);
  await session.getByText('Preserved conversation one', { exact: true }).waitFor();
  await runtime.getByRole('button', { name: 'Stop agent', exact: true }).click();
  await runtime.getByRole('button', { name: 'Start agent', exact: true }).click();
  await session.getByText('Preserved conversation one', { exact: true }).waitFor();
  assert.deepEqual(starts(await requests()).at(-1).selection, { provider: 'openai', model: 'fixture-openai', backendScript: 'main_v2.py' });
});

test('rejected Config changes do not change automatic startup', async t => {
  const { page, session, requests } = await fixture(t);
  await session.getByRole('button', { name: 'Setup', exact: true }).click();
  const config = session.locator('.artifact-setup-panel');
  await config.getByRole('combobox', { name: /^Model/ }).selectOption('fixture-new');
  await page.evaluate(() => window.artifactRuntimeFixture.failSave());
  await config.getByRole('button', { name: 'Save runtime', exact: true }).click();
  await config.getByRole('alert').filter({ hasText: 'Fixture runtime save failed' }).waitFor();
  await session.getByRole('button', { name: 'Send original request', exact: true }).click();
  await session.getByText('Preserved conversation one', { exact: true }).waitFor();
  assert.equal(starts(await requests())[0].selection.model, 'fixture-old');
});

test('an unsaved Config draft does not change Send original request startup', async t => {
  const { session, requests } = await fixture(t);
  await session.getByRole('button', { name: 'Setup', exact: true }).click();
  await session.locator('.artifact-setup-panel').getByRole('combobox', { name: /^Model/ }).selectOption('fixture-new');
  await session.getByRole('button', { name: 'Send original request', exact: true }).click();
  await session.getByText('Preserved conversation one', { exact: true }).waitFor();
  assert.equal(starts(await requests())[0].selection.model, 'fixture-old');
});

test('an explicit Agent panel choice after a Config save is honored on startup', async t => {
  const { session, requests } = await fixture(t);
  await save(session, 'gemini', 'fixture-new');
  const runtime = await openRuntime(session);
  await runtime.getByRole('combobox', { name: /^Model/ }).selectOption('fixture-old');
  await runtime.getByRole('button', { name: 'Start agent', exact: true }).click();
  await session.getByText('Preserved conversation one', { exact: true }).waitFor();
  assert.equal(starts(await requests())[0].selection.model, 'fixture-old');
});

test('open artifacts show their own issue history and use the artifact for offline and agent actions', async t => {
  const { page, session, requests } = await fixture(t);
  await session.getByRole('button', { name: 'Issues', exact: true }).click();
  await session.getByText('Saved issue for studio one', { exact: true }).waitFor();
  await session.getByRole('button', { name: 'Details for issue-042' }).click();
  await session.getByText('Completed goals', { exact: false }).waitFor();
  await session.getByRole('button', { name: 'Close issue-042' }).click();
  await session.getByRole('button', { name: 'Reopen issue-042' }).click();
  assert.deepEqual(await requests(), [{ id: 'one', type: 'close_issue' }, { id: 'one', type: 'reopen_issue' }]);
  await session.getByRole('button', { name: 'Continue issue-042' }).click();
  await session.getByText('Runtime connected', { exact: false }).waitFor();
  assert.deepEqual((await requests()).at(-1), { id: 'one', type: 'continue_issue' });
  await page.getByRole('button', { name: 'Studio two', exact: false }).click();
  await session.getByRole('button', { name: 'Issues', exact: true }).click();
  await session.getByText('Saved issue for studio two', { exact: true }).waitFor();
  assert.equal(await session.getByText('Saved issue for studio one', { exact: true }).count(), 0);
});
