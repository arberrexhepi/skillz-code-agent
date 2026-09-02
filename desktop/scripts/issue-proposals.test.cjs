const assert = require('node:assert/strict');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const loadTypeScript = require('./load-ts.cjs');
const { AgentSuggestions, decideIssueProposal } = loadTypeScript(() => ({
  ...require('../src/renderer/src/components/AgentSuggestions.tsx'),
  ...require('../src/shared/issueProposals.ts'),
}));
const proposal = { proposal_id: 'proposal-1', summary: 'Legacy type error', reason: 'Outside this goal', evidence: '<script>not executable</script>', paths: ['legacy.ts:4'], parent_issue_id: 'issue-1' };

test('suggestions stay actionable during execution and describe immediate deferral', () => {
  const html = renderToStaticMarkup(React.createElement(AgentSuggestions, { proposals: [proposal], busy: true, onDecide() {} }));
  assert.match(html, /Already deferred/);
  assert.match(html, /Agent-authored/);
  assert.match(html, />Accept</);
  assert.match(html, />Ignore</);
  assert.doesNotMatch(html, /disabled|<script>/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /legacy.ts:4/);
});

test('empty inbox disappears; storage errors remain visible', () => {
  const render = (error) => renderToStaticMarkup(React.createElement(AgentSuggestions, { proposals: [], error, busy: false, onDecide() {} }));
  assert.equal(render(), '');
  assert.match(render('Storage unavailable'), /role="alert"/);
});

test('each decision uses its own bridge command and waits for acknowledgment', async () => {
  for (const decision of ['accept', 'ignore']) {
    let acknowledge;
    const requests = [];
    let completed = false;
    const pending = decideIssueProposal({ plannerAction: (action, extras) => {
      requests.push({ action, extras });
      return new Promise((resolve) => { acknowledge = resolve; });
    } }, 'proposal-1', decision).then((result) => { completed = true; return result; });
    assert.deepEqual(requests, [{ action: `${decision}_issue_proposal`, extras: { proposal_id: 'proposal-1' } }]);
    await Promise.resolve();
    assert.equal(completed, false);
    acknowledge({ ok: true, state: { planner: {}, transcript: [] } });
    assert.equal((await pending).ok, true);
  }
});

test('failed persistence rejects without a success acknowledgment', async () => {
  await assert.rejects(decideIssueProposal({ plannerAction: async () => ({ ok: false, message: 'Disk full' }) }, 'proposal-1', 'accept'), /Disk full/);
  await assert.rejects(decideIssueProposal({ plannerAction: () => assert.fail('invalid request sent') }, '', 'ignore'), /Missing suggestion id/);
});
