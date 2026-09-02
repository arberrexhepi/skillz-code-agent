import { promises as fs } from 'node:fs';
import path from 'node:path';
import { readRepoFacts } from './repoFacts';
import { ISSUE_PROPOSALS_PATH, issuesFromLedger, parseIssueProposals, type WorkspaceIssuesSnapshot } from '../../shared/workspaceIssues';

async function readProposals(root: string) {
  const file = path.join(root, ISSUE_PROPOSALS_PATH);
  try {
    const stat = await fs.lstat(file);
    if (stat.isSymbolicLink() || !stat.isFile()) throw new Error('Issue proposal storage must be a regular workspace file.');
    if (stat.size > 5 * 1024 * 1024) throw new Error('Issue proposal storage exceeds the 5 MB viewer limit.');
    const content = new TextDecoder('utf-8', { fatal: true }).decode(await fs.readFile(file));
    return parseIssueProposals(content);
  } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return []; throw error; }
}
export async function readWorkspaceIssues(root: string): Promise<WorkspaceIssuesSnapshot> {
  const [facts, proposals] = await Promise.allSettled([readRepoFacts(root), readProposals(root)]);
  const ledger = facts.status === 'fulfilled' ? facts.value : undefined;
  return {
    workspaceRoot: root, status: ledger?.status || 'invalid', issues: issuesFromLedger(ledger?.ledger), activeIssueId: ledger?.ledger?.activeIssueId || '', warnings: ledger?.ledger?.warnings || [],
    error: facts.status === 'rejected' ? String(facts.reason).replace(/^Error: /, '') : ledger?.error,
    proposals: proposals.status === 'fulfilled' ? proposals.value : [],
    proposalError: proposals.status === 'rejected' ? String(proposals.reason).replace(/^Error: /, '') : undefined,
  };
}
