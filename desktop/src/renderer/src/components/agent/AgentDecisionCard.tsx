import { PathText } from '../PathText';
import { useRef, useState } from 'react';
import { combineSuggestedActions, continuousIsActive } from '../../../../shared/agentCore';
import type { DiscoveryExtensionRequest, SuggestedAction } from '../../../../shared/agentTypes';
import { useAgentWorkspace } from '../../agent/agentWorkspace';
import { PlanDecisionCard } from './PlanDecisionCard';

export function AgentDecisionCard(): React.JSX.Element | null {
  return <><PlanDecisionCard /><OtherDecisionCard /></>;
}

function OtherDecisionCard(): React.JSX.Element | null {
  const agent = useAgentWorkspace();
  const planner = agent.state.bridge.planner;
  const busy = Boolean(agent.state.pendingAction);
  const locked = continuousIsActive(agent.state.bridge);
  const extension = planner.pending_discovery_extension;
  if (extension) {
    return <DiscoveryExtensionDecisionCard key={extension.request_id} extension={extension} busy={busy || locked}
      onDecision={(accept) => agent.plannerAction(accept ? 'approve_discovery_extension' : 'decline_discovery_extension', { request_id: extension.request_id })} />;
  }
  if (planner.pending_discovery) {
    return <Decision title="Choose discovery depth" meta={planner.pending_discovery.reason || planner.pending_discovery.prompt}>
      <div className="decision-buttons"><button className="primary-button" disabled={busy || locked} onClick={() => void agent.plannerAction('select_discovery_mode', { mode: 'quick' })}>Quick</button><button disabled={busy || locked} onClick={() => void agent.plannerAction('select_discovery_mode', { mode: 'moderate' })}>Moderate</button><button disabled={busy || locked} onClick={() => void agent.plannerAction('select_discovery_mode', { mode: 'deep' })}>Deep</button><button className="quiet" disabled={busy || locked} onClick={() => void agent.plannerAction('skip_discovery')}>Skip</button></div>
    </Decision>;
  }
  const plan = planner.pending_plan || (planner.execution_paused ? planner.paused_plan : null);
  if (plan) return null;
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
  return <section className={`agent-decision ${tone}`}><header><span>DECISION</span><strong>{title}</strong></header>{meta && <p><PathText>{meta}</PathText></p>}<div className="decision-content">{children}</div></section>;
}
function humanize(value: string): string { return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase()); }

export function DiscoveryExtensionDecisionCard({ extension, busy, onDecision }: {
  extension: DiscoveryExtensionRequest;
  busy: boolean;
  onDecision: (accept: boolean) => Promise<boolean>;
}): React.JSX.Element {
  const active = useRef(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const decide = async (accept: boolean): Promise<void> => {
    if (active.current || busy) return;
    active.current = true;
    setSending(true);
    setError('');
    try {
      if (!(await onDecision(accept))) setError('Could not apply the decision. Review the current request and try again.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      active.current = false;
      setSending(false);
    }
  };
  return <Decision title="Continue discovery?" meta={extension.reason} tone="discovery-extension">
      <p>{extension.turns_used}/{extension.turns_max} turns and {extension.tool_calls_used}/{extension.tool_calls_max} tool actions used. Requesting {extension.additional_turns} more {extension.additional_turns === 1 ? 'turn' : 'turns'} and {extension.additional_tool_calls} additional tool actions.</p>
      <p><strong>Proposal</strong><br /><PathText>{extension.proposal}</PathText></p>
      <p><strong>Unresolved questions</strong></p>
      <ul>{extension.ambiguities.map((question, index) => <li key={index}><PathText>{question}</PathText></li>)}</ul>
      <details><summary>Findings so far</summary><p className="discovery-findings"><PathText>{extension.findings}</PathText></p></details>
      <p>Continue this discovery with its existing context, or let the planner proceed with these questions still open.</p>
      {error && <p role="alert"><PathText>{error}</PathText></p>}
      <div className="decision-buttons">
        <button type="button" className="primary-button" disabled={busy || sending} onClick={() => void decide(true)}>Allow {extension.additional_turns} more {extension.additional_turns === 1 ? 'turn' : 'turns'}</button>
        <button type="button" disabled={busy || sending} onClick={() => void decide(false)}>Plan with current findings</button>
      </div>
    </Decision>;
}
