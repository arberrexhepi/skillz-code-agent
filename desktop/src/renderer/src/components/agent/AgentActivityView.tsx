import { describeProgress, liveProgressLabel, modelProgressDetail } from '../../../../shared/agentCore';
import { useAgentWorkspace } from '../../agent/agentWorkspace';
import { PathChip, PathText } from '../PathText';
import { useModelUpdateAge } from './useModelUpdateAge';

/** Uses the surrounding provider so each workspace displays its own agent. */
export function AgentActivityView(): React.JSX.Element {
  const { state } = useAgentWorkspace();
  const activity = [...state.activity].reverse();
  const llm = state.bridge.planner.worker_state?.llm_activity;
  const live = state.liveProgress;
  const updateAge = useModelUpdateAge(live?.lastModelUpdateAt ?? live?.modelCallStartedAt);
  const detail = live ? modelProgressDetail(live, updateAge) : '';
  return <div className="dock-scroll activity-view">
    {live ? <div className="live-activity"><i /><strong><PathText>{liveProgressLabel(live, Boolean(state.reasoningSummary), updateAge)}</PathText></strong>
      {live.turn ? <span>{live.domain === 'planner' ? 'Planner call' : 'Turn'} {live.turn}</span> : null}
      {detail && <small>{detail}</small>}</div>
      : llm?.in_flight && <div className="live-activity"><i /><strong>Model working</strong>{llm.turn && <span>Turn {llm.turn}</span>}</div>}
    {!activity.length && !llm?.in_flight && !live && <div className="dock-empty"><span>◇</span><strong>No activity yet</strong><p>Model calls, skills, tools, patches, and goal transitions will appear here.</p></div>}
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
