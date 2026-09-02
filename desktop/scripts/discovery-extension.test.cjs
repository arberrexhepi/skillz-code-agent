const assert = require('node:assert/strict');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const loadTypeScript = require('./load-ts.cjs');
const { AgentDecisionCard, AgentWorkspaceContext, initialAgentUiState, extension } = loadTypeScript(() => ({
  ...require('../src/renderer/src/components/agent/AgentDecisionCard.tsx'),
  ...require('../src/renderer/src/agent/agentWorkspace.ts'),
  ...require('../src/shared/agentCore.ts'),
  ...require('./fixtures/discovery-extension.ts'),
}));
const render = (pending = extension, pendingAction = '') => renderToStaticMarkup(React.createElement(AgentWorkspaceContext.Provider, {
  value: { state: { ...initialAgentUiState, pendingAction, bridge: { planner: { pending_discovery_extension: pending }, transcript: [] } } },
}, React.createElement(AgentDecisionCard)));

test('extension displays cost, rationale, proposal, findings, and unresolved questions', () => {
  const html = render();
  for (const text of [extension.reason, extension.proposal, ...extension.ambiguities, '8/8 turns', '6/6 tool actions', 'Allow 2 more turns', 'Plan with current findings', 'Findings so far', 'kërkesë.ts']) assert.ok(html.includes(text), text);
});

test('busy decision disables both actions and disappears when no request is pending', () => {
  const html = render(extension, 'approve_discovery_extension');
  assert.equal((html.match(/disabled=""/g) || []).length, 2);
  assert.doesNotMatch(render(null), /Continue discovery|Allow 2 more turns/);
});

test('model proposal is plain text, never executable markup', () => {
  const html = render({ ...extension, proposal: '<script>alert(1)</script>' });
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});
