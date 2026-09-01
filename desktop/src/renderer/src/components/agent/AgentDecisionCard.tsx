import { combineSuggestedActions, continuousIsActive } from '../../../../shared/agentCore';
import type { SuggestedAction } from '../../../../shared/agentTypes';
import { useAgentWorkspace } from '../../agent/agentWorkspace';

export function AgentDecisionCard(): React.JSX.Element | null {
  const agent = useAgentWorkspace();
  const planner = agent.state.bridge.planner;
  const busy = Boolean(agent.state.pendingAction);
  const locked = continuousIsActive(agent.state.bridge);
  if (planner.pending_discovery) {
    return <Decision title="Choose discovery depth" meta={planner.pending_discovery.reason || planner.pending_discovery.prompt}>
      <div className="decision-buttons"><button className="primary-button" disabled={busy || locked} onClick={() => void agent.plannerAction('select_discovery_mode', { mode: 'quick' })}>Quick</button><button disabled={busy || locked} onClick={() => void agent.plannerAction('select_discovery_mode', { mode: 'moderate' })}>Moderate</button><button disabled={busy || locked} onClick={() => void agent.plannerAction('select_discovery_mode', { mode: 'deep' })}>Deep</button><button className="quiet" disabled={busy || locked} onClick={() => void agent.plannerAction('skip_discovery')}>Skip</button></div>
    </Decision>;
  }
  const plan = planner.pending_plan || (planner.execution_paused ? planner.paused_plan : null);
  if (plan) {
    const next = planner.resume_checkpoint?.next_goal_index;
    return <Decision title={planner.execution_paused ? 'Execution paused' : 'Plan ready'} meta={plan.summary}>
      {plan.goals?.slice(0, 3).map((goal, index) => <div className="decision-goal" key={goal.goal_id || index}><span>{index + 1}</span>{goal.title || goal.goal}</div>)}
      <div className="decision-buttons"><button className="primary-button" disabled={busy || locked} onClick={() => void agent.plannerAction(planner.execution_paused ? 'continue_issue' : 'approve_plan', planner.active_issue_id ? { issue_id: planner.active_issue_id } : {})}>{planner.execution_paused ? `Resume${next ? ` at goal ${next}` : ''}` : 'Approve plan'}</button><button disabled={busy || locked} onClick={() => void agent.plannerAction('reject_plan')}>Reject</button></div>
    </Decision>;
  }
  const error = planner.worker_state?.active_error;
  const elicitationActions = [
    ...combineSuggestedActions(agent.state.bridge),
    ...(error?.suggested_next_actions || []).map((action) => ({ ...action, source: 'worker' as const })),
  ].filter(isExplicitElicitation).filter((action, index, actions) => actions.findIndex((candidate) => candidate.type === action.type && candidate.confirmation_prompt === action.confirmation_prompt) === index);
  if (!busy && !locked && elicitationActions.length) {
    const prompt = elicitationActions.find((action) => action.confirmation_prompt)?.confirmation_prompt;
    return <Decision title={error ? 'Agent needs direction' : 'Permission required'} meta={prompt || error?.message || error?.error_type} tone={error ? 'danger' : ''}><ActionButtons actions={elicitationActions.slice(0, 4)} /></Decision>;
  }
  return null;
}

function isExplicitElicitation(action: SuggestedAction): boolean {
  if (['approve_npm_command', 'reject_npm_command'].includes(action.type)) return true;
  if (['delete_session', 'drop_context'].includes(action.type)) return false;
  return action.source === 'worker' && action.requires_confirmation === true;
}

function ActionButtons({ actions }: { actions: SuggestedAction[] }): React.JSX.Element {
  const agent = useAgentWorkspace();
  return <div className="decision-buttons">{actions.map((action, index) => <button type="button" key={`${action.source}-${action.type}-${index}`} className={action.style === 'primary' ? 'primary-button' : action.style === 'ghost' ? 'quiet' : ''} disabled={Boolean(agent.state.pendingAction)} onClick={() => void agent.runSuggestedAction(action)}>{action.label || humanize(action.type)}</button>)}</div>;
}

function Decision({ title, meta, tone = '', children }: { title: string; meta?: string; tone?: string; children: React.ReactNode }): React.JSX.Element {
  return <section className={`agent-decision ${tone}`}><header><span>DECISION</span><strong>{title}</strong></header>{meta && <p>{meta}</p>}<div className="decision-content">{children}</div></section>;
}
function humanize(value: string): string { return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase()); }
