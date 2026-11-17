import { describeProgress } from '../../../../shared/agentCore';
import { useAgentWorkspace } from '../../agent/agentWorkspace';
import { PathChip, PathText } from '../PathText';

/** Uses the surrounding provider so each workspace displays its own agent. */
export function AgentActivityView(): React.JSX.Element {
  const { state } = useAgentWorkspace();
  const activity = [...state.activity].reverse();
  const llm = state.bridge.planner.worker_state?.llm_activity;
  return <div className="dock-scroll activity-view">
    {llm?.in_flight && <div className="live-activity"><i /><strong>Model working</strong><span><PathText>{llm.last_event || `Turn ${llm.turn || ''}`}</PathText></span>{llm.elapsed_s !== undefined && <small>{llm.elapsed_s}s</small>}</div>}
    {!activity.length && !llm?.in_flight && <div className="dock-empty"><span>◇</span><strong>No activity yet</strong><p>Model calls, skills, tools, patches, and goal transitions will appear here.</p></div>}
    {activity.map((item, index) => {
      const line = describeProgress(item);
      return <div className={`activity-row ${line.tone}`} key={`${item.type}-${item.step}-${index}`}>
        <i /><div><strong><PathText>{line.title}</PathText></strong><span>
          {item.path && <PathChip path={item.path} />}
          <PathText>{[item.command, item.elapsed_s !== undefined ? `${item.elapsed_s}s` : ''].filter(Boolean).join(' · ') || (!item.path ? String(item.action_type || item.type).replaceAll('_', ' ') : '')}</PathText>
        </span></div><small>{item.step !== undefined ? `#${item.step}` : item.type.replace('_', ' ')}</small>
      </div>;
    })}
  </div>;
}
