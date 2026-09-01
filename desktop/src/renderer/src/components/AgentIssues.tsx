import { useState } from 'react';
import { issueState } from '../../../shared/agentCore';
import type { IssueSummary } from '../../../shared/agentTypes';
import { useAgentWorkspace } from '../agent/agentWorkspace';

export function AgentIssues({ onOpenPath }: { onOpenPath: (path: string) => void }): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [summary, setSummary] = useState('');
  const [expandedIssueId, setExpandedIssueId] = useState('');
  const issues = issueState(agent.state.bridge);
  const worker = agent.state.bridge.planner.worker_state;
  const diagnostics = worker?.issue_context?.run_diagnostics || [];
  const facts = worker?.current_run_facts || [];
  const busy = Boolean(agent.state.pendingAction);
  const toggleIssue = (issueId: string): void => setExpandedIssueId((current) => current === issueId ? '' : issueId);
  const create = async (): Promise<void> => {
    const value = summary.trim();
    if (value && await agent.plannerAction('create_issue', { summary: value })) setSummary('');
  };

  return <div className="issues-panel">
    <form className="issue-create" onSubmit={(event) => { event.preventDefault(); void create(); }}><input value={summary} onChange={(event) => setSummary(event.target.value)} placeholder="Create a durable issue…" /><button className="primary-button" disabled={!summary.trim() || busy}>＋</button></form>
    <div className="issue-summary"><span>{issues.open.length + (issues.active ? 1 : 0)} open</span><span>{issues.totalFacts} facts</span><span>{diagnostics.length} run findings</span></div>

    {issues.active && <IssueCard
      issue={issues.active}
      active
      expanded={expandedIssueId === issues.active.issue_id}
      busy={busy}
      onToggle={() => toggleIssue(String(issues.active?.issue_id || 'active'))}
      primaryLabel="Continue"
      onPrimary={() => void agent.plannerAction('continue_issue', { issue_id: issues.active?.issue_id })}
      onClose={() => void agent.submit(`/close-issue ${issues.active?.issue_id}`)}
    />}

    {issues.open.length > 0 && <Section title="OPEN ISSUES">{issues.open.map((issue) => <IssueCard
      issue={issue}
      key={issue.issue_id}
      expanded={expandedIssueId === issue.issue_id}
      busy={busy}
      onToggle={() => toggleIssue(String(issue.issue_id || ''))}
      primaryLabel="Continue"
      onPrimary={() => void agent.plannerAction('continue_issue', { issue_id: issue.issue_id })}
      onClose={() => void agent.submit(`/close-issue ${issue.issue_id}`)}
    />)}</Section>}

    {diagnostics.length > 0 && <Section title="RUN DIAGNOSTICS">{diagnostics.map((item, index) => <button className="run-diagnostic" key={item.issue_id || index} onClick={() => item.file ? onOpenPath(item.file) : void agent.workerAction({ type: 'show_run_issue', issue_id: item.issue_id })}><span>!</span><div><strong>{item.summary || item.code || item.issue_id}</strong><small>{item.file}{item.line ? `:${item.line}` : ''}</small></div></button>)}</Section>}
    {facts.length > 0 && <Section title="RUN FACTS">{facts.slice(0, 12).map((fact, index) => <div className="fact-row" key={`${fact.key}-${index}`}><strong>{fact.key}</strong><span>{fact.value}</span></div>)}</Section>}

    {issues.reopenable.length > 0 && <Section title="RECENTLY CLOSED">{issues.reopenable.slice(0, 6).map((issue) => <IssueCard
      issue={issue}
      closed
      key={issue.issue_id}
      expanded={expandedIssueId === issue.issue_id}
      busy={busy}
      onToggle={() => toggleIssue(String(issue.issue_id || ''))}
      primaryLabel="Reopen"
      onPrimary={() => void agent.submit(`reopen ${issue.issue_id}`)}
    />)}</Section>}

    {!issues.active && !issues.open.length && !diagnostics.length && <div className="panel-message">Durable issues, run findings, and facts will live here instead of competing with the conversation.</div>}
  </div>;
}

interface IssueCardProps {
  issue: IssueSummary;
  active?: boolean;
  closed?: boolean;
  expanded: boolean;
  busy: boolean;
  primaryLabel: 'Continue' | 'Reopen';
  onToggle: () => void;
  onPrimary: () => void;
  onClose?: () => void;
}

function IssueCard({ issue, active = false, closed = false, expanded, busy, primaryLabel, onToggle, onPrimary, onClose }: IssueCardProps): React.JSX.Element {
  const summary = issue.plan_summary || issue.request_summary || 'Untitled issue';
  return <article className={`issue-card ${active ? 'active' : ''} ${closed ? 'closed' : ''} ${expanded ? 'expanded' : ''}`}>
    <button type="button" className="issue-card-toggle" aria-expanded={expanded} onClick={onToggle}>
      <span className="issue-chevron">›</span>
      <span className="issue-card-copy"><span className="issue-kind">{active ? 'ACTIVE' : closed ? 'CLOSED' : 'OPEN'}</span><strong>{issue.issue_id}</strong><small>{summary}</small></span>
      {closed && issue.fact_count ? <span className="issue-fact-count">{issue.fact_count} facts</span> : null}
    </button>
    {expanded && <div className="issue-card-details">
      <p>{summary}</p>
      <div className="issue-card-actions"><button type="button" className="primary-button" disabled={busy} onClick={onPrimary}>{primaryLabel}</button>{onClose && <button type="button" disabled={busy} onClick={onClose}>Close</button>}</div>
    </div>}
  </article>;
}

function Section({ title, children }: { title: string; children: React.ReactNode }): React.JSX.Element {
  return <section className="issue-section"><header>{title}</header>{children}</section>;
}
