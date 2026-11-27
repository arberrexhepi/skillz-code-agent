const assert = require('node:assert/strict');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const loadTypeScript = require('./load-ts.cjs');
const { PlanDetails, PlanDecisionCard, AgentWorkspaceContext, initialAgentUiState, reviewPlan, pausedVerification } = loadTypeScript(() => ({
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
test('paused plans put Resume next to the full plan review', () => {
  const html = render({ execution_paused: true, paused_plan: reviewPlan, resume_checkpoint: { next_goal_index: 4 } });
  assert.match(html, /Execution paused/);
  assert.match(html, /Review full plan/);
  assert.match(html, /Resume at goal 4/);
  assert.doesNotMatch(html, /Suggest plan changes|Validate regression coverage/);
});
test('no stale decision is shown without a pending or paused plan', () => {
  const html = render({});
  assert.doesNotMatch(html, /Approve plan|Review full plan|Suggest plan changes/);
});
test('paused plan shows the actual stop reason and validation blocker before review', () => {
  const html = render({ execution_paused: true, paused_plan: reviewPlan,
    paused_failed_goal_id: 'goal-2', paused_failure_reason: 'Turn limit reached: 100 model turns in this attempt.',
    last_completed_results: [{ goal_id: 'goal-2', status: 'failed', validation_summary: 'An edit batch is still open.' }],
    resume_checkpoint: { next_goal_index: 2, completed_goal_count: 1 } });
  assert.match(html, /Stop reason/);
  assert.match(html, /100 model turns in this attempt/);
  assert.match(html, /An edit batch is still open/);
  assert.match(html, /Completed goals are saved/);
});

test('an exhausted goal has a suspended status and a direct resume action', () => {
  const html = render({ execution_paused: true, paused_plan: reviewPlan, paused_suspended_goal_id: 'goal-2',
    last_completed_results: [{ goal_id: 'goal-2', status: 'suspended', title: 'Export scene', final_message: 'Read allowance exhausted.' }],
    resume_checkpoint: { next_goal_index: 2, mode: 'resume_suspended_goal' } });
  assert.match(html, /Suspended goal.*Export scene/);
  assert.match(html, /Resume at goal 2/);
  assert.doesNotMatch(html, /Failed goal|Retry/);
});

test('verification pauses display a checklist with results, evidence, and the exact stopping limits', () => {
  const html = render(pausedVerification);
  assert.match(html, /1 of 5 passed/);
  for (const [status, label] of [['pass', 'Passed'], ['fail', 'Failed'], ['blocked', 'Blocked'], ['unverified', 'Unverified'], ['not_run', 'Not run']]) {
    assert.match(html, new RegExp(`data-status="${status}">${label}<`));
  }
  assert.match(html, /12\/12 turns/);
  assert.match(html, /Both halves started at frame 0/);
  assert.match(html, /The same failure was observed again/);
  assert.match(html, /The check used all 3 attempts/);
  assert.match(html, /Verification next steps/);
  assert.match(html, /Confirm the selected clip and retest the split boundary/);
  assert.match(html, /Proceed with next steps/);
  assert.match(html, /Reused evidence/);
  assert.match(html, /Verified by<\/strong> Implementation goal/);
  assert.doesNotMatch(html, /Stop reason|1\/5 required checks passed/);
});

test('MCP-backed checks display checkpoint decisions and the last observation', () => {
  const result = pausedVerification.last_completed_results[0];
  const html = render({ ...pausedVerification, last_completed_results: [{
    ...result, verification: {
      ...result.verification,
      checks: [{ id: 'check-2', requirement: 'Verify the viewport', status: 'unverified', attempts: 5, mcp_backed: true,
        mcp_attempts: 5, mcp_attempt_checkpoint: 6,
        evidence: 'The workflow is incomplete.', progress_evidence: 'Opened the editor.' }],
      mcp_session: { decisions: [{ turn: 12, calls: 18, decision: 'continue', reason: 'New verification evidence earned another window.',
        evidence: ['check-2 — Opened the editor.'] }] },
    },
  }] });
  assert.match(html, /5 of 6 MCP attempts/);
  assert.doesNotMatch(html, /5 of 3 attempts/);
  assert.match(html, /Progress checkpoints/);
  assert.match(html, /Turn 12 · 18 calls/);
  assert.match(html, /New verification evidence earned another window/);
  assert.match(html, /Last progress.*Opened the editor/);
});

test('older missing verdicts appear as unverified and retain their original report', () => {
  const html = render({ ...pausedVerification, paused_failure_reason: 'Original text for a saved run.',
    last_completed_results: [{ goal_id: 'g5', status: 'failed', verification: { checks: [
      { id: 'check-2', requirement: 'Clip boundaries are respected', status: 'fail', attempts: 2, evidence: 'The tool returned no usable verification verdict for check-2.' },
      { id: 'check-3', requirement: 'Undo reverts edits', status: 'blocked', attempts: 0, evidence: 'No verdict was recorded.' },
    ] } }],
  });
  assert.match(html, /data-status="unverified">Unverified/);
  assert.match(html, /data-status="not_run">Not run/);
  assert.match(html, /Original run report.*Original text for a saved run/);
  assert.doesNotMatch(html, /data-status="fail"/);
});
