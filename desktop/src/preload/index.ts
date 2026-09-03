import type { ArtifactEvent } from '../shared/artifacts';
import { contextBridge, ipcRenderer } from 'electron';
import type { AgentEvent, TerminalEvent, WorkbenchApi } from '../shared/contracts';

const api: WorkbenchApi = {
  artifacts: {
    capabilities: (selection) => ipcRenderer.invoke('artifacts:capabilities', selection),
    installCapabilities: (selection) => ipcRenderer.invoke('artifacts:install-capabilities', selection),
    setupProgress: () => ipcRenderer.invoke('artifacts:setup-progress'),
    saveProviderKey: (provider, key) => ipcRenderer.invoke('artifacts:save-provider-key', provider, key),
    openSetupDownload: (tool) => ipcRenderer.invoke('artifacts:setup-download', tool),
    library: () => ipcRenderer.invoke('artifacts:library'),
    chooseFolder: () => ipcRenderer.invoke('artifacts:choose-folder'),
    chooseReadDirectory: () => ipcRenderer.invoke('artifacts:choose-read-directory'),
    access: (id) => ipcRenderer.invoke('artifacts:access', id),
    saveAccess: (id, access) => ipcRenderer.invoke('artifacts:save-access', id, access),
    create: (options) => ipcRenderer.invoke('artifacts:create', options),
    apis: (id) => ipcRenderer.invoke('artifacts:apis', id),
    saveApis: (id, config) => ipcRenderer.invoke('artifacts:save-apis', id, config),
    start: (id) => ipcRenderer.invoke('artifacts:start', id),
    stop: (id) => ipcRenderer.invoke('artifacts:stop', id),
    installBrowser: () => ipcRenderer.invoke('artifacts:install-browser'),
    preview: (id) => ipcRenderer.invoke('artifacts:preview', id),
    input: (id, input) => ipcRenderer.invoke('artifacts:input', id, input),
    reload: (id) => ipcRenderer.invoke('artifacts:reload', id),
    closePreview: (id) => ipcRenderer.invoke('artifacts:close-preview', id),
    reveal: (id) => ipcRenderer.invoke('artifacts:reveal', id),
    agentStart: (id, options) => ipcRenderer.invoke('artifacts:agent-start', id, options),
    agentRuntimeOptions: (id, provider = '', model = '') => ipcRenderer.invoke('artifacts:agent-runtime', id, provider, model),
    agentSubmit: (id, text) => ipcRenderer.invoke('artifacts:agent-submit', id, text),
    agentPlannerAction: (id, action, extras = {}) => ipcRenderer.invoke('artifacts:agent-planner', id, action, extras),
    agentWorkerAction: (id, action) => ipcRenderer.invoke('artifacts:agent-worker', id, action),
    agentReconfigure: (id, provider, model) => ipcRenderer.invoke('artifacts:agent-reconfigure', id, provider, model),
    agentBackoff: (id, enabled, limit) => ipcRenderer.invoke('artifacts:agent-backoff', id, enabled, limit),
    agentStop: (id) => ipcRenderer.invoke('artifacts:agent-stop', id),
    onEvent: (listener) => subscribe<ArtifactEvent>('artifacts:event', listener),
  },
  workspace: {
    current: () => ipcRenderer.invoke('workspace:current'),
    choose: () => ipcRenderer.invoke('workspace:choose'),
    open: (root) => ipcRenderer.invoke('workspace:open', root),
    list: (path = '') => ipcRenderer.invoke('workspace:list', path),
    read: (path) => ipcRenderer.invoke('workspace:read', path),
    issues: (root) => ipcRenderer.invoke('workspace:issues', root),
    repoFacts: (root) => ipcRenderer.invoke('workspace:repo-facts', root),
    write: (path, content) => ipcRenderer.invoke('workspace:write', path, content),
    onChange: (listener) => subscribe('workspace:changed', listener),
  },
  git: {
    status: () => ipcRenderer.invoke('git:status'),
    initialize: (workspaceRoot) => ipcRenderer.invoke('git:initialize', workspaceRoot),
    history: (limit = 50) => ipcRenderer.invoke('git:history', limit),
    fileDiff: (path, staged = false) => ipcRenderer.invoke('git:file-diff', path, staged),
    stage: (paths) => ipcRenderer.invoke('git:stage', paths),
    stageAll: () => ipcRenderer.invoke('git:stage-all'),
    unstage: (paths) => ipcRenderer.invoke('git:unstage', paths),
    discard: (path) => ipcRenderer.invoke('git:discard', path),
    commit: (message) => ipcRenderer.invoke('git:commit', message),
    push: () => ipcRenderer.invoke('git:push'),
  },
  terminal: {
    create: (options) => ipcRenderer.invoke('terminal:create', options),
    write: (sessionId, data) => ipcRenderer.send('terminal:write', sessionId, data),
    resize: (sessionId, cols, rows) => ipcRenderer.send('terminal:resize', sessionId, cols, rows),
    dispose: (sessionId) => ipcRenderer.send('terminal:dispose', sessionId),
    onEvent: (listener) => subscribe<TerminalEvent>('terminal:event', listener),
  },
  agent: {
    start: (options) => ipcRenderer.invoke('agent:start', options),
    submit: (text) => ipcRenderer.invoke('agent:submit', text),
    plannerAction: (action, extras = {}) => ipcRenderer.invoke('agent:planner-action', action, extras),
    workerAction: (action) => ipcRenderer.invoke('agent:worker-action', action),
    reconfigureRuntime: (provider, model) => ipcRenderer.invoke('agent:reconfigure-runtime', provider, model),
    configureBackoff: (enabled, tokenLimitK) => ipcRenderer.invoke('agent:configure-backoff', enabled, tokenLimitK),
    runtimeOptions: (provider = '', model = '') => ipcRenderer.invoke('agent:runtime-options', provider, model),
    codexSubscriptionStatus: () => ipcRenderer.invoke('agent:codex-subscription-status'),
    codexSubscriptionLogin: () => ipcRenderer.invoke('agent:codex-subscription-login'),
    chooseCodexCli: () => ipcRenderer.invoke('agent:choose-codex-cli'),
    setCodexCliPath: (path) => ipcRenderer.invoke('agent:set-codex-cli-path', path),
    stop: () => ipcRenderer.invoke('agent:stop'),
    onEvent: (listener) => subscribe<AgentEvent>('agent:event', listener),
  },
};

// Artifact iframes never receive the privileged workbench bridge.
if (process.isMainFrame) contextBridge.exposeInMainWorld('workbench', api);

function subscribe<T>(channel: string, listener: (payload: T) => void): () => void {
  const wrapped = (_event: Electron.IpcRendererEvent, payload: T): void => listener(payload);
  ipcRenderer.on(channel, wrapped);
  return () => ipcRenderer.removeListener(channel, wrapped);
}
