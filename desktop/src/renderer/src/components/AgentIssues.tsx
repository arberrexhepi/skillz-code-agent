import { useEffect, useRef, useState } from 'react';
import { continuousIsActive } from '../../../shared/agentCore';
import { issueManagementView, ISSUE_PROPOSALS_PATH, type ManagedIssue, type WorkspaceIssuesSnapshot } from '../../../shared/workspaceIssues';
import { useAgentWorkspace } from '../agent/agentWorkspace';
import { PathChip, PathText } from './PathText';
import { IssueCreateForm } from './IssueCreateForm';
import { AgentSuggestions } from './AgentSuggestions';

export function AgentIssues({ workspaceRoot, revision, focusedIssueId, onOpenPath }: {
  workspaceRoot: string; revision: number; focusedIssueId?: string; onOpenPath: (path: string) => void;
}): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [saved, setSaved] = useState<WorkspaceIssuesSnapshot>();
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [summary, setSummary] = useState('');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const [limit, setLimit] = useState(20);
  const [expandedIssueId, setExpandedIssueId] = useState(focusedIssueId || '');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');
  const [actionId, setActionId] = useState('');
  const [actionError, setActionError] = useState('');
  const creatingRef = useRef(false);
  const actingRef = useRef(false);
  const mounted = useRef(true);
  const panel = useRef<HTMLElement>(null);
  const focused = useRef('');
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    let current = true;
    setLoading(true); setReadError('');
    void Promise.resolve().then(() => {
      if (!window.workbench.workspace.issues) throw new Error('Restart the desktop app to load the saved Issues reader.');
      return window.workbench.workspace.issues(workspaceRoot);
    }).then(next => {
      if (!current) return;
      if (next.workspaceRoot !== workspaceRoot) throw new Error('Workspace changed. Refresh Issues in the intended folder.');
      setSaved(next);
    }).catch(error => { if (current) { setSaved(undefined); setReadError(cleanError(error)); } })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [workspaceRoot, revision, refresh]);
  useEffect(() => {
    if (focusedIssueId) { setExpandedIssueId(focusedIssueId); setQuery(focusedIssueId); setFilter('all'); setLimit(20); }
  }, [focusedIssueId]);
  const snapshot = saved?.workspaceRoot === workspaceRoot ? saved : undefined;
  const live = agent.state.status === 'running' ? agent.state.bridge : undefined;
  const view = issueManagementView(snapshot, live);
  const diagnostics = live?.planner.worker_state?.issue_context?.run_diagnostics || [];
  const executionBusy = Boolean(agent.state.pendingAction || agent.state.bridge.planner.executing || continuousIsActive(agent.state.bridge));
  const busy = executionBusy || Boolean(actionId);
  const visible = view.issues.filter(issue => (filter === 'all' || issue.status === filter) && [issue.id, issue.request, issue.plan, issue.blockedReason].join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  useEffect(() => {
    if (!focusedIssueId || loading || focused.current === focusedIssueId) return;
    const card = [...(panel.current?.querySelectorAll<HTMLElement>('[data-issue-id]') || [])].find(item => item.dataset.issueId === focusedIssueId);
    if (card) { focused.current = focusedIssueId; card.querySelector<HTMLButtonElement>('.issue-card-toggle')?.focus(); }
  }, [focusedIssueId, loading, visible]);
  const reload = (): void => { if (mounted.current) setRefresh(value => value + 1); };
  const create = async (): Promise<void> => {
    if (!summary.trim() || creatingRef.current) return;
    creatingRef.current = true; setCreating(true); setCreateError('');
    try {
      if (agent.state.status === 'running') await agent.createIssue(summary.trim());
      else {
        if (!window.workbench.workspace.issueAction) throw new Error('Restart the desktop app to enable offline issue changes.');
        const next = await window.workbench.workspace.issueAction(workspaceRoot, 'create_issue', { summary: summary.trim() });
        if (mounted.current) setSaved(next);
      }
      if (mounted.current) { setSummary(''); reload(); }
    }
    catch (error) { if (mounted.current) setCreateError(cleanError(error)); }
    finally { creatingRef.current = false; if (mounted.current) setCreating(false); }
  };
  const manage = async (issue: ManagedIssue, action: 'continue_issue' | 'close_issue' | 'reopen_issue'): Promise<void> => {
    if (actingRef.current || busy) return;
    actingRef.current = true; setActionId(issue.id); setActionError('');
    try {
      if (action === 'continue_issue' || agent.state.status === 'running') {
        if (!(await agent.plannerAction(action, { issue_id: issue.id }))) throw new Error(`Could not update ${issue.id}. Check the agent notice or runtime settings and retry.`);
      } else {
        if (!window.workbench.workspace.issueAction) throw new Error('Restart the desktop app to enable offline issue changes.');
        const next = await window.workbench.workspace.issueAction(workspaceRoot, action, { issue_id: issue.id });
        if (mounted.current) setSaved(next);
      }
      reload();
    } catch (error) { if (mounted.current) setActionError(cleanError(error)); }
    finally { actingRef.current = false; if (mounted.current) setActionId(''); }
  };
  return <section ref={panel} className="issues-panel" aria-label="Issue management">
    <p className="issues-intro">Manage suggested, open, and closed work.</p>
    <IssueCreateForm summary={summary} creating={creating} executionBusy={executionBusy} error={createError} onChange={setSummary} onCreate={() => void create()} />
    <div className="issue-summary"><span>{view.issues.filter(issue => issue.status === 'open').length} open</span><span>{view.issues.filter(issue => issue.status === 'closed').length} closed</span><span>{view.proposals.length} suggestions</span></div>
    <p className="issues-load-state" role="status">{loading ? 'Loading saved issues…' : agent.state.status === 'running' ? 'Saved issues · Runtime connected' : 'Saved issues · Create, close, and reopen work offline'}</p>
    {(readError || snapshot?.error) && <div className="issues-error" role="alert"><PathText>{readError || snapshot?.error}</PathText><button type="button" onClick={reload}>Retry loading issues</button><button type="button" onClick={() => onOpenPath('repo_facts.md')}>Inspect saved ledger</button></div>}
    {snapshot?.warnings.map(warning => <p className="issues-error" role="status" key={warning}><PathText>{warning}</PathText></p>)}
    <AgentSuggestions proposals={view.proposals} error={view.proposalError} busy={executionBusy} onDecide={async (id, decision) => { await agent.decideIssueProposal(id, decision); reload(); }} />
    {view.proposalError && <div className="issues-source-actions"><button type="button" onClick={reload}>Retry suggestions</button><button type="button" onClick={() => onOpenPath(ISSUE_PROPOSALS_PATH)}>Inspect suggestions</button></div>}
    <div className="issues-filters"><label>Search issues<input type="search" value={query} onChange={event => { setQuery(event.target.value); setLimit(20); }} placeholder="Issue, summary, or blocked reason…" /></label><label>Status<select value={filter} onChange={event => { setFilter(event.target.value); setLimit(20); }}><option value="all">Open and closed</option><option value="open">Open</option><option value="closed">Closed</option></select></label></div>
    {actionError && <p className="issues-error" role="alert"><PathText>{actionError}</PathText></p>}
    {visible.slice(0, limit).map(issue => <ManagedIssueCard key={issue.id} issue={issue} active={issue.id === view.activeIssueId} expanded={issue.id === expandedIssueId} busy={busy} pending={actionId === issue.id} onToggle={() => setExpandedIssueId(current => current === issue.id ? '' : issue.id)} onAction={action => void manage(issue, action)} />)}
    {visible.length > limit && <button className="issues-more" type="button" onClick={() => setLimit(value => value + 20)}>Show more issues ({visible.length - limit} remaining)</button>}
    {!loading && !visible.length && <div className="panel-message">{view.issues.length ? 'No issues match this search or status.' : snapshot?.status === 'invalid' || readError ? 'Saved issues are unavailable until the file can be read.' : 'No saved issues yet. Add one above or accept an agent suggestion.'}{(query || filter !== 'all') && <button type="button" onClick={() => { setQuery(''); setFilter('all'); }}>Clear issue filters</button>}</div>}
    {diagnostics.length > 0 && <section className="issue-section"><header>RUN DIAGNOSTICS</header>{diagnostics.map((item, index) => <div className="run-diagnostic" key={item.issue_id || index}><span>{item.status === 'deferred' ? '↗' : '!'}</span><div><strong><PathText>{item.summary || item.code || item.issue_id}</PathText></strong><small>{item.status === 'deferred' ? 'Deferred · ' : ''}{item.file ? <PathChip path={`${item.file}${item.line ? `:${item.line}` : ''}`} /> : <button type="button" onClick={() => void agent.workerAction({ type: 'show_run_issue', issue_id: item.issue_id })}>Show finding</button>}</small></div></div>)}</section>}
  </section>;
}

