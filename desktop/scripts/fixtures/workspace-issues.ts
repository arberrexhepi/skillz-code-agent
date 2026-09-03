import type { AgentBridgeState, IssueProposal } from '../../src/shared/agentTypes';
import { issuesFromLedger, type WorkspaceIssuesSnapshot } from '../../src/shared/workspaceIssues';
import { repoFactsSnapshot } from './repo-facts';
export const savedProposals: IssueProposal[] = ['accept', 'ignore'].map(name => ({ proposal_id: `proposal-${name}`, status: 'proposed', author: 'agent', summary: `Review ${name} suggestion in src/app.ts`, reason: 'Separate from the current goal', evidence: 'Observed a problem in src/app.ts', paths: ['src/app.ts'], parent_issue_id: 'issue-042', goal: 'Render repository facts', created_at: '2026-09-02T12:00:00Z' }));
export function issuesSnapshot(root: string): WorkspaceIssuesSnapshot {
  const facts = repoFactsSnapshot(root);
  return { workspaceRoot: root, status: 'ready', activeIssueId: facts.ledger!.activeIssueId, issues: issuesFromLedger(facts.ledger), proposals: structuredClone(savedProposals), warnings: [] };
}
export function issuesBridge(snapshot: WorkspaceIssuesSnapshot): AgentBridgeState {
  const issues = snapshot.issues.map(issue => ({ issue_id: issue.id, status: issue.status, request_summary: issue.request, plan_summary: issue.plan }));
  return { planner: { issue_state: { active_issue_id: snapshot.activeIssueId, active_issue: issues.find(issue => issue.issue_id === snapshot.activeIssueId) || null, issues }, worker_state: { issue_proposals: { proposals: snapshot.proposals.filter(proposal => proposal.status === 'proposed') } } }, transcript: [] };
}
export function applyIssueAction(snapshot: WorkspaceIssuesSnapshot, action: string, extras: Record<string, unknown> = {}): void {
  if (action === 'accept_issue_proposal' || action === 'ignore_issue_proposal') {
    const proposal = snapshot.proposals.find(item => item.proposal_id === extras.proposal_id);
    if (!proposal || proposal.status !== 'proposed') throw new Error('Suggestion no longer pending');
    proposal.status = action === 'accept_issue_proposal' ? 'accepted' : 'ignored';
    if (proposal.status === 'accepted') snapshot.issues.push({ ...snapshot.issues[0], id: 'issue-accepted', status: 'open', request: proposal.summary, plan: '', checkpoints: [], notes: [] });
    return;
  }
  if (action === 'create_issue') { snapshot.issues.push({ ...snapshot.issues[0], id: 'issue-created', status: 'open', request: String(extras.summary), plan: '', checkpoints: [], notes: [] }); return; }
  const issue = snapshot.issues.find(item => item.id === extras.issue_id);
  if (!issue) throw new Error('Issue missing');
  if (action === 'close_issue') { issue.status = 'closed'; if (snapshot.activeIssueId === issue.id) snapshot.activeIssueId = ''; }
  if (action === 'reopen_issue') { issue.status = 'open'; issue.closedAt = ''; issue.reopenCount++; }
  if (action === 'continue_issue') snapshot.activeIssueId = issue.id;
}
