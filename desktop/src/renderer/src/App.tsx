import { useCallback, useEffect, useMemo, useState } from 'react';
import type { GitStatus, WorkspaceInfo } from '../../shared/contracts';
import { groupDiagnostics } from '../../shared/agentCore';
import { AgentWorkspaceProvider } from './agent/AgentWorkspaceContext';
import { useAgentWorkspace } from './agent/agentWorkspace';
import { AgentIssues } from './components/AgentIssues';
import { AgentPanel } from './components/AgentPanel';
import { AgentTopStatus } from './components/AgentTopStatus';
import { EditorPane, isFileTab } from './components/EditorPane';
import { FileExplorer } from './components/FileExplorer';
import { GitPanel } from './components/GitPanel';
import { WorkspaceDock } from './components/WorkspaceDock';
import { WorkspaceLayout } from './components/WorkspaceLayout';
import { WorkspaceViewControls } from './components/WorkspaceViewControls';
import type { EditorTab } from './editorTypes';
import { useWorkspaceView } from './useWorkspaceView';

export default function App(): React.JSX.Element {
  return <AgentWorkspaceProvider><WorkbenchApp /></AgentWorkspaceProvider>;
}

function WorkbenchApp(): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
  const { view, showEditor, toggleEditor, setAgentWidth, reset: resetView } = useWorkspaceView(workspace?.root || '');
  const [tabs, setTabs] = useState<EditorTab[]>([]);
  const [activeId, setActiveId] = useState('');
  const [sidebarMode, setSidebarMode] = useState<'files' | 'git' | 'issues'>('files');
  const [revision, setRevision] = useState(0);
  const [gitStatus, setGitStatus] = useState<GitStatus | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    void window.workbench.workspace.current().then(setWorkspace);
    return window.workbench.workspace.onChange(() => setRevision((value) => value + 1));
  }, []);

  const chooseWorkspace = async (): Promise<void> => {
    try {
      const selected = await window.workbench.workspace.choose();
      if (!selected) return;
      setWorkspace(selected);
      setGitStatus(null);
      setTabs([]);
      setActiveId('');
      setRevision((value) => value + 1);
      setError('');
    } catch (cause) {
      setError(cleanError(cause));
    }
  };

  const openFile = useCallback(async (path: string): Promise<void> => {
    const id = `file:${path}`;
    if (tabs.some((tab) => tab.id === id)) {
      setActiveId(id);
      showEditor();
      return;
    }
    try {
      const document = await window.workbench.workspace.read(path);
      setTabs((current) => [...current, {
        id, kind: 'file', title: path.split('/').at(-1) || path, document, content: document.content, dirty: false,
      }]);
      setActiveId(id);
      showEditor();
    } catch (cause) {
      setError(cleanError(cause));
    }
  }, [showEditor, tabs]);

  const openDiff = useCallback(async (path: string, staged: boolean): Promise<void> => {
    const id = `diff:${staged ? 'staged' : 'working'}:${path}`;
    const existing = tabs.some((tab) => tab.id === id);
    if (existing) {
      setActiveId(id);
      showEditor();
      return;
    }
    try {
      const diff = await window.workbench.git.fileDiff(path, staged);
      setTabs((current) => [...current, { id, kind: 'diff', title: `${path.split('/').at(-1)} (diff)`, diff }]);
      setActiveId(id);
      showEditor();
    } catch (cause) {
      setError(cleanError(cause));
    }
  }, [showEditor, tabs]);

  const updateTab = (id: string, content: string): void => {
    setTabs((current) => current.map((tab) => tab.id === id && tab.kind === 'file'
      ? { ...tab, content, dirty: content !== tab.document.content }
      : tab));
  };

  const saveTab = async (id: string): Promise<void> => {
    const tab = tabs.find((candidate) => candidate.id === id);
    if (!isFileTab(tab) || !tab.dirty) return;
    try {
      const document = await window.workbench.workspace.write(tab.document.path, tab.content);
      setTabs((current) => current.map((candidate) => candidate.id === id && candidate.kind === 'file'
        ? { ...candidate, document, content: document.content, dirty: false }
        : candidate));
    } catch (cause) {
      setError(cleanError(cause));
    }
  };

  const closeTab = (id: string): void => {
    const index = tabs.findIndex((tab) => tab.id === id);
    const closing = tabs[index];
    if (closing?.kind === 'file' && closing.dirty && !window.confirm(`Close ${closing.title} without saving?`)) return;
    const next = tabs.filter((tab) => tab.id !== id);
    setTabs(next);
    if (activeId === id) setActiveId(next[Math.min(index, next.length - 1)]?.id || '');
  };

  const canDiscardPath = (path: string): boolean => !tabs.some((tab) => (
    tab.kind === 'file' && tab.document.path === path && tab.dirty
  ));

  const refreshDiscardedPath = async (path: string): Promise<void> => {
    const document = await window.workbench.workspace.read(path).catch(() => null);
    setTabs((current) => current.flatMap((tab): EditorTab[] => {
      if (tab.kind === 'diff' && tab.diff.path === path) return [];
      if (tab.kind !== 'file' || tab.document.path !== path || tab.dirty) return [tab];
      return document ? [{ ...tab, document, content: document.content, dirty: false }] : [];
    }));
    setActiveId((id) => {
      if (id === `diff:working:${path}` || id === `diff:staged:${path}` || (!document && id === `file:${path}`)) return '';
      return id;
    });
    setRevision((value) => value + 1);
  };

  const dirtyCount = useMemo(() => tabs.filter((tab) => tab.kind === 'file' && tab.dirty).length, [tabs]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">S</span><span>skillz</span></div>
        <button type="button" className="workspace-switcher" onClick={() => void chooseWorkspace()}>
          <span className="muted">Workspace</span>
          <strong>{workspace?.name || 'Open repository'}</strong>
          <span>⌄</span>
        </button>
        {gitStatus && <div className="topbar-branch">⑂ {gitStatus.branch}{gitStatus.ahead ? ` ↑${gitStatus.ahead}` : ''}{gitStatus.behind ? ` ↓${gitStatus.behind}` : ''}</div>}
        {dirtyCount > 0 && <div className="unsaved-label">{dirtyCount} unsaved</div>}
        {workspace && <AgentTopStatus />}
        {workspace && <WorkspaceViewControls editorVisible={view.editorVisible} onToggleEditor={toggleEditor} onReset={resetView} />}
      </header>

      <WorkspaceLayout view={view} onAgentResize={setAgentWidth} sidebar={
        <aside className="sidebar">
          <div className="sidebar-tabs">
            <button type="button" className={sidebarMode === 'files' ? 'active' : ''} onClick={() => setSidebarMode('files')}>Files</button>
            <button type="button" className={sidebarMode === 'git' ? 'active' : ''} onClick={() => setSidebarMode('git')}>
              Git{gitStatus?.files.length ? <span className="count-badge">{gitStatus.files.length}</span> : null}
            </button>
            <button type="button" className={sidebarMode === 'issues' ? 'active' : ''} onClick={() => setSidebarMode('issues')}>Issues</button>
          </div>
          <div className="panel-heading">
            <span>{sidebarMode === 'files' ? workspace?.name.toUpperCase() || 'EXPLORER' : sidebarMode === 'git' ? 'SOURCE CONTROL' : 'AGENT CONTEXT'}</span>
            <button type="button" className="icon-button push-right" onClick={() => setRevision((value) => value + 1)}>↻</button>
          </div>
          <div className="sidebar-content">
            {!workspace && <div className="panel-message">Open a repository to begin.</div>}
            {workspace && sidebarMode === 'files' && <FileExplorer revision={revision} onOpenFile={(path) => void openFile(path)} />}
            {workspace && sidebarMode === 'git' && <GitPanel key={workspace.root} workspaceRoot={workspace.root} revision={revision} onOpenDiff={(path, staged) => void openDiff(path, staged)} onStatus={setGitStatus} onBeforeDiscard={canDiscardPath} onDiscard={refreshDiscardedPath} />}
            {workspace && sidebarMode === 'issues' && <AgentIssues onOpenPath={(path) => void openFile(path)} />}
          </div>
        </aside>
      } editor={
        <EditorPane diagnostics={groupDiagnostics(agent.state.bridge)} tabs={tabs} activeId={activeId} onActivate={setActiveId} onClose={closeTab} onChange={updateTab} onSave={(id) => void saveTab(id)} />
      } dock={
        workspace && <WorkspaceDock key={workspace.root} onOpenPath={(path) => void openFile(path)} onOpenDiff={(path, staged) => void openDiff(path, staged)} />
      } agent={workspace ? <AgentPanel key={workspace.root} /> : (
        <aside className="agent-panel empty-right"><span>✦</span><p>The agent becomes available when a workspace is open.</p></aside>
      )} />

      {error && <button type="button" className="global-error" onClick={() => setError('')}>{error}<span>×</span></button>}
      {!workspace && (
        <div className="workspace-overlay">
          <div className="overlay-card">
            <span className="overlay-mark">S</span>
            <h1>Build from the repository outward.</h1>
            <p>Open a local project to activate Monaco, the terminal, Git controls, and the Python agent.</p>
            <button type="button" className="primary-button large" onClick={() => void chooseWorkspace()}>Open repository</button>
          </div>
        </div>
      )}
    </div>
  );
}

function cleanError(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '');
}
