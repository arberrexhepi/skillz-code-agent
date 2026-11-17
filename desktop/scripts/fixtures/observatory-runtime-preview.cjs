// Deterministic browser fixture; no Docker worker or provider/model calls.
// Build the artifact, then: node desktop/scripts/fixtures/observatory-runtime-preview.cjs
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const dist = path.resolve(__dirname, '../../prebuilt-artifacts/beta-observatory/dist');
const selection = { provider: 'fixture', model: 'deterministic', backendScript: 'main_v2.py' };
const repositories = [{ id: 'repo', label: 'Observatory artifact', access: 'write' }, { id: 'project', label: 'Shared frontend', access: 'read' }, { id: 'worker', label: 'Agent worker', access: 'write' }];
const runs = new Map();
let active, serial = 0, phase = 0;
const summary = run => ({ ...run, events: undefined, state: undefined, eventCount: run.events.length });
function event(type, data) { active.events.push({ seq: active.events.length + 1, at: Date.now() - Date.parse(active.startedAt), type, data }); }
function pause(planner, message) {
  active.status = 'awaiting-input'; active.state = { planner, transcript: [] }; active.message = message;
  event('response', { status: active.status, message });
}
function actions(...items) { return items.map(([type, label]) => ({ type, label, requires_confirmation: true })); }
function decision() {
  if (phase === 0) pause({ pending_discovery: { reason: 'Choose how deeply to inspect the shared repository before planning.' }, suggested_next_actions: actions(['discovery_quick', 'Quick discovery'], ['discovery_moderate', 'Moderate discovery'], ['discovery_deep', 'Deep discovery'], ['skip_discovery', 'Skip discovery']) }, 'Discovery needs your choice.');
  else if (phase === 1) pause({ pending_discovery_extension: { reason: 'One entry point needs a closer look.', findings: 'The frontend and worker have separate entry points.', additional_turns: 2 }, suggested_next_actions: actions(['approve_discovery_extension', 'Allow 2 more turns'], ['decline_discovery_extension', 'Continue with findings']) }, 'Approve or decline the bounded extension.');
  else if (phase === 2) pause({ pending_plan: { summary: 'Explain the repository entry points and verify the findings.', goals: [{ goal_id: 'entry', title: 'Inspect the frontend and worker entry points', success_signals: ['Both entry points are identified with source evidence.'] }, { goal_id: 'verify', title: 'Check the explanation against source', success_signals: ['No unsupported execution claims.'] }] }, suggested_next_actions: actions(['approve_plan', 'Approve plan'], ['reject_plan', 'Revise plan']) }, 'Please review this plan before execution.');
  else if (phase === 3) pause({ suggested_next_actions: actions(['retry_failed_goal', 'Retry failed goal'], ['resume_execution', 'Continue execution']) }, 'The verification goal failed. Choose whether to retry or continue.');
  else { active.status = 'completed'; active.state = { planner: { status: 'completed' }, transcript: [] }; active.message = 'Fixture run completed after explicit decisions.'; event('response', { status: 'completed', message: active.message }); active.endedAt = new Date().toISOString(); active = undefined; }
}
function execute() {
  const run = active; run.status = 'running'; run.state = { planner: {}, transcript: [] };
  let count = 0;
  const timer = setInterval(() => {
    if (active !== run || run.status !== 'running') { clearInterval(timer); return; }
    const action = phase === 1 ? 'discovery_read' : phase === 3 ? 'run_check' : 'model_call_finish';
    event('progress', { action_type: action, ok: !(phase === 3 && count === 3), summary: `Fixture phase ${phase + 1}, event ${count + 1}` });
    if (++count === 4) { clearInterval(timer); decision(); }
  }, 700);
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const json = (data, code = 200) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(data)); };
  try {
    if (url.pathname === '/runtime/v1/runtime') return json({ available: true, ready: true, repositories, selection, busy: active?.status === 'running', activeRunId: active?.id, runs: [...runs.values()].map(summary).reverse() });
    const match = url.pathname.match(/^\/runtime\/v1\/runs\/([^/]+)(\/continue)?$/);
    if (match && req.method === 'GET') {
      const run = runs.get(match[1]); if (!run) return json({ error: 'Recording not found' }, 404);
      return json({ ...run, eventCount: run.events.length, events: run.events.filter(e => e.seq > Number(url.searchParams.get('after') || 0)) });
    }
    if (req.method === 'POST' && url.pathname.startsWith('/runtime/v1/')) {
      let text = ''; for await (const chunk of req) text += chunk;
      const body = JSON.parse(text || '{}');
      if (url.pathname === '/runtime/v1/runs') {
        if (active) return json({ error: 'A run is already active' }, 409);
        const repository = repositories.find(item => item.id === (body.repositoryId || 'repo'));
        if (!repository) return json({ error: 'Repository is not granted' }, 400);
        active = { repository, id: 'fixture-' + ++serial, prompt: body.text, startedAt: new Date().toISOString(), status: 'running', selection, events: [] };
        runs.set(active.id, active); phase = 0; event('input', { message: body.text }); execute(); return json({ id: active.id });
      }
      if (match?.[2]) {
        if (active?.id !== match[1] || active.status !== 'awaiting-input') return json({ error: 'Not awaiting input' }, 409);
        const offered = active.state.planner.suggested_next_actions;
        if (body.action && !offered.some(a => a.type === body.action)) return json({ error: 'Action is not offered' }, 400);
        console.log('Explicit decision:', body.action || 'text reply');
        event('input', { action: body.action, message: body.text || body.action });
        if (body.action) phase++; execute(); return json({ id: active.id });
      }
      if (url.pathname === '/runtime/v1/stop') {
        if (active) { active.status = 'interrupted'; active.endedAt = new Date().toISOString(); event('interrupted', { message: 'Stopped explicitly' }); active = undefined; }
        return json({ ok: true });
      }
      return json({ ok: true });
    }
    const file = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    if (!file.startsWith(dist + path.sep)) return json({ error: 'Invalid path' }, 400);
    const data = await fs.readFile(file);
    res.writeHead(200, { 'content-type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' }); res.end(data);
  } catch (error) { json({ error: error.message }, 500); }
});
server.listen(5183, '127.0.0.1', () => console.log('Agent Observatory fixture: http://127.0.0.1:5183'));
