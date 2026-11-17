import { useId, useState } from 'react';
import { useAgentWorkspace } from '../agent/agentWorkspace';
import { AgentActivityView } from './agent/AgentActivityView';

export function ArtifactAgentActivity(): React.JSX.Element {
  const { state } = useAgentWorkspace();
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();
  const working = state.bridge.planner.worker_state?.llm_activity?.in_flight || state.bridge.planner.executing || state.pendingAction;
  return <section className="artifact-agent-activity" aria-label="Artifact agent activity" data-expanded={expanded}>
    <button type="button" className="artifact-activity-toggle" aria-expanded={expanded} aria-controls={panelId} onClick={() => setExpanded(value => !value)}>
      <span>Agent activity</span>
      {state.activity.length > 0 && <span className="count-badge">{state.activity.length}</span>}
      <span className={`artifact-activity-status ${working ? 'working' : ''}`}>{working ? 'Working' : state.status}</span>
      <span aria-hidden="true">{expanded ? '⌄' : '⌃'}</span>
    </button>
    <div className="artifact-activity-body" id={panelId} hidden={!expanded}><AgentActivityView /></div>
  </section>;
}
