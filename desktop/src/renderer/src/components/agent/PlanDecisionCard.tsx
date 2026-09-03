import { PathText } from '../PathText';
import { useEffect, useId, useRef, useState } from 'react';
import { continuousIsActive } from '../../../../shared/agentCore';
import type { PlannerPlan } from '../../../../shared/agentTypes';
import { useAgentWorkspace } from '../../agent/agentWorkspace';
import { PlanDetails } from './PlanDetails';

export function PlanDecisionCard(): React.JSX.Element {
  const agent = useAgentWorkspace();
  const planner = agent.state.bridge.planner;
  const plan = planner.pending_plan || (planner.execution_paused ? planner.paused_plan : null);
  const [review, setReview] = useState<PlannerPlan | null>(null);
  const [editing, setEditing] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [error, setError] = useState('');
  const [sending, setSending] = useState(false);
  const submitting = useRef(false);
  const reviewVersion = useRef(0);
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const draftPlan = useRef('');
  const labelId = useId();
  const busy = sending || Boolean(agent.state.pendingAction) || Boolean(planner.executing) || continuousIsActive(agent.state.bridge);
  const changed = Boolean(review && JSON.stringify(review) !== JSON.stringify(plan));
  const title = planner.execution_paused ? 'Execution paused' : 'Plan ready';

  useEffect(() => {
    if (review) {
      if (!dialog.current?.open) dialog.current?.showModal();
      if (editing) input.current?.focus();
      else heading.current?.focus();
    } else dialog.current?.close();
  }, [review, editing]);

  const openReview = (suggest = false): void => {
    if (!plan) return;
    const identity = JSON.stringify(plan);
    if (draftPlan.current !== identity) setFeedback('');
    draftPlan.current = identity;
    reviewVersion.current += 1;
    setError('');
    setEditing(suggest);
    setReview(plan);
  };
  // Dismissing a view never cancels or waits for the agent's action.
  const close = (): void => {
    reviewVersion.current += 1;
    setReview(null);
  };
  const act = async (action: string, target: PlannerPlan): Promise<void> => {
    if (busy || submitting.current || (action === 'revise_plan' && !feedback.trim())) return;
    submitting.current = true;
    setSending(true);
    setError('');
    const actionReviewVersion = reviewVersion.current;
    // Approval/resume can await the entire execution, not just acceptance.
    if (action !== 'revise_plan') close();
    try {
      const ok = await agent.plannerAction(action, {
        expected_plan: target,
        ...(action === 'revise_plan' ? { feedback: feedback.trim() } : {}),
        ...(planner.active_issue_id ? { issue_id: planner.active_issue_id } : {}),
      });
      if (ok) {
        // A late response must not dismiss a subsequently reopened review.
        if (reviewVersion.current === actionReviewVersion) { close(); setFeedback(''); }
      }
      else setError('The request failed. Your feedback is saved here; check the agent notice and retry.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The request failed. Your feedback is saved here.');
    } finally {
      submitting.current = false;
      setSending(false);
    }
  };
  const approveAction = planner.execution_paused ? 'continue_issue' : 'approve_plan';
  const approveLabel = planner.execution_paused ? `Resume${planner.resume_checkpoint?.next_goal_index ? ` at goal ${planner.resume_checkpoint.next_goal_index}` : ''}` : 'Approve plan';
  const actions = (target: PlannerPlan): React.JSX.Element => <div className="decision-buttons plan-review-actions">
    <button type="button" className="plan-reject" disabled={busy || changed} onClick={() => void act('reject_plan', target)}>Reject</button>
    <button type="button" disabled={busy || changed} onClick={() => setEditing(true)}>Suggest plan changes</button>
    <button type="button" className="primary-button" disabled={busy || changed} onClick={() => void act(approveAction, target)}>{approveLabel}</button>
  </div>;

  return <>
    {plan && <section className="agent-decision plan-decision">
      <header><span>DECISION</span><strong>{title}</strong><small>{plan.goals?.length || 0} goals</small></header>
      {plan.summary && <p className="plan-preview-summary"><PathText>{plan.summary}</PathText></p>}
      <div className="decision-buttons"><button type="button" className="primary-button" onClick={() => openReview()}>Review full plan ↗</button><button type="button" disabled={busy} onClick={() => openReview(true)}>Suggest plan changes</button></div>
      {!review && error && <p role="alert" className="plan-error"><PathText>{error}</PathText></p>}
    </section>}
    <dialog onClickCapture={(event) => { if ((event.target as Element).closest('a.path-chip')) close(); }} ref={dialog} className="plan-review-dialog" aria-labelledby={labelId}
      onCancel={(event) => { event.preventDefault(); close(); }} onClose={() => {
        // Native close events are queued; ignore one from an earlier opening.
        if (!dialog.current?.open) setReview(null);
      }}>
      {review && <div className="plan-review-layout">
        <header>
          <div><h2 id={labelId} ref={heading} tabIndex={-1}>Plan review</h2><div className="plan-review-subtitle"><span>{review.goals?.length || 0} goals</span>Review before execution</div></div>
          <button type="button" onClick={close} aria-label="Close plan review"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" /></svg></button>
        </header>
        <div className="plan-review-body" tabIndex={0} role="region" aria-label="Full plan details"><PlanDetails plan={review} /></div>
        <footer>
          {changed && !sending && <p role="status">The plan has changed. Close this review and open the current plan before deciding.</p>}
          {error && <p role="alert" className="plan-error"><PathText>{agent.state.notice || error}</PathText> Your feedback is preserved.</p>}
          {editing ? <form onSubmit={(event) => { event.preventDefault(); if (!changed) void act('revise_plan', review); }}>
            <label htmlFor={`${labelId}-feedback`}>What should change?</label>
            <textarea id={`${labelId}-feedback`} ref={input} value={feedback} disabled={sending} onChange={(event) => setFeedback(event.target.value)} rows={3} placeholder="Describe changes to the scope, approach, goals, or success criteria…" />
            <p>The planner will revise this plan for your review. This does not approve execution.</p>
            <div className="decision-buttons"><button type="submit" className="primary-button" disabled={busy || changed || !feedback.trim()}>{sending ? 'Revising plan…' : 'Request revised plan'}</button><button type="button" disabled={sending} onClick={() => setEditing(false)}>Cancel changes</button></div>
          </form> : actions(review)}
        </footer>
      </div>}
    </dialog>
  </>;
}
