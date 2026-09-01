import { useEffect, useState } from 'react';
import type { AgentHandoff } from '../../../../shared/agentCore';
import { MarkdownMessage } from './MarkdownMessage';

interface AgentHandoffCardProps {
  handoff: AgentHandoff;
  onViewGoalReport: () => void;
}

export function AgentHandoffCard({ handoff, onViewGoalReport }: AgentHandoffCardProps): React.JSX.Element | null {
  const [discoveryExpanded, setDiscoveryExpanded] = useState(false);
  const discovery = handoff.discovery;
  const discoveryText = String(discovery?.final_message || '');
  const discoveryCanExpand = discoveryText.length > 220;
  const hasGoalReport = handoff.goalResults.length > 0;
  const inProgress = handoff.executionState === 'executing';
  const paused = handoff.executionState === 'paused';
  useEffect(() => setDiscoveryExpanded(false), [discovery?.mode, discoveryText]);
  if (!discovery && !hasGoalReport && !inProgress) return null;

  return (
    <section className="agent-handoff">
      <header><span>{inProgress ? 'WORK IN PROGRESS' : paused ? 'WORK PAUSED' : 'WORK HANDOFF'}</span><strong>{handoff.plan?.summary || 'Workspace update'}</strong></header>
      {inProgress && (
        <div className="handoff-section">
          <h4>Execution progress</h4>
          <div className="handoff-copy"><p>{handoff.completedGoalCount} of {handoff.totalGoalCount || '?'} goals completed. {handoff.currentGoalTitle ? `Now working on ${handoff.currentGoalTitle}.` : 'Preparing the next goal.'}</p></div>
          <div className="handoff-meta"><span>Live plan</span><span>{handoff.totalGoalCount - handoff.completedGoalCount} remaining</span></div>
        </div>
      )}
      {discovery?.final_message && (
        <div className="handoff-section">
          <h4>Discovery handoff</h4>
          <div className={`handoff-copy ${discoveryExpanded ? 'expanded' : ''}`}><MarkdownMessage content={discovery.final_message} /></div>
          {discoveryCanExpand && <button type="button" className="handoff-expand" aria-expanded={discoveryExpanded} onClick={() => setDiscoveryExpanded((value) => !value)}>{discoveryExpanded ? 'Collapse report' : 'Expand report'}</button>}
          <div className="handoff-meta">
            {discovery.mode && <span>{humanize(discovery.mode)} scan</span>}
            {discovery.touched_paths?.length ? <span>{discovery.touched_paths.length} paths inspected</span> : null}
          </div>
        </div>
      )}
      {handoff.nextSteps.length > 0 && (
        <div className="handoff-section">
          <h4>Planner's next suggested steps</h4>
          <ol>{handoff.nextSteps.map((step, index) => <li key={`${step}-${index}`}>{step}</li>)}</ol>
        </div>
      )}
      {hasGoalReport && (
        <footer>
          <span>{inProgress ? `${handoff.completedGoalCount} of ${handoff.totalGoalCount || '?'} goals completed so far` : `${handoff.goalResults.length} goal${handoff.goalResults.length === 1 ? '' : 's'} reported`}</span>
          <button type="button" onClick={onViewGoalReport}>{inProgress ? 'View progress' : 'View goal report'} <i>↗</i></button>
        </footer>
      )}
    </section>
  );
}

function humanize(value: string): string {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
}
