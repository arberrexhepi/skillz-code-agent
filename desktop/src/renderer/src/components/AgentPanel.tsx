import { useEffect, useRef, useState } from 'react';
import { agentHandoff, continuousIsActive, presentationTranscript } from '../../../shared/agentCore';
import { useAgentWorkspace } from '../agent/agentWorkspace';
import { AgentDecisionCard } from './agent/AgentDecisionCard';
import { AgentHandoffCard } from './agent/AgentHandoffCard';
import { GoalReportDialog } from './agent/GoalReportDialog';
import { MarkdownMessage } from './agent/MarkdownMessage';
import { RuntimeDrawer } from './agent/RuntimeDrawer';

export function AgentPanel(): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [prompt, setPrompt] = useState('');
  const [showRuntime, setShowRuntime] = useState(false);
  const [showGoalReport, setShowGoalReport] = useState(false);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const { bridge, pendingAction, status, notice } = agent.state;
  const continuous = bridge.planner.continuous_mode;
  const transcript = presentationTranscript(bridge);
  const handoff = agentHandoff(bridge);

  useEffect(() => {
    transcriptRef.current?.scrollTo({ top: transcriptRef.current.scrollHeight, behavior: 'smooth' });
  }, [bridge.transcript, bridge.planner.last_execution_summary]);

  const submit = async (): Promise<void> => {
    const text = prompt.trim();
    if (!text || pendingAction) return;
    setPrompt('');
    if (!(await agent.submit(text))) setPrompt(text);
  };

  return (
    <aside className="agent-panel">
      <div className="agent-header">
        <div><span className="eyebrow">WORKSPACE AGENT</span><h2>Agent</h2></div>
        <button type="button" className={`status-pill ${status}`} onClick={() => setShowRuntime((value) => !value)} title="Runtime controls">
          <i />{status}<span className="chevron">⌄</span>
        </button>
      </div>
      <div className="agent-context-strip">
        <span>{bridge.planner.executing ? bridge.planner.executing_goal_title || 'Executing plan' : 'Workspace conversation'}</span>
        {continuousIsActive(bridge) && <strong>Auto {continuous?.cycle || 0}/{continuous?.max_cycles || '∞'}</strong>}
      </div>
      {showRuntime && <RuntimeDrawer onClose={() => setShowRuntime(false)} />}
      <div className="agent-main">
        <div className="transcript" ref={transcriptRef}>
          {transcript.length === 0 && !handoff.discovery && (
            <div className="agent-empty"><span>✦</span><h3>Work from intent.</h3><p>Ask for a change, investigation, or plan. Decisions stay here; execution detail moves into the workspace dock.</p></div>
          )}
          {transcript.map((entry, index) => (
            <article className={`message ${entry.role}`} key={`${entry.role}-${index}`}><header>{entry.role === 'user' ? 'You' : 'Agent'}</header><MarkdownMessage content={entry.content} /></article>
          ))}
          <AgentHandoffCard handoff={handoff} onViewGoalReport={() => setShowGoalReport(true)} />
          {pendingAction && <div className="agent-working"><i /><span>{humanize(pendingAction)}</span></div>}
        </div>
        <AgentDecisionCard />
      </div>
      {notice && <button type="button" className="agent-notice" title={notice} onClick={agent.clearNotice}>{notice}<span>×</span></button>}
      <div className="composer-shell">
        <div className="composer-heading"><span>NEW INSTRUCTION</span><small>Workspace-wide</small></div>
        <div className="prompt-box">
        <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit(); }
        }} placeholder={status === 'running' ? 'Ask the agent…' : 'Describe what you want to build…'} rows={3} />
        <div className="composer-meta"><span>↵ send</span><span>⇧↵ newline</span></div>
        <button type="button" className="send-button" disabled={!prompt.trim() || Boolean(pendingAction)} onClick={() => void submit()}>↑</button>
        </div>
      </div>
      {showGoalReport && <GoalReportDialog handoff={handoff} onClose={() => setShowGoalReport(false)} />}
    </aside>
  );
}

function humanize(value: string): string { return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase()); }
