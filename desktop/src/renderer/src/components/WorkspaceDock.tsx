import { useMemo, useState } from 'react';
import { currentReview, describeProgress, groupDiagnostics, primaryReviewPath } from '../../../shared/agentCore';
import { useAgentWorkspace } from '../agent/agentWorkspace';
import { TerminalPanel } from './TerminalPanel';

type DockMode = 'terminal' | 'activity' | 'problems' | 'review';

export function WorkspaceDock({ onOpenPath, onOpenDiff }: { onOpenPath: (path: string) => void; onOpenDiff: (path: string, staged: boolean) => void }): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [mode, setMode] = useState<DockMode>('terminal');
  const [collapsed, setCollapsed] = useState(false);
  const diagnostics = useMemo(() => groupDiagnostics(agent.state.bridge), [agent.state.bridge]);
  const problemCount = Object.values(diagnostics).reduce((sum, values) => sum + values.length, 0);
  const review = currentReview(agent.state.bridge);
  const tabs: Array<{ key: DockMode; label: string; count?: number }> = [
    { key: 'terminal', label: 'Terminal' },
    { key: 'activity', label: 'Activity', count: agent.state.activity.length },
    { key: 'problems', label: 'Problems', count: problemCount },
    { key: 'review', label: 'Review', count: review ? 1 : 0 },
  ];
  return <section className={`workspace-dock ${collapsed ? 'collapsed' : ''}`}>
    <div className="dock-tabs">{tabs.map((tab) => <button type="button" key={tab.key} className={mode === tab.key ? 'active' : ''} onClick={() => { setMode(tab.key); setCollapsed(false); }}>{tab.label}{tab.count ? <span>{tab.count}</span> : null}</button>)}<button type="button" className="icon-button push-right" onClick={() => setCollapsed((value) => !value)}>{collapsed ? '⌃' : '⌄'}</button></div>
    {!collapsed && <div className="dock-body">
      <div className={`dock-pane ${mode === 'terminal' ? '' : 'hidden'}`}><TerminalPanel embedded /></div>
      {mode === 'activity' && <div className="dock-pane"><ActivityView onOpenPath={onOpenPath} /></div>}
      {mode === 'problems' && <div className="dock-pane"><ProblemsView diagnostics={diagnostics} onOpenPath={onOpenPath} /></div>}
      {mode === 'review' && <div className="dock-pane"><ReviewView onOpenDiff={onOpenDiff} /></div>}
    </div>}
  </section>;
}

function ActivityView({ onOpenPath }: { onOpenPath: (path: string) => void }): React.JSX.Element {
  const { state } = useAgentWorkspace();
  const activity = [...state.activity].reverse();
  const llm = state.bridge.planner.worker_state?.llm_activity;
  return <div className="dock-scroll activity-view">
    {llm?.in_flight && <div className="live-activity"><i /><strong>Model working</strong><span>{llm.last_event || `Turn ${llm.turn || ''}`}</span>{llm.elapsed_s !== undefined && <small>{llm.elapsed_s}s</small>}</div>}
    {!activity.length && <DockEmpty title="No activity yet" body="Model calls, skills, tools, patches, and goal transitions will appear here." />}
    {activity.map((item, index) => { const line = describeProgress(item); return <button type="button" className={`activity-row ${line.tone}`} key={`${item.type}-${item.step}-${index}`} onClick={() => item.path && onOpenPath(item.path)} disabled={!item.path}><i /><div><strong>{line.title}</strong><span>{line.detail || String(item.action_type || item.type).replaceAll('_', ' ')}</span></div><small>{item.step !== undefined ? `#${item.step}` : item.type.replace('_', ' ')}</small></button>; })}
  </div>;
}

function ProblemsView({ diagnostics, onOpenPath }: { diagnostics: ReturnType<typeof groupDiagnostics>; onOpenPath: (path: string) => void }): React.JSX.Element {
  const entries = Object.entries(diagnostics);
  return <div className="dock-scroll problems-view">{!entries.length && <DockEmpty title="No current problems" body="Agent diagnostics will also become editor markers." />}{entries.flatMap(([path, items]) => items.map((item, index) => <button type="button" className="problem-row" key={`${path}-${index}`} onClick={() => onOpenPath(path)}><span className="problem-icon">!</span><div><strong>{item.message || item.code || 'Diagnostic'}</strong><span>{path}{item.line ? `:${item.line}${item.column ? `:${item.column}` : ''}` : ''}</span></div><small>{item.code}</small></button>))}</div>;
}

function ReviewView({ onOpenDiff }: { onOpenDiff: (path: string, staged: boolean) => void }): React.JSX.Element {
  const { state } = useAgentWorkspace();
  const review = currentReview(state.bridge);
  if (!review) return <DockEmpty title="Nothing to review" body="Structural reviews and generated diffs will collect here." />;
  const path = primaryReviewPath(review);
  return <div className="dock-scroll review-view"><div className="review-summary"><span>AGENT REVIEW</span><h3>{review.summary || String(review.action_type || 'Changes ready')}</h3>{review.stat && <pre>{review.stat}</pre>}<div className="review-meta">{review.high_risk_paths?.length ? <strong>{review.high_risk_paths.length} high-risk path{review.high_risk_paths.length === 1 ? '' : 's'}</strong> : <span>No high-risk paths reported</span>}{path && <button className="primary-button" onClick={() => onOpenDiff(path, Boolean(review.staged))}>Open diff</button>}</div></div>{review.diff && <pre className="review-diff">{review.diff}</pre>}</div>;
}

function DockEmpty({ title, body }: { title: string; body: string }): React.JSX.Element { return <div className="dock-empty"><span>◇</span><strong>{title}</strong><p>{body}</p></div>; }
