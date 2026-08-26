import type { AgentHandoff } from '../../../../shared/agentCore';
import { MarkdownMessage } from './MarkdownMessage';

interface AgentHandoffCardProps {
  handoff: AgentHandoff;
  onViewGoalReport: () => void;
}

export function AgentHandoffCard({ handoff, onViewGoalReport }: AgentHandoffCardProps): React.JSX.Element | null {
  const discovery = handoff.discovery;
  const hasGoalReport = handoff.goalResults.length > 0;
  if (!discovery && !hasGoalReport) return null;

  return (
    <section className="agent-handoff">
      <header><span>WORK HANDOFF</span><strong>{handoff.plan?.summary || 'Workspace update'}</strong></header>
      {discovery?.final_message && (
        <div className="handoff-section">
          <h4>Discovery handoff</h4>
          <div className="handoff-copy"><MarkdownMessage content={discovery.final_message} /></div>
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
          <span>{handoff.goalResults.length} goal{handoff.goalResults.length === 1 ? '' : 's'} reported</span>
          <button type="button" onClick={onViewGoalReport}>View goal report <i>↗</i></button>
        </footer>
      )}
    </section>
  );
}

function humanize(value: string): string {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
}
