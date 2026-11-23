const assert = require('node:assert/strict');
const { before, after, test } = require('node:test');
const { createServer } = require('node:http');
const path = require('node:path');
const { build } = require('esbuild');
const { chromium } = require('playwright');

let server, browser, url;
before(async () => {
  const bundle = await build({ entryPoints: [path.join(__dirname, 'fixtures/artifact-runtime-selection.tsx')], bundle: true,
    write: false, outdir: 'fixture-out', format: 'esm', platform: 'browser', jsx: 'automatic', define: { 'process.env.NODE_ENV': '"development"' } });
  const javascript = bundle.outputFiles.find(file => file.path.endsWith('.js')).contents;
  const css = bundle.outputFiles.find(file => file.path.endsWith('.css')).contents;
  server = createServer((request, response) => {
    if (request.url === '/fixture.js') { response.setHeader('content-type', 'text/javascript'); response.end(javascript); }
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

test('agent card keeps the last thought visible when streamed reasoning is collapsed during a tool call', async t => {
  const { page, session } = await fixture(t);
  const card = session.getByRole('region', { name: 'Current turn thought' });
  const update = (action_type, payload = {}) => emit(page, 'one', {
    type: 'progress', payload: { type: 'progress', action_type, turn: 1, ...payload },
  });

  await update('turn_thought', { thought: 'I will inspect the Blender connection.' });
  await update('model_call_start', { step: 2, reasoning_effort: { requested: 'low', parameter: 'thinking_level', value: 'low' } });
  await card.getByText('Waiting for model', { exact: true }).waitFor();
  await card.getByText('Reasoning effort low', { exact: true }).waitFor();
  const thought = card.getByText('I will inspect the Blender connection.', { exact: true });
  assert.equal(await thought.isVisible(), true);
  const disclosure = card.getByRole('button', { name: 'Reasoning summary' });
  const details = card.getByRole('region', { name: 'Reasoning summary' });
  await details.getByText('Waiting for the model to send a reasoning summary.').waitFor();
  await update('model_reasoning_summary', { thought: 'Checking the workspace.\n'.repeat(40) });
  await details.getByText(/Checking the workspace/).waitFor();
  assert.equal(await details.evaluate(el => el.scrollHeight > el.clientHeight && el.clientHeight <= 140), true);
  await disclosure.click();
  await update('model_call_finish');
  await update('tool_start', { step: 5, summary: 'Querying Blender MCP' });
  await card.getByText('Querying Blender MCP', { exact: true }).waitFor();
  assert.equal(await thought.isVisible(), true);
  assert.equal(await disclosure.getAttribute('aria-expanded'), 'false');
  await disclosure.click();
  await details.waitFor();
  assert.equal(await thought.isVisible(), true);
});

test('planner waiting and response phases appear in the agent card and Activity', async t => {
  const { page, session } = await fixture(t);
  await page.clock.install();
  await session.getByRole('button', { name: /^Agent activity/ }).click();
  const activity = session.getByRole('region', { name: 'Artifact agent activity' });
  const card = session.getByRole('region', { name: 'Current turn thought' });
  const update = (action_type, extra = {}) => emit(page, 'one', {
    type: 'progress',
    payload: { type: 'progress', domain: 'planner', action_type, turn: 1, phase: 'Planning', ...extra },
  });

  await update('model_call_start', { summary: 'Planning · Waiting for model' });
  await card.getByText('Planner call 1', { exact: true }).waitFor();
  await activity.locator('.live-activity').getByText('Planning · Waiting for model').waitFor();
  await page.clock.fastForward(6000);
  await card.getByText(/Waiting \d+s for the first model update/).waitFor();
  await activity.locator('.live-activity').getByText(/Waiting \d+s for the first model update/).waitFor();
  await update('model_reasoning_summary', { thought: 'Ordering the goals.' });
  await card.getByRole('region', { name: 'Reasoning summary' }).getByText('Ordering the goals.').waitFor();
  await page.clock.fastForward(6000);
  await card.getByText('Planning · Waiting for planner response').waitFor();
  await activity.locator('.live-activity').getByText(/Last model update \d+s ago/).waitFor();
  await update('model_output_progress', { output_chars: 37 });
  await card.getByText('Planning · Receiving planner response').waitFor();
  await card.getByText('37 characters received').waitFor();
  await update('model_output_progress', { output_chars: 120 });
  await activity.locator('.live-activity').getByText('120 characters received').waitFor();
  await page.clock.fastForward(6000);
  await card.getByText('Planning · Waiting for more response text').waitFor();
  await card.getByText(/120 characters received · Last model update \d+s ago/).waitFor();
  await update('model_call_finish', { summary: 'Planning · Model response received' });
  await update('plan_presented', { domain: 'plan', summary: 'Plan ready · 3 goals' });
  await activity.getByText('Plan ready · 3 goals').waitFor();
  assert.equal(await activity.locator('.live-activity').count(), 0);
  await update('goal_handoff', { domain: 'plan', turn: undefined, summary: 'Carrying prior goal results into Rebuild.' });
  await card.getByText('Carrying prior goal results into Rebuild.').waitFor();
  await activity.locator('.live-activity').getByText('Carrying prior goal results into Rebuild.').waitFor();
});

test('draft strategy streams separately from reasoning and stays visible during tool dispatch', async t => {
  const { page, session } = await fixture(t);
  await page.clock.install();
  const card = session.getByRole('region', { name: 'Current turn thought' });
  const update = (action_type, extra = {}) => emit(page, 'one', {
    type: 'progress',
    payload: { type: 'progress', domain: 'plan', action_type, turn: 2, ...extra },
  });

  await update('model_call_start', { step: 1 });
  await card.getByText('Draft strategy', { exact: true }).click();
  const draft = card.getByRole('region', { name: 'Draft strategy' });
  const reasoning = card.getByRole('region', { name: 'Reasoning summary' });
  await draft.getByText('Waiting for command tags.').waitFor();
  await update('model_reasoning_summary', { thought: 'Inspecting the workspace.' });
  await update('model_output_progress', { output_chars: 60, draft_strategy: ['>>th:', '>>pl:', 's1: read-line-range'] });
  await draft.getByText('s1: read-line-range', { exact: false }).waitFor();
  assert.equal(await reasoning.getByText('Inspecting the workspace.').isVisible(), true);
  assert.equal(await card.getByText('Running tool').count(), 0);
  await update('model_output_progress', { output_chars: 120, draft_strategy: ['s1 -> s2: mcp'] });
  await draft.getByText('s1 -> s2: mcp', { exact: false }).waitFor();
  assert.deepEqual(await draft.locator('li code').allTextContents(), ['>>th:', '>>pl:', 's1: read-line-range', 's1 -> s2: mcp']);
  await page.clock.fastForward(6000);
  await card.getByText('Waiting for model to finish response').waitFor();
  await update('model_response_received');
  await card.getByText('Checking commands before dispatch').waitFor();
  await update('model_call_finish', { ok: false, summary: 'Response rejected' });
  await update('model_call_start', { step: 6, phase: 'Retrying after malformed tool call' });
  await card.getByText(/Retrying after malformed tool call/).waitFor();
  assert.equal(await card.getByText('s1: read-line-range', { exact: false }).count(), 0);
  await update('model_output_progress', { output_chars: 37, draft_strategy: ['s1: mcp'] });
  await update('model_call_finish', { ok: true });
  await update('tool_start', { summary: 'Querying Blender MCP' });
  await card.getByText('Querying Blender MCP', { exact: true }).waitFor();
  const preview = card.locator('.turn-thought-draft');
  if (await preview.getAttribute('open') === null) await preview.locator('summary').click();
  await draft.getByText('s1: mcp', { exact: true }).waitFor();
});

test('long activity fits its panel and both open streams can scroll into the conversation', async t => {
  const { page, session } = await fixture(t);
  await page.setViewportSize({ width: 1060, height: 560 });
  const panel = session.locator('.agent-panel');
  const conversation = panel.getByRole('region', { name: 'Agent conversation' });
  const card = panel.getByRole('region', { name: 'Current turn thought' });
  const update = (action_type, extra = {}) => emit(page, 'one', {
    type: 'progress', payload: { type: 'progress', domain: 'plan', action_type, turn: 9, ...extra },
  });
  const file = 'src/motion/' + 'long-directory-name/'.repeat(5) + 'persistence.ts';
  await emit(page, 'one', { type: 'status', status: 'running' });
  await emit(page, 'one', { type: 'state', state: {
    planner: { executing: true, executing_goal_index: 2, executing_goal_count: 3, executing_goal_title: 'Finalize ' + file },
    transcript: [{ role: 'assistant', content: 'Read the current project requirements.' }],
  } });
  await update('turn_thought', { thought: `Inspect ${file} before running the typecheck. `.repeat(7) });
  await update('model_call_start');
  await update('model_reasoning_summary', { thought: 'Checking the persistence layer before touching the public API.\n'.repeat(30) });
  const entries = Array.from({ length: 24 }, (_, i) => `s${i + 1}: read-line-range`);
  await update('model_output_progress', { output_chars: 27115, draft_strategy: entries });
  await emit(page, 'one', { type: 'stderr', message: 'Transient model failure (HTTP 429). Waiting 60s before retrying ' + file });

  const reasoning = card.getByRole('region', { name: 'Reasoning summary' });
  await card.locator('.turn-thought-draft > summary').click();
  const draft = card.getByRole('region', { name: 'Draft strategy' });
  await draft.waitFor();
  assert.equal(await card.getByRole('button', { name: 'Reasoning summary' }).getAttribute('aria-expanded'), 'true');
  for (const region of [reasoning, draft]) {
    assert.ok(await region.evaluate(el => el.clientHeight <= 140 && el.scrollHeight > el.clientHeight));
  }
  assert.ok(await panel.evaluate(el => {
    const boundary = el.getBoundingClientRect();
    const children = el.querySelectorAll('.agent-main, .turn-thought, .agent-notice, .prompt-box, .send-button');
    return [...children].every(child => {
      const { left, right } = child.getBoundingClientRect();
      return left >= boundary.left && right <= boundary.right;
    });
  }));
  assert.ok(await conversation.evaluate(el => el.scrollHeight > el.clientHeight && el.scrollWidth <= el.clientWidth + 1));
  await reasoning.scrollIntoViewIfNeeded();
  await reasoning.evaluate(el => { el.scrollTop = 0; });
  await reasoning.hover();
  await page.mouse.wheel(0, 80);
  await page.waitForFunction(() => {
    const el = document.querySelector('.artifact-session:not([hidden]) [aria-label="Reasoning summary"].turn-thought-copy');
    return el.scrollTop > 0;
  });
  // The conversation has its own scroll track beside the independently
  // scrollable streams, even when a wheel gesture starts inside a stream.
  await conversation.hover({ position: { x: 3, y: 12 } });
  const beforeChain = await conversation.evaluate(el => el.scrollTop);
  assert.ok(await conversation.evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop > 5));
  await page.mouse.wheel(0, 250);
  await page.waitForFunction(before => document.querySelector('.artifact-session:not([hidden]) .agent-main').scrollTop > before, beforeChain);

  await draft.locator('li').last().scrollIntoViewIfNeeded();
  assert.equal(await draft.locator('li').last().textContent(), 's24: read-line-range');
  assert.ok(await draft.evaluate(el => el.scrollTop > 0));
  await conversation.evaluate(el => el.scrollTop === 0 || new Promise(resolve => {
    el.addEventListener('scroll', () => resolve(true), { once: true });
    el.scrollTop = 0;
  }));
  await update('model_output_progress', { output_chars: 27118, draft_strategy: ['s25: mcp'] });
  assert.equal(await conversation.evaluate(el => el.scrollTop), 0);
  assert.equal(await panel.getByRole('button', { name: 'Send instruction' }).isVisible(), true);

  if (process.env.SKILLZ_AGENT_CARD_SNAPSHOT) {
    await page.setViewportSize({ width: 1340, height: 940 });
    await card.scrollIntoViewIfNeeded();
    await panel.screenshot({ path: process.env.SKILLZ_AGENT_CARD_SNAPSHOT });
  }
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
