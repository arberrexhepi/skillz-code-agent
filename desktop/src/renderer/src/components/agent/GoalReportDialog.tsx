import { PathText } from '../PathText';
import { useEffect, useRef } from 'react';
import type { AgentHandoff } from '../../../../shared/agentCore';
import { MarkdownMessage } from './MarkdownMessage';

interface GoalReportDialogProps {
  handoff: AgentHandoff;
  onClose: () => void;
}

export function GoalReportDialog({ handoff, onClose }: GoalReportDialogProps): React.JSX.Element {
  const closeRef = useRef<HTMLButtonElement>(null);
  const inProgress = handoff.executionState === 'executing';

  useEffect(() => {
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <div className="goal-report-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section onClickCapture={(event) => { if ((event.target as Element).closest('a.path-chip')) onClose(); }} className="goal-report-dialog" role="dialog" aria-modal="true" aria-labelledby="goal-report-title">
        <header>
          <div><span>{inProgress ? 'GOAL PROGRESS' : 'GOAL REPORT'}</span><h3 id="goal-report-title"><PathText>{handoff.plan?.summary || (inProgress ? 'Work in progress' : 'Completed work')}</PathText></h3></div>
          <button ref={closeRef} type="button" className="icon-button" aria-label="Close goal report" onClick={onClose}>×</button>
        </header>
        <div className="goal-report-body">
          {inProgress && <div className="goal-report-summary"><h4>In progress</h4><p>{handoff.completedGoalCount} of {handoff.totalGoalCount || '?'} goals completed. <PathText>{handoff.currentGoalTitle ? `Currently executing ${handoff.currentGoalTitle}.` : 'Preparing the next goal.'}</PathText></p></div>}
          {handoff.executionSummary && <div className="goal-report-summary"><h4>{handoff.executionState === 'paused' ? 'Pause summary' : 'Final summary'}</h4><MarkdownMessage content={handoff.executionSummary} /></div>}
          <div className="goal-report-list">
            {handoff.goalResults.map((result, index) => {
              const complete = result.status === 'completed';
              return (
                <article className="goal-report-item" key={result.goal_id || index}>
                  <header><span className={complete ? 'complete' : 'failed'}>{complete ? '✓' : '!'}</span><div><small>GOAL {index + 1}</small><strong><PathText>{result.title || result.goal_id || 'Untitled goal'}</PathText></strong></div><em>{result.status || 'unknown'}</em></header>
                  {result.final_message && <MarkdownMessage content={result.final_message} />}
                  <dl>
                    {result.validation_summary && <><dt>Validation</dt><dd><PathText>{result.validation_summary}</PathText></dd></>}
                    {result.touched_paths?.length ? <><dt>Touched paths</dt><dd><PathText>{result.touched_paths.join(' · ')}</PathText></dd></> : null}
                    {result.duration_s !== undefined && <><dt>Duration</dt><dd>{result.duration_s.toFixed(1)}s</dd></>}
                  </dl>
                </article>
              );
            })}
          </div>
        </div>
      </section>
    </div>
  );
}
