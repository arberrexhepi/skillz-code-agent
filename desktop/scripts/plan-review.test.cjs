const assert = require('node:assert/strict');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const loadTypeScript = require('./load-ts.cjs');
const { PlanDetails, PlanDecisionCard, AgentWorkspaceContext, initialAgentUiState, reviewPlan } = loadTypeScript(() => ({
  ...require('../src/renderer/src/components/agent/PlanDetails.tsx'),
  ...require('../src/renderer/src/components/agent/PlanDecisionCard.tsx'),
  ...require('../src/renderer/src/agent/agentWorkspace.ts'),
  ...require('../src/shared/agentCore.ts'),
  ...require('./fixtures/plan-review.ts'),
}));
const render = (planner = { pending_plan: reviewPlan }, pendingAction = '') => renderToStaticMarkup(React.createElement(AgentWorkspaceContext.Provider, {
  value: { state: { ...initialAgentUiState, pendingAction, bridge: { planner, transcript: [] } } },
}, React.createElement(PlanDecisionCard)));

test('full reading view includes the entire summary and all five detailed goals', () => {
  const html = renderToStaticMarkup(React.createElement(PlanDetails, { plan: reviewPlan }));
  assert.ok(html.includes(reviewPlan.summary));
  for (const goal of reviewPlan.goals) {
    assert.ok(html.includes(goal.title));
    assert.ok(html.includes(goal.goal));
    assert.ok(html.includes(goal.success_signals[0]));
  }
});
test('inline card is only a short preview and opens review before approval', () => {
  const html = render();
  assert.match(html, /plan-preview-summary/);
  assert.match(html, /5 goals/);
  assert.match(html, /Review full plan/);
  assert.match(html, /Suggest plan changes/);
  assert.doesNotMatch(html, /Approve plan|>Reject<|Clarified scope|Success criteria|plan-details/);
});
test('review includes scope, assumptions, exclusions, dependencies, and implementation notes', () => {
  const html = renderToStaticMarkup(React.createElement(PlanDetails, { plan: reviewPlan }));
  for (const text of [reviewPlan.original_request, reviewPlan.clarification_summary, ...reviewPlan.assumptions, ...reviewPlan.not_in_scope, ...reviewPlan.next_steps_preview, ...reviewPlan.dependency_repairs, reviewPlan.confirmation_prompt, ...reviewPlan.goals[4].delegation_notes]) assert.ok(html.includes(text), text);
  assert.match(html, /Depends on/);
  assert.match(html, /Preserve previous context/);
});
test('plain plan text is escaped rather than treated as HTML', () => {
  const html = renderToStaticMarkup(React.createElement(PlanDetails, { plan: { summary: '<script>alert(1)</script>', goals: [] } }));
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});
test('approval and change controls are disabled during another action or execution', () => {
  for (const html of [render(undefined, 'submit'), render({ pending_plan: reviewPlan, executing: true }), render({ pending_plan: reviewPlan, continuous_mode: { enabled: true, status: 'running' } })]) {
    assert.match(html, /disabled=""[^>]*>Suggest plan changes/);
  }
});
test('paused plans retain a compact preview with review and revision controls', () => {
  const html = render({ execution_paused: true, paused_plan: reviewPlan, resume_checkpoint: { next_goal_index: 4 } });
  assert.match(html, /Execution paused/);
  assert.match(html, /Review full plan/);
  assert.match(html, /Suggest plan changes/);
  assert.doesNotMatch(html, /Validate regression coverage/);
});
test('no stale decision is shown without a pending or paused plan', () => {
  const html = render({});
  assert.doesNotMatch(html, /Approve plan|Review full plan|Suggest plan changes/);
});
