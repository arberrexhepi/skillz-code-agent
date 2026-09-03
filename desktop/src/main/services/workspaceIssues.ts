import { promises as fs } from 'node:fs';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { readRepoFacts } from './repoFacts';
import { ISSUE_PROPOSALS_PATH, issuesFromLedger, parseIssueProposals, type WorkspaceIssuesSnapshot } from '../../shared/workspaceIssues';

export type WorkspaceIssueAction = 'create_issue' | 'close_issue' | 'reopen_issue';
type JsonObject = Record<string, unknown>;
const factsName = 'repo_facts.md';
const maxBytes = 5 * 1024 * 1024;
const timestamp = (): string => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
const object = (value: unknown): value is JsonObject => Boolean(value && typeof value === 'object' && !Array.isArray(value));

function parseDocument(source: string): { before: string; after: string; ledger: JsonObject & { issues: JsonObject[] } } {
  const block = /```json\s*([\s\S]*?)\s*```/.exec(source);
  const json = block?.[1] ?? source.trim();
  let value: unknown;
  try { value = JSON.parse(json); }
  catch { throw new Error('repo_facts.md does not contain valid JSON. Open it in the editor and repair it before changing issues.'); }
  if (!object(value) || value.schema_version !== 2 || !Array.isArray(value.issues) || value.issues.some(issue => !object(issue) || typeof issue.issue_id !== 'string')) {
    throw new Error('Issue changes require a schema-version-2 repo_facts.md ledger.');
  }
  const ids = value.issues.map(issue => String((issue as JsonObject).issue_id));
  if (new Set(ids).size !== ids.length) throw new Error('repo_facts.md contains duplicate issue identifiers.');
  if (!block) return { before: '', after: '\n', ledger: value as JsonObject & { issues: JsonObject[] } };
  const contentStart = block.index + block[0].indexOf(block[1]);
  return { before: source.slice(0, contentStart), after: source.slice(contentStart + block[1].length), ledger: value as JsonObject & { issues: JsonObject[] } };
}

function emptyDocument(): { source: string; ledger: JsonObject & { issues: JsonObject[] } } {
  const ledger = { schema_version: 2, active_issue_id: '', migration: { legacy_flat_list_migrated: false }, issues: [] };
  return { source: '# Repo Facts\n\nIssue-scoped durable facts recorded by the agent.\n\n```json\n' + JSON.stringify(ledger, null, 2) + '\n```\n', ledger };
}

/** Mutate the durable issue ledger directly; no Python runtime or agent process is involved. */
export async function mutateWorkspaceIssue(root: string, action: WorkspaceIssueAction, extras: Record<string, unknown>, assertCurrent: () => void = () => {}): Promise<void> {
  assertCurrent();
  const target = path.join(root, factsName);
  let original: Buffer | undefined;
  let mode = 0o600;
  try {
    const info = await fs.lstat(target);
    if (info.isSymbolicLink() || !info.isFile()) throw new Error('repo_facts.md must be a regular workspace file.');
    if (info.size > maxBytes) throw new Error('repo_facts.md is larger than the 5 MB issue-management limit.');
    original = await fs.readFile(target);
    if (original.includes(0)) throw new Error('repo_facts.md contains binary data.');
    mode = info.mode;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT' || action !== 'create_issue') throw error;
  }

  let existing: string;
  try { existing = original ? new TextDecoder('utf-8', { fatal: true }).decode(original) : emptyDocument().source; }
  catch { throw new Error('repo_facts.md is not valid UTF-8. Save it as UTF-8 before changing issues.'); }
  const document = parseDocument(existing);
  const ledger = document.ledger;
  if (action === 'create_issue') {
    const summary = String(extras.summary || '').trim();
    if (!summary) throw new Error('Enter issue details before adding an issue.');
    const highest = Math.max(0, ...ledger.issues.map(issue => /^issue-(\d+)$/.exec(String(issue.issue_id))?.[1]).filter((value): value is string => Boolean(value)).map(Number));
    ledger.issues.push({ issue_id: `issue-${String(highest + 1).padStart(3, '0')}`, request_summary: summary, plan_summary: summary, status: 'open', opened_at: timestamp(), source: 'workbench', parent_issue_id: '', source_excerpt: '', priority: 0, reopen_count: 0, facts: [], completed_goals: [], lifecycle_notes: [] });
  } else {
    const id = String(extras.issue_id || '').trim();
    const issue = ledger.issues.find(item => item.issue_id === id);
    if (!issue) throw new Error(`Issue ${id || '(missing ID)'} no longer exists.`);
    if (action === 'close_issue') {
      issue.status = 'closed';
      if (!String(issue.closed_at || '').trim()) issue.closed_at = timestamp();
      if (ledger.active_issue_id === id) ledger.active_issue_id = '';
    } else {
      issue.status = 'open'; issue.closed_at = ''; issue.reopen_count = Number(issue.reopen_count || 0) + 1; ledger.active_issue_id = id;
    }
  }
  const output = document.before + JSON.stringify(ledger, null, 2) + document.after;
  if (Buffer.byteLength(output) > maxBytes) throw new Error('The updated repo_facts.md would exceed 5 MB.');
  const current = await fs.readFile(target).catch(error => (error as NodeJS.ErrnoException).code === 'ENOENT' ? undefined : Promise.reject(error));
  if ((original && (!current || !current.equals(original))) || (!original && current)) throw new Error('repo_facts.md changed while the issue was being updated. Refresh and retry.');
  assertCurrent();
  const temporary = path.join(root, `.repo_facts.skillz-${randomUUID()}.tmp`);
  try {
    const file = await fs.open(temporary, 'wx', mode);
    try { await file.writeFile(output, 'utf8'); await file.sync(); } finally { await file.close(); }
    await fs.chmod(temporary, mode);
    await fs.rename(temporary, target);
  } finally { await fs.rm(temporary, { force: true }); }
}

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
