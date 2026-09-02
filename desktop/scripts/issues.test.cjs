const assert = require('node:assert/strict');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const loadTypeScript = require('./load-ts.cjs');
const { IssueCreateForm, createInactiveIssue, issueState } = loadTypeScript(() => ({
  ...require('../src/renderer/src/components/IssueCreateForm.tsx'),
  ...require('../src/shared/issueCreation.ts'),
  ...require('../src/shared/agentCore.ts'),
}));

const props = { summary: 'A separate issue', creating: false, executionBusy: true, error: '', onChange: () => {}, onCreate: () => {} };

test('add button stays enabled while a different issue is executing', () => {
  const html = renderToStaticMarkup(React.createElement(IssueCreateForm, props));
  assert.match(html, /type="submit"/);
  assert.match(html, /aria-label="Add issue"/);
  assert.doesNotMatch(html, /disabled/);
  let submits = 0;
  let prevented = false;
  const view = IssueCreateForm({ ...props, onCreate: () => { submits += 1; } });
  const form = React.Children.toArray(view.props.children).find((child) => child.type === 'form');
  form.props.onSubmit({ preventDefault: () => { prevented = true; } });
  assert.equal(submits, 1);
  assert.equal(prevented, true);
});

test('queued creation is explicit and prevents duplicate submission', () => {
  const pending = { ...props, creating: true };
  const html = renderToStaticMarkup(React.createElement(IssueCreateForm, pending));
  assert.match(html, /Queued for creation/);
  assert.match(html, /Waiting for the current agent action/);
  assert.match(html, /disabled/);
  const view = IssueCreateForm({ ...pending, onCreate: () => assert.fail('Duplicate submission') });
  React.Children.toArray(view.props.children).find((child) => child.type === 'form').props.onSubmit({ preventDefault() {} });
});

test('creation sends activate false, waits for acknowledgment, and projects the new open issue', async () => {
  let acknowledge;
  const response = new Promise((resolve) => { acknowledge = resolve; });
  const requests = [];
  const pending = createInactiveIssue({ plannerAction: (action, extras) => {
    requests.push({ action, extras });
    return response;
  } }, '  Another issue  ');
  assert.deepEqual(requests, [{ action: 'create_issue', extras: { summary: 'Another issue', activate: false } }]);
  let done = false;
  pending.then(() => { done = true; });
  await Promise.resolve();
  assert.equal(done, false);
  const active = { issue_id: 'issue-001', status: 'open', request_summary: 'Current work' };
  const added = { issue_id: 'issue-002', status: 'open', request_summary: 'Another issue' };
  acknowledge({ ok: true, state: { planner: { issue_state: { active_issue: active, issues: [active, added] } }, transcript: [] } });
  const result = await pending;
  assert.deepEqual(issueState(result.state).active, active);
  assert.deepEqual(issueState(result.state).open, [added]);
});

test('failed creation keeps details visible with a local error', async () => {
  await assert.rejects(createInactiveIssue({ plannerAction: async () => ({ ok: false, message: 'Disk is read-only' }) }, props.summary), /Disk is read-only/);
  const html = renderToStaticMarkup(React.createElement(IssueCreateForm, { ...props, error: 'Disk is read-only' }));
  assert.match(html, /value="A separate issue"/);
  assert.match(html, /role="alert"/);
  assert.match(html, /Disk is read-only/);
});

test('empty input is rejected without a bridge request', async () => {
  await assert.rejects(createInactiveIssue({ plannerAction: () => assert.fail('Empty request') }, '  '), /Enter issue details/);
});
