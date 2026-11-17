import { useState } from 'react';
import type { useRuntime } from '../runtime/useRuntime';
import { Icon } from './Icon';
import { RunRepositoryPicker } from './RunRepositoryPicker';
import { RunDecisionPanel, type DecisionReply } from './RunDecisionPanel';
export function RunConsole({ runtime, reply, onReplyChange }: { runtime: ReturnType<typeof useRuntime> } & DecisionReply) {
  const { status, run, pending, start, stop, select, configure } = runtime;
  const repositoryAvailable = !status.repositories || status.repositories.some(item => item.id === runtime.repositoryId);
  const canRun = status.available && status.ready !== false && repositoryAvailable;
  const [prompt, setPrompt] = useState('');
  const active = status.activeRunId === run.id;
  const waiting = active && run.status === 'awaiting-input';
  const working = active && run.status === 'running';
  async function submit() {
    if (!prompt.trim()) return;
    await start(prompt);
  }
  return <section className="run-console" id="run-console">
    <div className="console-title"><div><Icon name="terminal" /><h2>Put the loop in motion.</h2></div><span className={`runtime-badge ${status.available ? 'connected' : ''}`}><span className="signal" />{status.available ? `${status.selection?.provider} / ${status.selection?.model}` : 'Runtime not connected'}</span></div>
    <p className="console-description">Your configured model. An isolated beta worker. A trace you can inspect and replay.</p>
    {!status.available && <div className="connection-note">{status.error || 'Open this prebuilt in the Workbench, enable Allow agent runtime in File access, and restart its preview. The sample remains available here.'}</div>}
    {status.available && (status.ready === false || status.setupAvailable) && <div className="connection-note">
      {status.ready === false ? status.setupMessage || 'The agent runtime needs setup before starting a live run.' : 'Runtime settings are managed by Workbench.'}
      {status.setupAvailable && <button disabled={pending} onClick={() => void configure()}>Configure runtime <Icon name="arrow" size={14} /></button>}
    </div>}
    {status.activeRunId && !active && <div className="connection-note">Another recording is active. <button onClick={() => select(status.activeRunId!)}>Open active run <Icon name="arrow" size={14} /></button></div>}
    <RunRepositoryPicker runtime={runtime} />
    <RunDecisionPanel runtime={runtime} location="console" reply={reply} onReplyChange={onReplyChange} />
    {!waiting && <form onSubmit={event => { event.preventDefault(); void submit(); }}><label className="sr-only" htmlFor="run-prompt">New run prompt</label><textarea id="run-prompt" maxLength={20000} rows={2} value={prompt} onChange={event => setPrompt(event.target.value)} placeholder='Give the beta a task. For example: inspect the shared repository and explain its entry points…' disabled={!canRun || working || pending || Boolean(status.activeRunId && !active)} /><div className="composer-footer"><span><span className="mono">BETA</span> · Uses model usage and this artifact’s file grants</span>{working ? <span className="working-text"><span className="signal live" /> Recording runtime events</span> : <button className="primary" type="submit" disabled={!canRun || pending || Boolean(status.busy) || !prompt.trim() || Boolean(status.activeRunId && !active)}>{pending ? 'Connecting…' : 'Start a live run'}<Icon name="arrow" size={16} /></button>}</div></form>}
    {active && <button className="stop-run" disabled={pending} onClick={() => void stop()}><Icon name="stop" size={13} /> Stop this run</button>}
    {!active && !run.sample && run.message && <div className="run-outcome"><span className="eyebrow">{run.status.replace('-', ' ')}</span><p>{run.message}</p></div>}
  </section>;
}
