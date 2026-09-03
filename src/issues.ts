export type Raw = Record<string, unknown>;
export type Issue = Raw & { issue_id: string; status?: string; request_summary?: string; plan_summary?: string; opened_at?: string; closed_at?: string; priority?: number; reopen_count?: number };
export interface LedgerDocument { before: string; after: string; ledger: Raw & { issues: Issue[]; active_issue_id?: string } }
const now = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
export function parseLedgerDocument(source: string): LedgerDocument {
  const block = /```json\s*([\s\S]*?)\s*```/.exec(source);
  if (!block) throw new Error('repo_facts.md must contain a fenced JSON ledger.');
  const ledger = JSON.parse(block[1]) as LedgerDocument['ledger'];
  if (ledger.schema_version !== 2 || !Array.isArray(ledger.issues)) throw new Error('Expected a schema-version-2 issue ledger.');
  if (ledger.issues.some(issue => !issue || typeof issue.issue_id !== 'string')) throw new Error('Every issue needs an issue_id.');
  return { before: source.slice(0, block.index) + '```json\n', after: '\n```' + source.slice(block.index + block[0].length), ledger };
}
export function serializeLedger(document: LedgerDocument, ledger: LedgerDocument['ledger']): string { return document.before + JSON.stringify(ledger, null, 2) + document.after; }
export function newLedger(): string { return '# Repository Facts\n\n```json\n' + JSON.stringify({ schema_version: 2, active_issue_id: '', migration: { legacy_flat_list_migrated: false }, issues: [] }, null, 2) + '\n```\n'; }
export function transitionIssue(ledger: LedgerDocument['ledger'], id: string, action: 'activate' | 'close' | 'reopen', timestamp = now()): void {
  const item = ledger.issues.find(issue => issue.issue_id === id); if (!item) throw new Error('Issue no longer exists.');
  if (action === 'close') { item.status = 'closed'; item.closed_at ||= timestamp; if (ledger.active_issue_id === id) ledger.active_issue_id = ''; }
  if (action === 'reopen') { item.status = 'open'; item.closed_at = ''; item.reopen_count = Number(item.reopen_count || 0) + 1; ledger.active_issue_id = id; }
  if (action === 'activate') { if (item.status !== 'open') throw new Error('Reopen this issue first.'); ledger.active_issue_id = id; }
}
export function createIssue(ledger: LedgerDocument['ledger'], request: string, plan: string, timestamp = now()): string {
  const summary = request.trim(); if (!summary) throw new Error('Issue summary is required.');
  const numbers = ledger.issues.map(issue => /^issue-(\d+)$/.exec(issue.issue_id)?.[1]).filter((value): value is string => Boolean(value)).map(Number);
  const id = `issue-${String(Math.max(0, ...numbers) + 1).padStart(3, '0')}`;
  ledger.issues.push({ issue_id: id, status: 'open', request_summary: summary, plan_summary: plan.trim() || summary, opened_at: timestamp, source: 'artifact', priority: 0, facts: [], completed_goals: [], lifecycle_notes: [] });
  ledger.active_issue_id = id; return id;
}
