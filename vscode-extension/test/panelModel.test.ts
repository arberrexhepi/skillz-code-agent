import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildPlannerActionMessage,
  buildReviewReport,
  combineSuggestedActions,
  groupLatestDiagnosticsByPath,
  progressTimelineTarget,
  primaryPathForReview,
  type BridgeState,
} from '../src/panelModel';

test('combineSuggestedActions preserves planner then worker order with source tags', () => {
  const state: BridgeState = {
    planner: {
      suggested_next_actions: [{ type: 'approve_plan', label: 'Approve Plan' }],
      worker_state: {
        suggested_next_actions: [{ type: 'git_diff', path: 'main.py', label: 'Diff main.py' }],
      },
    },
    transcript: [],
  };

  const actions = combineSuggestedActions(state);
  assert.equal(actions.length, 2);
  assert.equal(actions[0].source, 'planner');
  assert.equal(actions[0].type, 'approve_plan');
  assert.equal(actions[1].source, 'worker');
  assert.equal(actions[1].type, 'git_diff');
});

test('groupLatestDiagnosticsByPath falls back to latest diagnostics path', () => {
  const state: BridgeState = {
    planner: {
      worker_state: {
        latest_diagnostics: {
          path: 'src/example.ts',
          diagnostics: [
            { line: 5, column: 2, message: 'Missing export' },
            { path: 'src/other.ts', line: 3, message: 'Name error' },
          ],
        },
      },
    },
    transcript: [],
  };

  const grouped = groupLatestDiagnosticsByPath(state);
  assert.deepEqual(Object.keys(grouped).sort(), ['src/example.ts', 'src/other.ts']);
  assert.equal(grouped['src/example.ts'][0].message, 'Missing export');
  assert.equal(grouped['src/other.ts'][0].message, 'Name error');
});

test('buildReviewReport renders review_changes as json and diff actions as diff text', () => {
  const reviewJson = buildReviewReport({ action_type: 'review_changes', files: [{ path: 'main.py', risk: 'high' }] });
  assert.ok(reviewJson);
  assert.equal(reviewJson?.language, 'json');
  assert.match(reviewJson?.content || '', /main\.py/);

  const reviewDiff = buildReviewReport({ action_type: 'git_diff', stat: ' main.py | 2 ++', diff: '@@ -1 +1 @@' });
  assert.ok(reviewDiff);
  assert.equal(reviewDiff?.language, 'diff');
  assert.match(reviewDiff?.content || '', /main\.py/);
  assert.match(reviewDiff?.content || '', /@@ -1 \+1 @@/);
});

test('primaryPathForReview prefers explicit path then file entries', () => {
  assert.equal(primaryPathForReview({ path: 'main.py' }), 'main.py');
  assert.equal(primaryPathForReview({ files: ['planner.py'] }), 'planner.py');
  assert.equal(primaryPathForReview({ files: [{ path: 'readme.md' }] }), 'readme.md');
  assert.equal(primaryPathForReview({ files: [] }), undefined);
});

test('buildPlannerActionMessage forwards issue_id for reopen actions', () => {
  const message = buildPlannerActionMessage({
    type: 'reopen_issue',
    issue_id: 'issue-003',
    label: 'Reopen issue-003',
    source: 'planner',
  });

  assert.equal(message.action, 'reopen_issue');
  assert.deepEqual(message.payload, { issue_id: 'issue-003' });
});

test('buildPlannerActionMessage forwards issue_id for close actions', () => {
  const message = buildPlannerActionMessage({
    type: 'close_issue',
    issue_id: 'issue-003',
    label: 'Close issue-003',
    source: 'planner',
  });

  assert.equal(message.action, 'close_issue');
  assert.deepEqual(message.payload, { issue_id: 'issue-003' });
});

test('buildPlannerActionMessage preserves discovery mode payloads', () => {
  const message = buildPlannerActionMessage({
    type: 'select_discovery_mode',
    mode: 'moderate',
    label: 'Moderate Scan',
    source: 'planner',
  });

  assert.equal(message.action, 'select_discovery_mode');
  assert.equal(message.mode, 'moderate');
  assert.deepEqual(message.payload, { mode: 'moderate' });
});

test('progressTimelineTarget preserves discovery routing while action remains pending', () => {
  assert.equal(progressTimelineTarget(undefined, 'select_discovery_mode'), 'discovery');
});

test('progressTimelineTarget prefers execution routing when execution has started', () => {
  assert.equal(progressTimelineTarget({ executing: true }, 'select_discovery_mode'), 'plan');
});

test('progressTimelineTarget uses discovery state from planner snapshots', () => {
  assert.equal(progressTimelineTarget({ pending_discovery: { reason: 'Need repo scan' } }, ''), 'discovery');
});

test('progressTimelineTarget still routes discovery when a prior discovery result exists', () => {
  assert.equal(
    progressTimelineTarget({ pending_discovery: { reason: 'Need repo scan' }, last_discovery: { final_message: 'Earlier discovery' } }, ''),
    'discovery',
  );
});
