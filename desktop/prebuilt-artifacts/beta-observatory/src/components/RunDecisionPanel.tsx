import type { useRuntime } from '../runtime/useRuntime';
import { pendingDecision } from '../runtime/playbackState';
import { Icon } from './Icon';

const supported = new Set(['approve_plan', 'reject_plan', 'retry_failed_goal', 'resume_execution', 'select_discovery_mode', 'discovery_quick', 'discovery_moderate', 'discovery_deep', 'skip_discovery', 'approve_discovery_extension', 'decline_discovery_extension']);
export interface DecisionReply { reply: string; onReplyChange: (value: string) => void }
interface Props extends DecisionReply { runtime: ReturnType<typeof useRuntime>; location: 'observation' | 'console' }

export function RunDecisionPanel({ runtime, location, reply, onReplyChange }: Props) {
  const { status, run, pending, continueRun } = runtime;
  if (status.activeRunId !== run.id || run.status !== 'awaiting-input') return null;
  const planner = run.state?.planner;
  const plan = planner?.pending_plan || planner?.paused_plan;
  const disabled = pending || Boolean(status.busy);
  const id = `${location}-decision`;
  async function submitAction(action: string) {
    if (disabled) return;
    if (await continueRun({ action })) onReplyChange('');
  }
  async function submitReply() {
    if (disabled || !reply.trim()) return;
    if (await continueRun({ text: reply })) onReplyChange('');
  }
  return <section id={id} className={`decision-panel ${location}-decision`} aria-labelledby={`${id}-title`} aria-busy={pending}>
    <span className="eyebrow accent">AWAITING YOUR INPUT</span>
    <h3 id={`${id}-title`}>{pendingDecision(run)}</h3>
    {plan && <><p className="decision-summary">{plan.summary}</p><ol>{plan.goals?.map((goal, i) => <li key={goal.goal_id || i}><strong>{goal.title || goal.goal}</strong>{goal.success_signals?.map(signal => <p key={signal}>{signal}</p>)}</li>)}</ol></>}
    {planner?.pending_discovery?.reason && <p>{planner.pending_discovery.reason}</p>}
    {planner?.pending_discovery_extension && <><p>{planner.pending_discovery_extension.reason}{planner.pending_discovery_extension.additional_turns != null && ` · ${planner.pending_discovery_extension.additional_turns} additional turns requested.`}</p>{planner.pending_discovery_extension.findings && <p className="response-message">{planner.pending_discovery_extension.findings}</p>}</>}
    {run.message && <p className="response-message">{run.message}</p>}
    {runtime.error && <p className="decision-error" role="alert">{runtime.error}</p>}
    <div className="decision-actions">{planner?.suggested_next_actions?.filter(action => supported.has(action.type)).map(action => <button key={action.type} disabled={disabled} title={action.confirmation_prompt} onClick={() => void submitAction(action.type)}>{action.label || action.type.replaceAll('_', ' ')}<Icon name="arrow" size={14} /></button>)}</div>
    <form className="decision-reply" onSubmit={event => { event.preventDefault(); void submitReply(); }}>
      <label htmlFor={`${id}-reply`}>Clarify, revise, or reply</label>
      <textarea id={`${id}-reply`} maxLength={20000} rows={2} value={reply} onChange={event => onReplyChange(event.target.value)} placeholder="Answer the agent here…" disabled={disabled} />
      <button className="primary" type="submit" disabled={disabled || !reply.trim()}>{pending ? 'Sending…' : 'Send reply'}<Icon name="arrow" size={14} /></button>
    </form>
  </section>;
}
