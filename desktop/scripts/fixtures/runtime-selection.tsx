// Real shared React controls with independent, offline repository/artifact APIs.
// No model requests, authentication files, Docker, or workspace files are used.
import { useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { AgentWorkspaceProvider } from '../../src/renderer/src/agent/AgentWorkspaceContext';
import { useAgentWorkspace } from '../../src/renderer/src/agent/agentWorkspace';
import { RuntimeDrawer } from '../../src/renderer/src/components/agent/RuntimeDrawer';
import type { AgentEvent, AgentStartOptions, WorkbenchApi } from '../../src/shared/contracts';
import '../../src/renderer/src/styles.css';

const state = { planner: {}, transcript: [] };
const codexStatus = async () => ({ available: true, authenticated: true, models: ['gpt-5.4', 'gpt-5.6-terra'] });
window.workbench = { agent: { codexSubscriptionStatus: codexStatus } } as WorkbenchApi;

function Controls(): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [open, setOpen] = useState(true);
  return <>
    <p>Selected: {agent.runtime.provider} / {agent.runtime.model} / {agent.runtime.backendScript}</p>
    <p>Status: {agent.state.status}</p>
    {agent.state.notice && <p role="alert">{agent.state.notice}</p>}
    {open ? <RuntimeDrawer onClose={() => setOpen(false)} /> : <button onClick={() => setOpen(true)}>Open runtime</button>}
    <button disabled={Boolean(agent.state.pendingAction)} onClick={() => void agent.submit('Original prompt ë')}>Send original request</button>
  </>;
}
function Scenario({ name }: { name: string }): React.JSX.Element {
  const [calls, setCalls] = useState<string[]>([]);
  const fixture = useMemo(() => {
    let emit: ((event: AgentEvent) => void) | undefined;
    let runtime: AgentStartOptions | undefined;
    let reject = false;
    let release: (() => void) | undefined;
    let lookups = 0;
    const record = (text: string) => setCalls(current => [...current, text]);
    const response = () => ({ ok: true, state });
    const api = {
      onEvent: (listener: (event: AgentEvent) => void) => { emit = listener; return () => { emit = undefined; }; },
      start: async (selection: AgentStartOptions) => {
        runtime = selection;
        record(`start ${selection.provider} / ${selection.model} / ${selection.backendScript}`);
        emit?.({ type: 'status', status: 'running' });
        return response();
      },
      submit: async () => { record(`send ${runtime?.provider} / ${runtime?.model} / ${runtime?.backendScript}`); return response(); },
      stop: async () => { record('stop'); emit?.({ type: 'status', status: 'stopped' }); },
      runtimeOptions: async () => {
        if (lookups++ === 0) await new Promise<void>(resolve => { release = resolve; });
        // Intentionally stale even after a successful start/switch.
        return { current_provider: 'gemini', current_model: 'gemini-3-flash-preview', providers: [] };
      },
      reconfigureRuntime: async (provider: string, model: string) => {
        if (reject) { reject = false; record(`rejected ${provider}`); return { ok: false, state, message: 'Fixture provider is unavailable.' }; }
        runtime = { ...runtime!, provider, model };
        record(`apply ${provider} / ${model}`);
        return response();
      },
      configureBackoff: async () => response(),
    } as WorkbenchApi['agent'];
    return { api, release: () => { release?.(); record('released old Gemini lookup'); }, fail: () => { reject = true; } };
  }, []);
  return <section aria-label={name} style={{ padding: 24, width: 520, border: '1px solid #455' }}>
    <h2>{name}</h2>
    <AgentWorkspaceProvider api={fixture.api}><Controls /></AgentWorkspaceProvider>
    <hr /><button onClick={fixture.release}>Release earlier runtime lookup</button><button onClick={fixture.fail}>Fail next apply</button>
    <pre aria-label={`${name} calls`}>{calls.join('\n')}</pre>
  </section>;
}
createRoot(document.getElementById('root')!).render(<div style={{ display: 'flex', gap: 24, padding: 24, overflow: 'auto', height: '100vh' }}><Scenario name="Repository agent" /><Scenario name="Artifact agent" /></div>);
