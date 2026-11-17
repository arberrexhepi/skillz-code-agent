import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { WorkbenchApi } from '../../src/shared/contracts';
import type { CodexSubscriptionStatus } from '../../src/shared/agentTypes';
import { initialAgentUiState } from '../../src/shared/agentCore';
import { AgentWorkspaceContext, type AgentWorkspaceValue } from '../../src/renderer/src/agent/agentWorkspace';
import { RuntimeDrawer } from '../../src/renderer/src/components/agent/RuntimeDrawer';
import { ArtifactRuntimeSetup } from '../../src/renderer/src/components/ArtifactRuntimeSetup';
import '../../src/renderer/src/styles.css';

// Browser-only fixture; native picker, credentials, and persistence are mocked.
let savedPath = '';
let signedIn = false;
const ignoreReady = () => {};
const artifactMode = new URLSearchParams(location.search).has('artifact');
const record = { id: 'beta-observatory', root: '/fixture/beta-observatory', title: 'Beta Observatory', prompt: 'Inspect the beta loop', createdAt: new Date().toISOString(), sourceRoot: '', shareFacts: false, shareMemory: false, contextMode: 'none' as const, runtime: { provider: 'codex-subscription', model: 'fixture-model', backendScript: 'main_v2.py' as const } };
let activePath = '';
let running = new URLSearchParams(location.search).has('running');
let cancelNextBrowse = false;
const chosenPath = 'C:\\Users\\Example User\\AppData\\Local\\OpenAI\\Codex\\bin\\runtime\\codex.exe';
const status = (): CodexSubscriptionStatus => ({
  available: Boolean(savedPath), authenticated: signedIn && Boolean(savedPath),
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
  codexSubscriptionLogin: async () => { await delay(); if (new URLSearchParams(location.search).has('loginError')) throw new Error('Sign-in was cancelled.'); signedIn = true; return status(); },
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
}, artifacts: {
  capabilities: async selection => ({ selection, ready: Boolean(savedPath && signedIn), items: [
    { id: 'python', label: 'Python', ready: true, detail: 'Fixture Python ready' },
    { id: 'provider', label: 'Codex CLI', ready: Boolean(savedPath), detail: savedPath ? 'Discovered on this computer.' : 'Locate Codex under Agent runtime.' },
    { id: 'credentials', label: 'ChatGPT sign-in', ready: signedIn, detail: signedIn ? 'Signed in.' : 'Sign in under Agent runtime.' },
  ], keySaved: false, canSaveKey: true }),
  setupProgress: async () => ({ running: false, step: '', log: '' }),
  onEvent: () => () => {},
  saveRuntime: async (_id, runtime) => ({ ...record, runtime }),
  start: async () => ({ id: record.id, status: 'running', logs: '' }),
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
      {artifactMode ? <ArtifactRuntimeSetup record={record} running={agentRunning} onReadyChange={ignoreReady} onBusyChange={ignoreReady} /> : open && <RuntimeDrawer onClose={() => setOpen(false)} />}
    </AgentWorkspaceContext.Provider>
  </main>;
}
createRoot(document.getElementById('fixture')!).render(<Fixture />);
