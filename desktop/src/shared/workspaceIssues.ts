import type { AgentBridgeState, IssueProposal, IssueSummary } from './agentTypes';
import type { RepoFactsIssue, RepoFactsLedger } from './repoFacts';

export const ISSUE_PROPOSALS_PATH = '.agent-issue-proposals.json';
export const isRepositoryScope = (id: string): boolean => ['global-architecture', 'legacy-architecture'].includes(id);
export type ManagedIssue = Omit<RepoFactsIssue, 'facts' | 'repositoryScope'> & { factCount: number };
export interface WorkspaceIssuesSnapshot {
  workspaceRoot: string;
  status: 'ready' | 'missing' | 'invalid';
  issues: ManagedIssue[];
  activeIssueId: string;
  warnings: string[];
  error?: string;
  proposals: IssueProposal[];
  proposalError?: string;
}
export function issuesFromLedger(ledger?: RepoFactsLedger): ManagedIssue[] {
  return (ledger?.issues || []).filter(issue => !issue.repositoryScope).map(({ facts, repositoryScope: _scope, ...issue }) => ({ ...issue, factCount: facts.length }));
}
export function parseIssueProposals(text: string): IssueProposal[] {
  const value = JSON.parse(text.replace(/^\uFEFF/, ''));
  if (value?.version !== 1 || !Array.isArray(value.proposals)) throw new Error('Invalid issue proposal storage. Expected version 1 and a proposals list.');
  const ids = new Set<string>();
  return value.proposals.map((raw: Record<string, unknown>) => {
    if (!raw || typeof raw.proposal_id !== 'string' || !raw.proposal_id.trim() || ids.has(raw.proposal_id) || !['proposed', 'accepted', 'ignored'].includes(String(raw.status)) || ['summary', 'reason', 'evidence'].some(key => typeof raw[key] !== 'string') || !Array.isArray(raw.paths) || raw.paths.some(path => typeof path !== 'string')) throw new Error('Invalid or duplicate saved issue proposal. Open the source to inspect it.');
    ids.add(raw.proposal_id);
    return { proposal_id: raw.proposal_id, status: raw.status as IssueProposal['status'], author: 'agent', summary: String(raw.summary), reason: String(raw.reason), evidence: String(raw.evidence), paths: raw.paths as string[], parent_issue_id: String(raw.parent_issue_id || ''), goal: String(raw.goal || ''), created_at: String(raw.created_at || '') };
  });
}
const text = (value: unknown): string => typeof value === 'string' ? value : '';
function liveIssue(raw: IssueSummary, previous?: ManagedIssue): ManagedIssue {
  const value = (key: string, old?: string): string => typeof raw[key] === 'string' ? text(raw[key]) : old || '';
  return {
    id: String(raw.issue_id), status: raw.status === 'closed' ? 'closed' : raw.status === 'open' ? 'open' : previous?.status || 'open',
    request: value('request_summary', previous?.request), plan: value('plan_summary', previous?.plan),
    factCount: typeof raw.fact_count === 'number' ? raw.fact_count : previous?.factCount || 0,
    checkpoints: previous?.checkpoints || [], notes: Array.isArray(raw.lifecycle_notes) ? raw.lifecycle_notes.filter((note): note is string => typeof note === 'string') : previous?.notes || [],
    openedAt: value('opened_at', previous?.openedAt), closedAt: value('closed_at', previous?.closedAt),
    source: value('source', previous?.source), parentId: value('parent_issue_id', previous?.parentId), excerpt: value('source_excerpt', previous?.excerpt),
    blockedReason: value('blocked_reason', previous?.blockedReason), review: value('last_review_decision', previous?.review),
    priority: typeof raw.priority === 'number' ? raw.priority : previous?.priority || 0,
    reopenCount: typeof raw.reopen_count === 'number' ? raw.reopen_count : previous?.reopenCount || 0,
  };
}
/** Saved records survive a stopped agent; current runtime records override matching saved ids. */
export function issueManagementView(saved: WorkspaceIssuesSnapshot | undefined, live?: AgentBridgeState): { issues: ManagedIssue[]; activeIssueId: string; proposals: IssueProposal[]; proposalError?: string } {
  const indexed = new Map((saved?.issues || []).map(issue => [issue.id, issue]));
  const worker = live?.planner.worker_state;
  const source = worker?.issue_state || live?.planner.issue_state;
  let activeIssueId = saved?.activeIssueId || '';
  if (source) {
    const active = source.active_issue as IssueSummary | null | undefined;
    const records = [source.issues, source.open_issues, source.reopenable_issues].flatMap(items => Array.isArray(items) ? items : []) as IssueSummary[];
    if (active) records.push(active);
    for (const raw of records) if (raw?.issue_id && !isRepositoryScope(raw.issue_id)) indexed.set(raw.issue_id, liveIssue(raw, indexed.get(raw.issue_id)));
    if ('active_issue' in source || 'active_issue_id' in source) activeIssueId = active?.issue_id || text(source.active_issue_id);
  } else if (worker?.issue_context?.active_durable_issue) {
    const active = worker.issue_context.active_durable_issue;
    if (active.issue_id && !isRepositoryScope(active.issue_id)) { indexed.set(active.issue_id, liveIssue(active, indexed.get(active.issue_id))); activeIssueId = active.issue_id; }
  }
  const issues = [...indexed.values()].filter(issue => !isRepositoryScope(issue.id));
  if (!issues.some(issue => issue.id === activeIssueId && issue.status === 'open')) activeIssueId = '';
  issues.sort((a, b) => Number(b.id === activeIssueId) - Number(a.id === activeIssueId) || Number(b.status === 'open') - Number(a.status === 'open') || (b.closedAt || b.openedAt).localeCompare(a.closedAt || a.openedAt) || b.id.localeCompare(a.id, undefined, { numeric: true }));
  const proposals = worker?.issue_proposals;
  return { issues, activeIssueId, proposals: (proposals?.proposals && !proposals.error ? proposals.proposals : saved?.proposals || []).filter(proposal => proposal.status === 'proposed'), proposalError: proposals?.error || saved?.proposalError };
}
