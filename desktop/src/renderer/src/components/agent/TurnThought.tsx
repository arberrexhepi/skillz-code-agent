import { PathText } from '../PathText';
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { AgentProgressMessage } from '../../../../shared/agentTypes';
import { liveProgressLabel, modelProgressDetail, type LiveAgentProgress } from '../../../../shared/agentCore';
import { useModelUpdateAge } from './useModelUpdateAge';
import { DraftStrategyDropdown } from './DraftStrategyDropdown';

export function TurnThought({ active, action, thought, progress, reasoning, draftStrategy, agentRestartNeeded }: {
  active: boolean;
  action: string;
  thought?: AgentProgressMessage;
  progress?: LiveAgentProgress;
  reasoning?: AgentProgressMessage;
  draftStrategy?: string[];
  agentRestartNeeded?: boolean;
}): React.JSX.Element | null {
  const [elapsed, setElapsed] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const detailsId = useId();
  const detailsRef = useRef<HTMLDivElement>(null);
  const followSummary = useRef(true);
  const waiting = progress?.action_type === 'model_call_start';
  const showDraft = Boolean(draftStrategy?.length || (waiting && progress.domain !== 'planner'));
  const summary = reasoning?.thought?.trim();
  const note = thought?.thought?.trim();
  const turn = progress?.turn || reasoning?.turn || thought?.turn;
  const planning = (progress || reasoning)?.domain === 'planner';
  const turnLabel = turn ? (planning ? 'Planner call ' : 'Turn ') + turn : '';
  const previousNote = note && thought?.turn && (planning || thought.turn !== turn);
  const updateAge = useModelUpdateAge(progress?.lastModelUpdateAt ?? progress?.modelCallStartedAt);
  const status = progress ? liveProgressLabel(progress, Boolean(summary), updateAge) : '';
  const modelDetail = progress ? modelProgressDetail(progress, updateAge) : '';
  useEffect(() => {
    if (!active) return;
    const started = Date.now();
    setElapsed(0);
    const timer = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [active, action, progress?.step, progress?.action_type, progress?.turn, progress?.domain]);
  useLayoutEffect(() => { followSummary.current = true; }, [reasoning?.turn, waiting]);
  useLayoutEffect(() => {
    const details = detailsRef.current;
    if (details && followSummary.current) details.scrollTop = details.scrollHeight;
  }, [summary, collapsed]);
  const emptySummary = agentRestartNeeded ? 'Restart the agent to enable reasoning summaries.'
    : waiting && !progress.output_started ? 'Waiting for the model to send a reasoning summary.'
      : 'No reasoning summary was received for this turn.';
  const disclosure = (active || summary || agentRestartNeeded) && <div className="turn-thought-reasoning">
    <button type="button" className="turn-thought-disclosure" aria-expanded={!collapsed}
      aria-controls={detailsId} onClick={() => setCollapsed((value) => !value)}>
      Reasoning summary
    </button>
    <div id={detailsId} ref={detailsRef} className="turn-thought-copy" hidden={collapsed} tabIndex={0}
      role="region" aria-label="Reasoning summary"
      onScroll={({ currentTarget }) => { followSummary.current = currentTarget.scrollHeight - currentTarget.scrollTop - currentTarget.clientHeight < 8; }}>
      {summary ? <p><PathText>{summary}</PathText></p> : <p className="turn-thought-empty">{emptySummary}</p>}
    </div>
  </div>;
  if (!active && !note && !summary) return null;
  if (!active) return <details className="turn-thought last-thought">
    <summary>{note ? 'Last turn thought' : 'Last reasoning summary'}{note && thought?.turn ? ' · Turn ' + thought.turn : turnLabel ? ' · ' + turnLabel : ''}</summary>
    <p><PathText>{note || summary}</PathText></p>
    {note && disclosure}
  </details>;
  return <section className="turn-thought live-thought" aria-label="Current turn thought">
    <header>
      <i aria-hidden="true" /><strong>{planning ? 'Planner' : note ? 'Turn thought' : 'Agent activity'}</strong>
      {turnLabel && <span>{turnLabel}</span>}<time>{elapsed}s</time>
    </header>
    {previousNote ? <small>Thought from turn {thought.turn}</small> : null}
    {note && <p className="turn-thought-note" tabIndex={0}><PathText>{note}</PathText></p>}
    {(status || modelDetail) && <div className="turn-thought-progress">
      {status && <div className="turn-thought-status" role="status"><PathText>{status}</PathText></div>}
      {modelDetail && <small>{modelDetail}</small>}
    </div>}
    {(disclosure || showDraft) && <div className="turn-thought-streams">
      {disclosure}
      {showDraft && <DraftStrategyDropdown entries={draftStrategy} />}
    </div>}
    {action && (!status || action.startsWith('Goal ')) && <small className="turn-thought-goal"><PathText>{action.replaceAll('_', ' ')}</PathText></small>}
  </section>;
}
