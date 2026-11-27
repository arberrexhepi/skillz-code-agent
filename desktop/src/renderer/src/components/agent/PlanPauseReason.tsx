import type { PlannerState } from '../../../../shared/agentTypes';
import { PathText } from '../PathText';
import { VerificationChecklist } from './VerificationChecklist';

export function PlanPauseReason({ planner, onProceed, busy }: {
  planner: PlannerState;
  onProceed?: () => void;
  busy?: boolean;
}): React.JSX.Element | null {
  if (!planner.execution_paused) return null;
  const results = [...(planner.last_completed_results || []), ...(planner.completed_results || [])];
  const paused = results.find(result => result.goal_id === (planner.paused_suspended_goal_id || planner.paused_failed_goal_id))
    || results.find(result => result.status === 'suspended' || result.status === 'failed');
  const reason = planner.paused_failure_reason || paused?.final_message;
  const validation = paused?.validation_summary;
  const suspendedTitle = paused?.status === 'suspended' && (paused.title || planner.resume_checkpoint?.next_goal?.title);
  const checkpointSaved = Boolean(planner.resume_checkpoint?.completed_goal_count);
  if (paused?.verification?.checks?.length) return <div className="plan-pause-verification">
    {suspendedTitle && <p><strong>Suspended goal</strong> <PathText>{suspendedTitle}</PathText></p>}
    <VerificationChecklist report={paused.verification} legacyLog={reason} busy={busy}
      onProceed={paused.goal_id && paused.goal_id === planner.paused_failed_goal_id ? onProceed : undefined} />
    {checkpointSaved && <p>Completed goals are saved. Resume continues from the next incomplete goal.</p>}
  </div>;
  if (!reason && !validation) return null;
  return <div className="plan-pause-reason" role="status">
    {suspendedTitle && <p><strong>Suspended goal</strong><PathText>{suspendedTitle}</PathText></p>}
    {reason && <p><strong>Stop reason</strong><PathText>{reason}</PathText></p>}
    {validation && validation !== reason && <p><strong>Validation</strong><PathText>{validation}</PathText></p>}
    {checkpointSaved && <p>Completed goals are saved. Resume continues from the next incomplete goal.</p>}
  </div>;
}
