import { parseRepoFacts, type RepoFactsSnapshot } from '../../src/shared/repoFacts';

export const repoFactsPayload = {
  schema_version: 2, active_issue_id: 'issue-042', migration: { legacy_flat_list_migrated: false },
  issues: [
    { issue_id: 'global-architecture', status: 'closed', plan_summary: 'Repository conventions', facts: [
      { key: 'text.encoding', value: 'All repository text and subprocess streams use UTF-8. Preserve kërkesë.ts and emoji 🧭.', fact_type: 'architecture', source_action: 'read_file', updated_run_id: 7, updated_step: 3 },
      { key: 'desktop.boundary', value: 'The React renderer uses the typed preload API.\nElectron owns file and process access.', fact_type: 'architecture', source_action: 'repo_map', updated_run_id: 7, updated_step: 5 },
    ] },
    { issue_id: 'issue-042', status: 'open', request_summary: 'Render repository facts in the desktop', plan_summary: 'Expose durable knowledge in a Repo Facts tab', opened_at: '2026-09-02T12:00:00Z', source: 'manual', priority: 60, parent_issue_id: 'issue-039', lifecycle_notes: ['Discovery identified the persisted schema.'], facts: [
      { key: 'facts.storage', value: 'issue_facts.py writes a JSON ledger inside repo_facts.md. It contains facts, issue metadata, and completed goals.', fact_type: 'architecture', source_action: 'read_file', updated_run_id: 12, updated_step: 4 },
      { key: 'facts.offline', value: 'The rendered view must work while the agent is stopped and update when the saved file changes.', fact_type: 'goal', source_action: 'set_fact', updated_run_id: 12, updated_step: 6 },
    ], completed_goals: [
      { goal_id: 'goal-1', title: 'Read the persisted ledger safely', plan_summary: 'Expose durable knowledge in a Repo Facts tab', final_message: 'The workspace reader preserves UTF-8 and distinguishes missing from malformed files.', validation_summary: 'Reader tests passed for Unicode and missing files.', completed_at: '2026-09-02T12:15:00Z', original_index: 1, total_goal_count: 3, source: 'execution', goal_signature: 'goal-reader-42' },
    ] },
    { issue_id: 'issue-039', status: 'closed', request_summary: 'Add discovery budget extensions', closed_at: '2026-09-02T10:00:00Z', last_review_decision: 'accepted', facts: [
      { key: 'discovery.approval', value: 'Only an explicit user decision extends discovery. Declining preserves ambiguity in goal delegation.', fact_type: 'architecture', source_action: 'set_fact', updated_run_id: 9, updated_step: 11 },
    ], completed_goals: [{ goal_id: 'goal-2', title: 'Preserve discovery context', final_message: 'Approved extensions retain worker history.', validation_summary: 'Stable and beta continuation tests passed.', source: 'execution' }] },
    { issue_id: 'issue-001', status: 'closed', plan_summary: 'Initial repository setup', facts: [], lifecycle_notes: ['Older fact details compacted; issue record retained.'] },
  ],
};
export const repoFactsMarkdown = '# Repo Facts\n\nIssue-scoped durable facts recorded by the agent.\n\n```json\n' + JSON.stringify(repoFactsPayload, null, 2) + '\n```\n';
export const repoFactsSnapshot = (root: string): RepoFactsSnapshot => ({ workspaceRoot: root, path: 'repo_facts.md', status: 'ready', modifiedAt: Date.UTC(2026, 8, 2, 12, 15), ledger: parseRepoFacts(repoFactsMarkdown) });
