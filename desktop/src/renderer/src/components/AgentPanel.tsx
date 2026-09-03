import { PathText } from './PathText';
import { useEffect, useRef, useState } from 'react';
import { agentHandoff, continuousIsActive } from '../../../shared/agentCore';
import { conversationTimeline } from '../../../shared/agentTimeline';
import { useAgentWorkspace } from '../agent/agentWorkspace';
import { AgentDecisionCard } from './agent/AgentDecisionCard';
import { WorkflowReportCard } from './agent/WorkflowReportCard';
import { TurnThought } from './agent/TurnThought';
import { MarkdownMessage } from './agent/MarkdownMessage';
import { RuntimeDrawer } from './agent/RuntimeDrawer';

export function AgentPanel({ label = 'WORKSPACE AGENT' }: { label?: string }): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [prompt, setPrompt] = useState('');
  const [showRuntime, setShowRuntime] = useState(false);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const { bridge, pendingAction, status, notice } = agent.state;
  const continuous = bridge.planner.continuous_mode;
  const timeline = conversationTimeline(bridge);
  const handoff = agentHandoff(bridge);
  const workingLabel = bridge.planner.executing
    ? `Goal ${bridge.planner.executing_goal_index || handoff.completedGoalCount + 1}/${bridge.planner.executing_goal_count || handoff.totalGoalCount || '?'}: ${bridge.planner.executing_goal_title || 'Executing plan'}`
    : pendingAction;

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
        <div><span className="eyebrow">{label}</span><h2>Agent</h2></div>
        <button type="button" className={`status-pill ${status}`} onClick={() => setShowRuntime((value) => !value)} title="Runtime controls">
          <i />{status}<span className="chevron">⌄</span>
        </button>
      </div>
      <div className="agent-context-strip">
        <span><PathText>{bridge.planner.executing ? bridge.planner.executing_goal_title || 'Executing plan' : label === 'ARTIFACT AGENT' ? 'Artifact conversation' : 'Workspace conversation'}</PathText></span>
        {continuousIsActive(bridge) && <strong>Auto {continuous?.cycle || 0}/{continuous?.max_cycles || '∞'}</strong>}
      </div>
      {showRuntime && <RuntimeDrawer onClose={() => setShowRuntime(false)} />}
      <div className="agent-main">
        <div className="transcript" ref={transcriptRef}>
          {timeline.length === 0 && !handoff.discovery && (
            <div className="agent-empty"><span>✦</span><h3>Work from intent.</h3><p>Ask for a change, investigation, or plan. Decisions stay here; execution detail moves into the workspace dock.</p></div>
          )}
          {timeline.map((item) => item.kind === 'workflow'
            ? <WorkflowReportCard key={item.id} report={item} />
            : <article className={`message ${item.entry.role}`} key={item.id}><header>{item.entry.role === 'user' ? 'You' : 'Agent'}</header><MarkdownMessage content={item.entry.content} /></article>)}
        </div>
        <TurnThought active={Boolean(pendingAction || bridge.planner.executing)} action={workingLabel} thought={agent.state.turnThought} />
        <AgentDecisionCard />
      </div>
      {notice && <div className="agent-notice" role="status"><div className="notice-copy"><PathText>{notice}</PathText></div><button type="button" aria-label="Dismiss notice" onClick={agent.clearNotice}>×</button></div>}
      <div className="composer-shell">
        <div className="composer-heading"><span>NEW INSTRUCTION</span><small>{label === 'ARTIFACT AGENT' ? 'This artifact' : 'Workspace-wide'}</small></div>
        <div className="prompt-box">
        <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit(); }
        }} placeholder={status === 'running' ? 'Ask the agent…' : 'Describe what you want to build…'} rows={3} />
        <div className="composer-meta"><span>↵ send</span><span>⇧↵ newline</span></div>
        <button type="button" className="send-button" disabled={!prompt.trim() || Boolean(pendingAction)} onClick={() => void submit()}>↑</button>
        </div>
      </div>
    </aside>
  );
}
