import type { PlannerState } from '../../../../shared/agentTypes';
import { PathText } from '../PathText';

export function PlanPauseReason({ planner }: { planner: PlannerState }): React.JSX.Element | null {
  if (!planner.execution_paused) return null;
  const results = [...(planner.last_completed_results || []), ...(planner.completed_results || [])];
  const failed = results.find(result => result.goal_id === planner.paused_failed_goal_id)
    || results.find(result => result.status === 'failed');
  const reason = planner.paused_failure_reason || failed?.final_message;
  const validation = failed?.validation_summary;
  if (!reason && !validation) return null;
  return <div className="plan-pause-reason" role="status">
    {reason && <p><strong>Stop reason</strong><PathText>{reason}</PathText></p>}
    {validation && validation !== reason && <p><strong>Validation</strong><PathText>{validation}</PathText></p>}
    {Boolean(planner.resume_checkpoint?.completed_goal_count) && <p>Completed goals are saved. Resume continues from the next incomplete goal.</p>}
  </div>;
}
