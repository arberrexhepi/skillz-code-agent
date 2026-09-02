import type { AgentBridgeState } from '../../src/shared/agentTypes';

const active = { issue_id: 'issue-041', status: 'open', request_summary: 'Improve Pro prerequisite states' };
export const proposalsFixture: AgentBridgeState = {
  planner: {
    executing: true,
    issue_state: { active_issue: active, issues: [active] },
    worker_state: {
      issue_proposals: { proposals: [
        { proposal_id: 'proposal-1', status: 'proposed', author: 'agent', summary: 'Legacy text-content runtime diagnostic', reason: 'The failing text-content path is outside the Pro prerequisite change.', evidence: 'Existing run reports “Text content did not match” in LegacyPreview.tsx:42. Focused Pro checks passed.', paths: ['src/components/LegacyPreview.tsx'], parent_issue_id: 'issue-041' },
        { proposal_id: 'proposal-2', status: 'proposed', author: 'agent', summary: 'Missing accessible label in account settings', reason: 'Account settings is outside the current goal.', evidence: 'Existing route check reports an unlabelled input.', paths: ['src/settings/Account.tsx'], parent_issue_id: 'issue-041' },
      ].map((proposal) => ({ ...proposal, status: 'proposed' as const, author: 'agent' as const, goal: 'Improve Pro prerequisite states', created_at: '2026-09-02T12:00:00Z' })) },
      issue_context: { run_diagnostics: [{ issue_id: 'run-legacy', status: 'deferred', summary: 'Text content did not match', file: 'src/components/LegacyPreview.tsx', line: '42' }] },
    },
  },
  transcript: [{ role: 'assistant', content: 'Focused Pro validation passed. I recorded two unrelated findings separately; neither blocks this goal.' }],
};