export function ManagedIssueCard({ issue, active, expanded, busy, pending, onToggle, onAction }: {
  issue: ManagedIssue; active: boolean; expanded: boolean; busy: boolean; pending: boolean; onToggle: () => void;
  onAction: (action: 'continue_issue' | 'close_issue' | 'reopen_issue') => void;
}): React.JSX.Element {
  const closed = issue.status === 'closed';
  return <article className={`issue-card ${active ? 'active' : ''} ${closed ? 'closed' : ''} ${expanded ? 'expanded' : ''}`} aria-label={issue.id} data-issue-id={issue.id}>
    <button type="button" className="issue-card-toggle" aria-label={`Details for ${issue.id}`} aria-expanded={expanded} onClick={onToggle}><span className="issue-chevron">›</span><span className="issue-card-copy"><span className="issue-kind">{active ? 'ACTIVE' : closed ? 'CLOSED' : 'OPEN'}</span><strong>{issue.id}</strong></span></button>
    <p className="issue-path-summary"><PathText>{issue.plan || issue.request || 'Untitled issue'}</PathText></p>
    {issue.blockedReason && <p className="issue-blocked">Blocked: <PathText>{issue.blockedReason}</PathText></p>}
    <div className="issue-card-actions issue-primary-actions">{closed ? <button type="button" disabled={busy} aria-label={`Reopen ${issue.id}`} onClick={() => onAction('reopen_issue')}>Reopen</button> : <><button type="button" className="primary-button" disabled={busy} aria-label={`Continue ${issue.id}`} onClick={() => onAction('continue_issue')}>Continue</button><button type="button" disabled={busy} aria-label={`Close ${issue.id}`} onClick={() => onAction('close_issue')}>Close</button></>}{pending && <small role="status">Updating…</small>}</div>
    {expanded && <div className="issue-card-details">
      <dl className="issues-metadata"><Detail label="Request" value={issue.request} /><Detail label="Plan" value={issue.plan} /><Detail label="Opened" value={issue.openedAt} /><Detail label="Closed" value={issue.closedAt} /><Detail label="Source" value={issue.source} /><Detail label="Parent issue" value={issue.parentId} /><Detail label="Priority" value={String(issue.priority)} /><Detail label="Reopened" value={String(issue.reopenCount)} /><Detail label="Last review" value={issue.review} /><Detail label="Source excerpt" value={issue.excerpt} /></dl>
      {issue.notes.length > 0 && <section><h4>Lifecycle notes</h4><ul>{issue.notes.map((note, index) => <li key={index}><PathText>{note}</PathText></li>)}</ul></section>}
      {issue.checkpoints.length > 0 && <section><h4>Completed goals · {issue.checkpoints.length}</h4>{issue.checkpoints.map((goal, index) => <details className="issue-checkpoint" key={goal.signature || `${goal.id}-${index}`}><summary><PathText>{goal.title || goal.id || 'Completed goal'}</PathText></summary><p><PathText>{goal.result}</PathText></p>{goal.validation && <p><strong>Validation</strong><br /><PathText>{goal.validation}</PathText></p>}<dl className="issues-metadata"><Detail label="Completed" value={goal.completedAt} /><Detail label="Plan" value={goal.plan} /><Detail label="Source" value={goal.source} /><Detail label="Goal position" value={goal.index ? `${goal.index} of ${goal.total || '?'}` : ''} /><Detail label="Signature" value={goal.signature} /></dl></details>)}</section>}
    </div>}
  </article>;
}
function Detail({ label, value }: { label: string; value: string }): React.JSX.Element | null { return value ? <div><dt>{label}</dt><dd><PathText>{value}</PathText></dd></div> : null; }
function cleanError(error: unknown): string { return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '').replace(/^Error: /, ''); }
