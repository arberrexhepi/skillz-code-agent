import { continuousIsActive } from '../../../shared/agentCore';
import { useAgentWorkspace } from '../agent/agentWorkspace';

export function AgentTopStatus(): React.JSX.Element {
  const { state, runtime } = useAgentWorkspace();
  const planner = state.bridge.planner;
  const issue = planner.active_issue_id || planner.worker_state?.issue_state?.active_issue_id;
  return <div className="agent-top-status"><span className={`agent-dot ${state.status}`} />{issue && <span>{issue}</span>}<span>{runtime.provider} · {runtime.model}</span>{continuousIsActive(state.bridge) && <strong>AUTO</strong>}{planner.executing && <span>{planner.executing_goal_index || 0}/{planner.executing_goal_count || '?'} goals</span>}</div>;
}
