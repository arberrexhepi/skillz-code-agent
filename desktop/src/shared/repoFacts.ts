/** The persisted issue_facts.py ledger, rendered without starting or modifying the agent. */
export const REPO_FACTS_PATH = 'repo_facts.md';
export type RepoFactKind = 'architecture' | 'goal';
export interface RepoFact {
  key: string;
  value: string;
  kind: RepoFactKind;
  source: string;
  run: number;
  step: number;
}
export interface RepoGoalCheckpoint {
  id: string;
  title: string;
  plan: string;
  result: string;
  validation: string;
  completedAt: string;
  source: string;
  signature: string;
  index: number;
  total: number;
}
export interface RepoFactsIssue {
  id: string;
  request: string;
  plan: string;
  status: 'open' | 'closed';
  repositoryScope: boolean;
  facts: RepoFact[];
  checkpoints: RepoGoalCheckpoint[];
  openedAt: string;
  closedAt: string;
  source: string;
  parentId: string;
  excerpt: string;
  priority: number;
  reopenCount: number;
  blockedReason: string;
  review: string;
  notes: string[];
}
export interface RepoFactsLedger {
  schemaVersion: number | null;
  legacy: boolean;
  activeIssueId: string;
  issues: RepoFactsIssue[];
  migration: Record<string, unknown>;
  warnings: string[];
}
export interface RepoFactsSnapshot {
  workspaceRoot: string;
  path: typeof REPO_FACTS_PATH;
  status: 'ready' | 'missing' | 'invalid';
  modifiedAt?: number;
  ledger?: RepoFactsLedger;
  error?: string;
}
const text = (value: unknown): string => typeof value === 'string' ? value : '';
const number = (value: unknown): number => typeof value === 'number' && Number.isFinite(value) ? value : 0;
const map = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object' && !Array.isArray(value));

