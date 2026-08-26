import { useEffect, useRef, useState } from 'react';
import type { AgentBridgeState, AgentEvent } from '../../../shared/contracts';

type AgentStatus = 'stopped' | 'starting' | 'running' | 'error';

export function AgentPanel(): React.JSX.Element {
  const [state, setState] = useState<AgentBridgeState>({ planner: {}, transcript: [] });
  const [status, setStatus] = useState<AgentStatus>('stopped');
  const [provider, setProvider] = useState('gemini');
  const [model, setModel] = useState('gemini-3-flash-preview');
  const [backendScript, setBackendScript] = useState('main.py');
  const [prompt, setPrompt] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const transcriptRef = useRef<HTMLDivElement>(null);

  useEffect(() => window.workbench.agent.onEvent((event: AgentEvent) => {
    if (event.type === 'state') setState(event.state);
    if (event.type === 'status') {
      setStatus(event.status);
      if (event.message) setNotice(event.message);
    }
    if (event.type === 'stderr') setNotice(event.message);
  }), []);

  useEffect(() => {
    transcriptRef.current?.scrollTo({ top: transcriptRef.current.scrollHeight, behavior: 'smooth' });
  }, [state.transcript]);

  const start = async (): Promise<boolean> => {
    setBusy(true);
    setNotice('');
    try {
      const response = await window.workbench.agent.start({ provider, model, backendScript });
      setState(response.state);
      setStatus('running');
      return true;
    } catch (cause) {
      setStatus('error');
      setNotice(cleanError(cause));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const submit = async (): Promise<void> => {
    const text = prompt.trim();
    if (!text || busy) return;
    setPrompt('');
    if (status !== 'running' && !(await start())) return;
    setBusy(true);
    try {
      const response = await window.workbench.agent.submit(text);
      setState(response.state);
      if (!response.ok) setNotice(response.message || 'Agent request failed.');
    } catch (cause) {
      setNotice(cleanError(cause));
    } finally {
      setBusy(false);
    }
  };

  const action = async (name: string, extras: Record<string, unknown> = {}): Promise<void> => {
    setBusy(true);
    try {
      const response = await window.workbench.agent.plannerAction(name, extras);
      setState(response.state);
    } catch (cause) {
      setNotice(cleanError(cause));
    } finally {
      setBusy(false);
    }
  };

  const planner = state.planner as Record<string, unknown>;
  const hasPlan = Boolean(planner.pending_plan);
  const pendingDiscovery = planner.pending_discovery as Record<string, unknown> | undefined;

  return (
    <aside className="agent-panel">
      <div className="agent-header">
        <div>
          <span className="eyebrow">SKILLZ</span>
          <h2>Agent</h2>
        </div>
        <span className={`status-pill ${status}`}><i />{status}</span>
      </div>
      <div className="runtime-row">
        <select value={provider} disabled={status === 'running' || busy} onChange={(event) => {
          const next = event.target.value;
          setProvider(next);
          setModel(defaultModels[next] || '');
        }}>
          {Object.keys(defaultModels).map((key) => <option key={key} value={key}>{key}</option>)}
        </select>
        <input value={model} disabled={status === 'running' || busy} onChange={(event) => setModel(event.target.value)} aria-label="Model" />
      </div>
      <div className="runtime-row secondary-runtime">
        <select value={backendScript} disabled={status === 'running' || busy} onChange={(event) => setBackendScript(event.target.value)}>
          <option value="main.py">Stable runtime</option>
          <option value="main_v2.py">Beta TreeLoop</option>
        </select>
        {status === 'running'
          ? <button type="button" className="ghost-button" onClick={() => void window.workbench.agent.stop().then(() => setStatus('stopped'))}>Stop</button>
          : <button type="button" className="ghost-button" disabled={busy || !model.trim()} onClick={() => void start()}>Start</button>}
      </div>

      <div className="transcript" ref={transcriptRef}>
        {state.transcript.length === 0 && (
          <div className="agent-empty">
            <span>✦</span>
            <p>Ask for a change, investigation, or plan. The agent operates directly against this workspace.</p>
          </div>
        )}
        {state.transcript.map((entry, index) => (
          <article className={`message ${entry.role}`} key={`${entry.role}-${index}`}>
            <header>{entry.role === 'user' ? 'You' : 'Agent'}</header>
            <div>{entry.content}</div>
          </article>
        ))}
        {busy && <div className="agent-working"><i /><span>Working</span></div>}
      </div>

      {(hasPlan || pendingDiscovery) && (
        <div className="agent-actions">
          {pendingDiscovery && (
            <>
              <button type="button" disabled={busy} onClick={() => void action('select_discovery_mode', { mode: 'quick' })}>Quick scan</button>
              <button type="button" disabled={busy} onClick={() => void action('select_discovery_mode', { mode: 'moderate' })}>Moderate</button>
              <button type="button" disabled={busy} onClick={() => void action('select_discovery_mode', { mode: 'deep' })}>Deep scan</button>
            </>
          )}
          {hasPlan && (
            <>
              <button type="button" className="primary-button" disabled={busy} onClick={() => void action('approve_plan')}>Approve plan</button>
              <button type="button" disabled={busy} onClick={() => void action('reject_plan')}>Reject</button>
            </>
          )}
        </div>
      )}
      {notice && <div className="agent-notice" title={notice}>{notice}</div>}
      <div className="prompt-box">
        <textarea
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              void submit();
            }
          }}
          placeholder="Ask the agent…"
          rows={3}
        />
        <button type="button" className="send-button" disabled={!prompt.trim() || busy} onClick={() => void submit()}>↑</button>
      </div>
    </aside>
  );
}

const defaultModels: Record<string, string> = {
  gemini: 'gemini-3-flash-preview',
  openai: 'gpt-5.4',
  anthropic: 'claude-sonnet-4-6',
  meta: 'muse-spark-1.2',
  local: 'gemma4',
  'ollama-local': 'qwen3-coder',
};

function cleanError(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '');
}
