import type { WorkflowItem } from '../../../../shared/agentTimeline';
import { MarkdownMessage } from './MarkdownMessage';

export function WorkflowReportCard({ report }: { report: WorkflowItem }): React.JSX.Element {
  const goals = report.goals || [];
  const outcome = report.discovery?.final_message;
  const status = report.status === 'offered' ? 'Proposed' : report.status === 'complete' ? 'Completed' : report.status;
  return <details className={`workflow-report ${report.status}`}>
    <summary>
      <span className="workflow-chevron" aria-hidden="true">›</span>
      <span className="workflow-title">{report.title}</span>
      {report.selection && <span className="workflow-choice">{report.selection}</span>}
      <span className="workflow-status">{status}</span>
    </summary>
    {(report.summary || report.plan?.summary) && <p className="workflow-overview">{report.summary || report.plan?.summary}</p>}
    <div className="workflow-report-body">
      {report.currentGoal && <p>Now: {report.currentGoal} · {goals.filter((goal) => goal.status === 'completed').length}/{report.plan?.goals?.length || '?'} goals completed</p>}
      <ol className="workflow-events" aria-label="Workflow history">
        {report.events.map((event, index) => <li key={index}>
          <strong>{event.role === 'user' ? 'You' : 'Agent'}</strong>
          <span>{event.selection || event.status}{event.discovery?.mode ? ` · ${event.discovery.mode} scan` : ''}{event.goals?.length ? ` · ${event.goals[0].title || event.goals[0].goal_id}` : ''}</span>
        </li>)}
      </ol>
      {report.plan?.goals?.length ? <section><h4>Planned goals</h4><ol>{report.plan.goals.map((goal, index) => <li key={goal.goal_id || index}>{goal.title || goal.goal}{goal.success_signals?.length ? <ul>{goal.success_signals.map((signal) => <li key={signal}>{signal}</li>)}</ul> : null}</li>)}</ol></section> : null}
      {outcome && <section><h4>Discovery report</h4><MarkdownMessage content={outcome} />{report.discovery?.touched_paths?.length ? <p className="workflow-paths">{report.discovery.touched_paths.join(' · ')}</p> : null}</section>}
      {goals.map((goal, index) => <section key={`${goal.goal_id}-${index}`}><h4>{goal.title || goal.goal_id} · {goal.status}</h4>{goal.final_message && <MarkdownMessage content={goal.final_message} />}{goal.validation_summary && <p>Validation: {goal.validation_summary}</p>}{goal.touched_paths?.length ? <p className="workflow-paths">{goal.touched_paths.join(' · ')}</p> : null}</section>)}
      <details className="workflow-original"><summary>Original workflow text</summary>{report.events.map((event, index) => <section key={index}><h4>{event.role === 'user' ? 'You selected' : 'Agent report'}</h4><MarkdownMessage content={event.content} /></section>)}</details>
    </div>
  </details>;
}
