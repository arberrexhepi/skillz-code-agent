import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { WorkbenchApi } from '../../src/shared/contracts';
import type { CodexSubscriptionStatus } from '../../src/shared/agentTypes';
import { initialAgentUiState } from '../../src/shared/agentCore';
import { AgentWorkspaceContext, type AgentWorkspaceValue } from '../../src/renderer/src/agent/agentWorkspace';
import { RuntimeDrawer } from '../../src/renderer/src/components/agent/RuntimeDrawer';
import '../../src/renderer/src/styles.css';

// Browser-only fixture; native picker, credentials, and persistence are mocked.
let savedPath = '';
let activePath = '';
let running = new URLSearchParams(location.search).has('running');
let cancelNextBrowse = false;
const chosenPath = 'C:\\Users\\Example User\\AppData\\Local\\OpenAI\\Codex\\bin\\runtime\\codex.exe';
const status = (): CodexSubscriptionStatus => ({
  available: Boolean(savedPath), authenticated: false,
  error: savedPath ? undefined : 'Codex CLI was not found. Locate your executable below.',
  cli_path: savedPath || undefined, configured_cli_path: savedPath,
  cli_path_source: savedPath ? 'settings' : 'discovery',
  cli_version: savedPath ? 'codex-cli fixture' : undefined,
  models: savedPath ? ['fixture-model'] : [],
  restart_required: running && activePath !== savedPath,
});
const delay = () => new Promise((resolve) => setTimeout(resolve, 100));
window.workbench = { agent: {
  codexSubscriptionStatus: async () => { await delay(); return status(); },
  codexSubscriptionLogin: async () => { await delay(); if (new URLSearchParams(location.search).has('loginError')) throw new Error('Sign-in was cancelled.'); return { ...status(), authenticated: true }; },
  chooseCodexCli: async () => {
    await delay();
    if (cancelNextBrowse) { cancelNextBrowse = false; return null; }
    return chosenPath;
  },
  setCodexCliPath: async (candidate: string | null) => {
    await delay();
    if (candidate?.includes('invalid')) throw new Error('The selected file does not identify itself as Codex CLI. Choose the Codex executable.');
    savedPath = candidate || '';
    return status();
  },
} } as WorkbenchApi;

function Fixture(): React.JSX.Element {
  const [open, setOpen] = useState(true);
  const [agentRunning, setAgentRunning] = useState(running);
  const value = {
    state: { ...initialAgentUiState, status: agentRunning ? 'running' : 'stopped' },
    runtime: { provider: 'codex-subscription', model: 'fixture-model', backendScript: 'main.py' },
    setRuntime: () => {}, switchRuntime: async () => true, setBackoff: async () => true,
    stop: async () => { running = false; setAgentRunning(false); },
    start: async () => { running = true; activePath = savedPath; setAgentRunning(true); return true; },
  } as AgentWorkspaceValue;
  return <main style={{ position: 'relative', width: 'min(560px, 100%)', height: '100vh', margin: '0 auto' }}>
    <div style={{ padding: 12, display: 'flex', gap: 8 }}>
      <button onClick={() => setOpen(true)}>Open Runtime settings</button>
      <button onClick={() => { cancelNextBrowse = true; }}>Cancel next browse</button>
    </div>
    <AgentWorkspaceContext.Provider value={value}>
      {open && <RuntimeDrawer onClose={() => setOpen(false)} />}
    </AgentWorkspaceContext.Provider>
  </main>;
}
createRoot(document.getElementById('fixture')!).render(<Fixture />);
