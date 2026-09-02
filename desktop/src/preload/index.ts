import { contextBridge, ipcRenderer } from 'electron';
import type { AgentEvent, TerminalEvent, WorkbenchApi } from '../shared/contracts';

const api: WorkbenchApi = {
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

contextBridge.exposeInMainWorld('workbench', api);

function subscribe<T>(channel: string, listener: (payload: T) => void): () => void {
  const wrapped = (_event: Electron.IpcRendererEvent, payload: T): void => listener(payload);
  ipcRenderer.on(channel, wrapped);
  return () => ipcRenderer.removeListener(channel, wrapped);
}
