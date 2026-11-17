const assert = require('node:assert/strict');
const { before, after, test } = require('node:test');
const { createServer } = require('node:http');
const path = require('node:path');
const { build } = require('esbuild');
const { chromium } = require('playwright');

let server, browser, url;
before(async () => {
  const bundle = await build({ entryPoints: [path.join(__dirname, 'fixtures/artifact-runtime-selection.tsx')], bundle: true,
    write: false, format: 'esm', platform: 'browser', jsx: 'automatic', define: { 'process.env.NODE_ENV': '"development"' } });
  server = createServer((request, response) => {
    response.setHeader('content-type', request.url === '/fixture.js' ? 'text/javascript' : 'text/html');
    response.end(request.url === '/fixture.js' ? bundle.outputFiles[0].contents : '<!doctype html><div id="root"></div><script type="module" src="/fixture.js"></script>');
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