export function parseRepoFacts(markdown: string): RepoFactsLedger {
  const trimmed = markdown.replace(/^\uFEFF/, '').trim();
  if (!trimmed) return { schemaVersion: null, legacy: false, activeIssueId: '', issues: [], migration: {}, warnings: [] };
  const block = /```json\s*([\s\S]*?)\s*```/.exec(trimmed);
  let payload: unknown;
  try { payload = JSON.parse(block ? block[1] : trimmed); }
  catch { throw new Error('The saved file does not contain valid JSON. Open the source to inspect it, then refresh.'); }
  const legacy = Array.isArray(payload) || (map(payload) && Array.isArray(payload.facts));
  if (!legacy && (!map(payload) || payload.schema_version !== 2)) {
    throw new Error('This repo facts format is not supported. Expected schema version 2 or a legacy facts list. Open the source to inspect it.');
  }
  const document = map(payload) ? payload : {};
  const sourceIssues: unknown = legacy
    ? [{ issue_id: 'legacy-architecture', request_summary: 'Legacy repository facts', facts: Array.isArray(payload) ? payload : document.facts }]
    : document.issues ?? [];
  if (!Array.isArray(sourceIssues)) throw new Error('The saved issues field must be a list. Open the source to inspect it.');
  const warnings: string[] = [];
  let skipped = 0;
  const issues: RepoFactsIssue[] = [];
  const ids = new Set<string>();
  for (const raw of sourceIssues) {
    if (!map(raw) || !text(raw.issue_id).trim()) { skipped++; continue; }
    const id = text(raw.issue_id).trim();
    if (ids.has(id)) throw new Error(`Duplicate issue identifier: ${id}. Open the source to inspect it.`);
    ids.add(id);
    if ((raw.facts != null && !Array.isArray(raw.facts)) || (raw.completed_goals != null && !Array.isArray(raw.completed_goals))) {
      throw new Error(`Facts and completed goals for ${id} must be lists. Open the source to inspect it.`);
    }
    const facts: RepoFact[] = [];
    for (const fact of (raw.facts || []) as unknown[]) {
      if (!map(fact) || !text(fact.key).trim() || !text(fact.value).trim()) { skipped++; continue; }
      facts.push({ key: text(fact.key), value: text(fact.value), kind: !legacy && text(fact.fact_type).trim().toLowerCase() === 'goal' ? 'goal' : 'architecture',
        source: text(fact.source_action), run: number(fact.updated_run_id), step: number(fact.updated_step) });
    }
    const checkpoints: RepoGoalCheckpoint[] = [];
    for (const goal of (raw.completed_goals || []) as unknown[]) {
      if (!map(goal) || !(text(goal.goal_id) || text(goal.title) || text(goal.goal_signature)).trim()) { skipped++; continue; }
      checkpoints.push({ id: text(goal.goal_id), title: text(goal.title), plan: text(goal.plan_summary), result: text(goal.final_message),
        validation: text(goal.validation_summary), completedAt: text(goal.completed_at), source: text(goal.source), signature: text(goal.goal_signature),
        index: number(goal.original_index), total: number(goal.total_goal_count) });
    }
    if (legacy && !facts.length) continue;
    issues.push({ id, request: text(raw.request_summary), plan: text(raw.plan_summary), status: text(raw.status).trim().toLowerCase() === 'open' ? 'open' : 'closed',
      repositoryScope: ['global-architecture', 'legacy-architecture'].includes(id), facts, checkpoints,
      openedAt: text(raw.opened_at), closedAt: text(raw.closed_at), source: text(raw.source), parentId: text(raw.parent_issue_id),
      excerpt: text(raw.source_excerpt), priority: number(raw.priority), reopenCount: number(raw.reopen_count),
      blockedReason: text(raw.blocked_reason), review: text(raw.last_review_decision),
      notes: Array.isArray(raw.lifecycle_notes) ? raw.lifecycle_notes.filter((note): note is string => typeof note === 'string') : [] });
  }
  if (skipped) warnings.push(`${skipped} invalid record(s) could not be displayed. Inspect the source for the complete file.`);
  const active = legacy ? '' : text(document.active_issue_id).trim();
  if (active && !ids.has(active)) warnings.push(`The saved active issue (${active}) is missing from this ledger.`);
  return { schemaVersion: legacy ? null : 2, legacy, activeIssueId: ids.has(active) ? active : '', issues,
    migration: map(document.migration) ? document.migration : {}, warnings };
}

export type RepoFactsFilter = { query: string; kind: 'all' | RepoFactKind | 'checkpoints'; scope: string };
export function filterRepoFacts(ledger: RepoFactsLedger, filter: RepoFactsFilter): RepoFactsIssue[] {
  const query = filter.query.trim().toLocaleLowerCase();
  const matches = (...values: string[]): boolean => values.join(' ').toLocaleLowerCase().includes(query);
  return ledger.issues.filter((issue) => filter.scope === 'all' || issue.id === filter.scope).map((issue) => {
    const issueMatch = Boolean(query) && matches(issue.id, issue.request, issue.plan);
    const facts = filter.kind === 'checkpoints' ? [] : issue.facts.filter((fact) =>
      (filter.kind === 'all' || fact.kind === filter.kind) && (issueMatch || matches(fact.key, fact.value, fact.source)));
    const checkpoints = filter.kind !== 'all' && filter.kind !== 'checkpoints' ? [] : issue.checkpoints.filter((goal) =>
      issueMatch || matches(goal.id, goal.title, goal.plan, goal.result, goal.validation));
    return { ...issue, facts, checkpoints };
  }).filter((issue) => issue.facts.length || issue.checkpoints.length || (!query && filter.kind === 'all') || (filter.kind === 'all' && matches(issue.id, issue.request, issue.plan)))
    .sort((a, b) => {
      const rank = (issue: RepoFactsIssue): number => issue.id === ledger.activeIssueId ? 0 : issue.repositoryScope ? 1 : issue.status === 'open' ? 2 : 3;
      return rank(a) - rank(b) || a.id.localeCompare(b.id, undefined, { numeric: true });
    });
}
