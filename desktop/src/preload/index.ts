import { contextBridge, ipcRenderer } from 'electron';
import type { AgentEvent, TerminalEvent, WorkbenchApi } from '../shared/contracts';

const api: WorkbenchApi = {
  workspace: {
    current: () => ipcRenderer.invoke('workspace:current'),
    choose: () => ipcRenderer.invoke('workspace:choose'),
    open: (root) => ipcRenderer.invoke('workspace:open', root),
    list: (path = '') => ipcRenderer.invoke('workspace:list', path),
    read: (path) => ipcRenderer.invoke('workspace:read', path),
    write: (path, content) => ipcRenderer.invoke('workspace:write', path, content),
    onChange: (listener) => subscribe('workspace:changed', listener),
  },
  git: {
    status: () => ipcRenderer.invoke('git:status'),
    fileDiff: (path, staged = false) => ipcRenderer.invoke('git:file-diff', path, staged),
    stage: (paths) => ipcRenderer.invoke('git:stage', paths),
    unstage: (paths) => ipcRenderer.invoke('git:unstage', paths),
    commit: (message) => ipcRenderer.invoke('git:commit', message),
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
