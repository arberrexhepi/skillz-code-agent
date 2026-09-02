const assert = require('node:assert/strict');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const loadTypeScript = require('./load-ts.cjs');
const { conversationTimeline, latestTurnThought, reduceAgentUi, initialAgentUiState, WorkflowReportCard, TurnThought } = loadTypeScript(() => ({
  ...require('../src/shared/agentTimeline.ts'),
  ...require('../src/shared/agentCore.ts'),
  ...require('../src/renderer/src/components/agent/WorkflowReportCard.tsx'),
  ...require('../src/renderer/src/components/agent/TurnThought.tsx'),
}));

const message = (role, content) => ({ role, content });
const report = (category, status, extras = {}) => ({ kind: 'workflow', category, status, title: category === 'plan' ? 'Goal plan' : 'Discovery', content: `${category} ${status}`, ...extras });
const annotated = (role, ...parts) => ({ role, content: parts.map((part) => part.content).join('\n\n'), presentation: parts });
const state = (transcript, planner = {}) => ({ transcript, planner });
const offer = 'Discovery Suggested\n- Reason: Locate the sidebar\n- Prompt: Inspect\n\nChoose a Discovery Depth\n1. Quick Scan\n2. Moderate Scan\n3. Deep Scan\n\nResponse Options\n- Reply with 1, 2, or 3 to run discovery.';
const planText = "Plan Summary\n- Fix the sidebar\n\nGoals\n1. Find the missing project\n\nApproval\n- Reply with 'approve' to execute.";

test('discovery selection and report fold into one card without swallowing conversational tail', () => {
  const input = state([
    message('user', 'Show ten projects'),
    annotated('assistant', report('discovery', 'offered')),
    annotated('user', report('discovery', 'selected', { selection: 'Moderate', content: 'moderate' })),
    annotated('assistant', report('discovery', 'complete', { discovery: { final_message: 'Found the sidebar', mode: 'moderate' } }), { kind: 'message', content: 'The database query also needs a fix.' }, report('plan', 'offered')),
  ]);
  const copy = JSON.stringify(input);
  const items = conversationTimeline(input);
  assert.deepEqual(items.map((item) => item.kind), ['message', 'workflow', 'message', 'workflow']);
  assert.equal(items[1].selection, 'Moderate');
  assert.equal(items[1].events.length, 3);
  assert.equal(items[2].entry.content, 'The database query also needs a fix.');
  assert.equal(JSON.stringify(input), copy);
});

test('historical reports retain their own selections across successive requests', () => {
  const transcript = [];
  for (const mode of ['Moderate', 'Quick', 'Deep']) transcript.push(
    message('user', `Request ${mode}`),
    annotated('assistant', report('discovery', 'offered')),
    annotated('user', report('discovery', 'selected', { selection: mode })),
    annotated('assistant', report('discovery', 'complete', { discovery: { mode, final_message: `Found ${mode}` } })),
  );
  const reports = conversationTimeline(state(transcript, { last_discovery: { mode: 'unrelated' } })).filter((item) => item.kind === 'workflow');
  assert.deepEqual(reports.map((item) => item.selection), ['Moderate', 'Quick', 'Deep']);
  assert.deepEqual(reports.map((item) => item.discovery.final_message), ['Found Moderate', 'Found Quick', 'Found Deep']);
});

test('goal approval and individual outcomes form one report with a separate final reply', () => {
  const plan = { summary: 'Fix sidebar', goals: [{ goal_id: 'a', title: 'Load projects' }, { goal_id: 'b', title: 'Render projects' }] };
  const items = conversationTimeline(state([
    annotated('assistant', report('plan', 'offered', { plan })),
    annotated('user', report('plan', 'selected', { selection: 'Approved', content: 'approve' })),
    annotated('assistant', report('plan', 'complete', { goals: [{ goal_id: 'a', status: 'completed' }] }), report('plan', 'failed', { goals: [{ goal_id: 'b', status: 'failed', final_message: 'Validation failed' }] }), { kind: 'message', content: 'Loading is fixed; rendering still needs attention.' }),
  ]));
  assert.equal(items[0].selection, 'Approved');
  assert.equal(items[0].status, 'failed');
  assert.equal(items[0].goals.length, 2);
  assert.equal(items[1].entry.content, 'Loading is fixed; rendering still needs attention.');
});

test('current goal progress is reflected before the final bridge response', () => {
  const plan = { summary: 'Update sidebar' };
  const items = conversationTimeline(state([annotated('assistant', report('plan', 'offered', { plan }))], {
    pending_plan: plan, executing: true, executing_goal_title: 'Update sidebar', completed_results: [{ status: 'completed', goal_id: 'first' }],
  }));
  assert.equal(items[0].status, 'running');
  assert.equal(items[0].currentGoal, 'Update sidebar');
  assert.equal(items[0].goals.length, 1);
});

