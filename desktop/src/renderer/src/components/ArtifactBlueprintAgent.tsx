import { useState } from 'react';
import type { ArtifactBlueprintChatMessage, ArtifactSetupSelection } from '../../../shared/artifacts';

export function ArtifactBlueprintAgent({ id, selection, active, onChanged }: { id: string; selection: ArtifactSetupSelection; active: boolean; onChanged: () => void }): React.JSX.Element {
  const [messages, setMessages] = useState<ArtifactBlueprintChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit() {
    const message = input.trim();
    if (!message || busy) return;
    const history = messages.slice(-20);
    setMessages((current) => [...current, { role: 'user', content: message }]);
    setInput(''); setBusy(true); setError('');
    try {
      const result = await window.workbench.artifacts.blueprintAgent(id, message, history, selection);
      const detail = result.changes.length ? result.message + '\n\n' + result.changes.join('\n') : result.message;
      setMessages((current) => [...current, { role: 'assistant', content: detail }]);
      if (result.changes.length) onChanged();
    } catch (cause) { setError(clean(cause)); }
    finally { setBusy(false); }
  }

  return <section className="artifact-blueprint-agent" hidden={!active} aria-label="API Blueprint agent">
    <header><div><span className="eyebrow">ARTIFACT AGENT</span><strong>Blueprint mode</strong></div><span className="artifact-blueprint-scope">API configuration only</span></header>
    <div className="artifact-blueprint-thread">
      {!messages.length && <div className="artifact-blueprint-intro"><span>⌘</span><h3>Manage API Blueprints with the agent</h3><p>Ask it to inspect, add, change, or remove requests. This mode validates Blueprint configuration and cannot edit the artifact app.</p><div><button type="button" onClick={() => setInput('Summarize the current API Blueprints and identify any missing descriptions, examples, or tests.')}>Review blueprints</button><button type="button" onClick={() => setInput('Create an API Blueprint for ')}>Create a request</button></div></div>}
      {messages.map((message, index) => <article className={'artifact-blueprint-message ' + message.role} key={index}><span>{message.role === 'user' ? 'You' : 'Blueprint agent'}</span><p>{message.content}</p></article>)}
      {busy && <p className="artifact-blueprint-thinking" role="status">Reviewing the validated Blueprint configuration…</p>}
    </div>
    {error && <p className="artifacts-error" role="alert">{error}</p>}
    <form className="artifact-blueprint-composer" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <label htmlFor={'blueprint-agent-' + id}>New instruction</label>
      <textarea id={'blueprint-agent-' + id} rows={3} value={input} disabled={busy} placeholder="Add a GET request for the GitHub repository issues endpoint…" onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit(); } }} />
      <div><span>Enter to send · Shift+Enter for newline</span><button className="primary-button" disabled={busy || !input.trim()} aria-label="Send Blueprint instruction">↑</button></div>
    </form>
  </section>;
}

function clean(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '');
}