test('unrelated active plans do not rewrite a historical report', () => {
  const items = conversationTimeline(state([annotated('assistant', report('plan', 'complete', { plan: { summary: 'Older task' } }))], {
    pending_plan: { summary: 'New task' }, executing: true, executing_goal_title: 'New goal',
  }));
  assert.equal(items[0].status, 'complete');
  assert.equal(items[0].currentGoal, undefined);
  assert.equal(conversationTimeline(state([message('user', 'close the sidebar')]))[0].kind, 'message');
});

test('legacy menus and contextual choices become cards, ordinary short messages stay', () => {
  const items = conversationTimeline(state([
    message('user', 'quick'), message('assistant', offer), message('user', 'moderate'),
    message('assistant', 'Discovery Complete\n- Mode: Moderate Scan\n- Worker result: Found it\n\n' + planText),
    message('user', 'approve'), message('user', 'approve the design but keep the old route'),
  ]));
  assert.equal(items[0].entry.content, 'quick');
  assert.equal(items[1].selection, 'Moderate');
  assert.equal(items[2].selection, 'Approved');
  assert.equal(items[3].entry.content, 'approve the design but keep the old route');
});

test('issue lifecycle commands and acknowledgments are retained in a compact card', () => {
  const items = conversationTimeline(state([message('user', '/close-issue issue-062'), message('assistant', 'Closed issue issue-062. It will stay out of active context until reopened.')]));
  assert.equal(items.length, 1);
  assert.equal(items[0].category, 'issue');
  assert.equal(items[0].events.length, 2);
});

test('a new action clears stale thought without deleting Activity history', () => {
  const thought = { type: 'progress', domain: 'worker', action_type: 'turn_thought', thought: 'Inspect project query', turn: 2 };
  let current = reduceAgentUi(initialAgentUiState, { type: 'progress', progress: thought });
  current = reduceAgentUi(current, { type: 'progress', progress: { type: 'progress', action_type: 'grep', summary: 'Found 3 matches', domain: 'worker' } });
  assert.equal(current.turnThought.thought, thought.thought);
  current = reduceAgentUi(current, { type: 'pending', action: 'submit' });
  assert.equal(current.turnThought, undefined);
  assert.equal(current.activity.length, 2);
});

test('new goal and discovery boundaries clear prior thoughts; tool summaries never replace them', () => {
  const thought = { type: 'progress', domain: 'worker', thought: 'Read the query first' };
  assert.equal(latestTurnThought([thought, { type: 'progress', summary: 'Read 100 lines' }]), thought);
  assert.equal(latestTurnThought([thought, { type: 'goal_start' }]), undefined);
  assert.equal(latestTurnThought([thought, { type: 'progress', action_type: 'discovery_start' }]), undefined);
  assert.equal(latestTurnThought([{ type: 'progress', domain: 'model', thought: 'internal provider payload' }]), undefined);
});

test('cards are collapsed by default and retain selections and full original report', () => {
  const item = conversationTimeline(state([annotated('assistant', report('discovery', 'complete', { selection: 'Quick', content: 'Full original discovery report', discovery: { final_message: 'Found the header' } }))]))[0];
  const view = WorkflowReportCard({ report: item });
  assert.equal(view.type, 'details');
  assert.equal(view.props.open, undefined);
  const summary = React.Children.toArray(view.props.children)[0];
  assert.match(renderToStaticMarkup(summary), /Quick/);
  // Inspect Markdown props without replacing the browser-only sanitizer in Node.
  const contents = [];
  const walk = (node) => React.Children.forEach(node, (child) => {
    if (!React.isValidElement(child)) return;
    if (child.props.content) contents.push(child.props.content);
    walk(child.props.children);
  });
  walk(view);
  assert.ok(contents.includes('Full original discovery report'));
  assert.ok(contents.includes('Found the header'));
});

test('live thought is visible, untruncated, and previous thought is collapsible when idle', () => {
  const thought = { type: 'progress', thought: 'Inspect the code before changing it. '.repeat(20), turn: 4 };
  const live = renderToStaticMarkup(React.createElement(TurnThought, { active: true, action: 'approve_plan', thought }));
  assert.match(live, /aria-label="Current turn thought"/);
  assert.match(live, /Turn 4/);
  assert.ok(live.includes(thought.thought.trim()));
  const idle = renderToStaticMarkup(React.createElement(TurnThought, { active: false, action: '', thought }));
  assert.match(idle, /<details class="turn-thought last-thought">/);
  assert.match(idle, /Last turn thought/);
});
