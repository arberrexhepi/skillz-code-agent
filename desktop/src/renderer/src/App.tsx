import { FileNavigationContext, PathText } from './components/PathText';
import { resolveFileReference, type FileReference } from '../../shared/fileReferences';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { GitStatus, WorkspaceInfo } from '../../shared/contracts';
import { groupDiagnostics } from '../../shared/agentCore';
import { AgentWorkspaceProvider } from './agent/AgentWorkspaceContext';
import { useAgentWorkspace } from './agent/agentWorkspace';
import { AgentIssues } from './components/AgentIssues';
import { RepoFactsPanel } from './components/RepoFactsPanel';
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
  const [sidebarMode, setSidebarMode] = useState<'files' | 'git' | 'issues' | 'facts'>('files');
  const [revision, setRevision] = useState(0);
  const [focusedIssueId, setFocusedIssueId] = useState('');
  const [gitStatus, setGitStatus] = useState<GitStatus | null>(null);
  const [error, setError] = useState('');
  const workspaceRoot = useRef(workspace?.root || '');
  workspaceRoot.current = workspace?.root || '';
  const revealSequence = useRef(0);

  useEffect(() => {
    void window.workbench.workspace.current().then(setWorkspace);
    return window.workbench.workspace.onChange(() => setRevision((value) => value + 1));
  }, []);

  const chooseWorkspace = async (): Promise<void> => {
    try {
      const selected = await window.workbench.workspace.choose();
      if (!selected) return;
      workspaceRoot.current = selected.root;
      setWorkspace(selected);
      setFocusedIssueId('');
      setGitStatus(null);
      setTabs([]);
      setActiveId('');
      setRevision((value) => value + 1);
      setError('');
    } catch (cause) {
      setError(cleanError(cause));
    }
  };

  const openFile = useCallback(async (raw: string, position?: FileReference): Promise<void> => {
    const root = workspaceRoot.current;
    const reference = position || resolveFileReference(raw, root);
    if (!reference) { setError('This file reference is outside the current workspace.'); return; }
    const path = reference.path;
    const id = `file:${path}`;
    const reveal = reference.line ? { line: reference.line, column: reference.column || 1, request: ++revealSequence.current } : undefined;
    if (tabs.some((tab) => tab.id === id)) {
      if (reveal) setTabs((current) => current.map((tab) => tab.id === id && tab.kind === 'file' ? { ...tab, reveal } : tab));
      setActiveId(id); showEditor(); return;
    }
    try {
      const document = await window.workbench.workspace.read(path);
      if (root !== workspaceRoot.current) return;
      setTabs((current) => current.some((tab) => tab.id === id) ? current.map((tab) => tab.id === id && tab.kind === 'file' && reveal ? { ...tab, reveal } : tab) : [...current, {
        id, kind: 'file', title: path.split('/').at(-1) || path, document, content: document.content, dirty: false, reveal,
      }]);
      setActiveId(id); showEditor();
    } catch (cause) { if (root === workspaceRoot.current) setError(cleanError(cause)); }
  }, [showEditor, tabs]);
  const fileNavigation = useMemo(() => ({ root: workspace?.root || '', open: (reference: FileReference) => { void openFile(reference.path, reference); } }), [workspace?.root, openFile]);

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
    <FileNavigationContext.Provider value={fileNavigation}><div className="app-shell">
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
            <button type="button" className={sidebarMode === 'facts' ? 'active' : ''} onClick={() => setSidebarMode('facts')}>Repo Facts</button>
          </div>
          <div className="panel-heading">
            <span>{sidebarMode === 'files' ? workspace?.name.toUpperCase() || 'EXPLORER' : sidebarMode === 'git' ? 'SOURCE CONTROL' : sidebarMode === 'facts' ? 'REPOSITORY FACTS' : 'ISSUES'}</span>
            <button type="button" className="icon-button push-right" aria-label={sidebarMode === 'facts' ? 'Refresh repo facts' : 'Refresh sidebar'} onClick={() => setRevision((value) => value + 1)}>↻</button>
          </div>
          <div className="sidebar-content" key={`${workspace?.root}:${sidebarMode}`}>
            {!workspace && <div className="panel-message">Open a repository to begin.</div>}
            {workspace && sidebarMode === 'files' && <FileExplorer revision={revision} onOpenFile={(path) => void openFile(path)} />}
            {workspace && sidebarMode === 'git' && <GitPanel key={workspace.root} workspaceRoot={workspace.root} revision={revision} onOpenDiff={(path, staged) => void openDiff(path, staged)} onStatus={setGitStatus} onBeforeDiscard={canDiscardPath} onDiscard={refreshDiscardedPath} />}
            {workspace && sidebarMode === 'issues' && <AgentIssues key={workspace.root} workspaceRoot={workspace.root} revision={revision} focusedIssueId={focusedIssueId} onOpenPath={(path) => void openFile(path)} />}
            {workspace && sidebarMode === 'facts' && <RepoFactsPanel onOpenIssue={(id) => { setFocusedIssueId(id); setSidebarMode('issues'); }} key={workspace.root} workspaceRoot={workspace.root} revision={revision} onOpenPath={(path) => void openFile(path)} />}
          </div>
        </aside>
      } editor={
        <EditorPane diagnostics={groupDiagnostics(agent.state.bridge)} tabs={tabs} activeId={activeId} onActivate={setActiveId} onClose={closeTab} onChange={updateTab} onSave={(id) => void saveTab(id)} />
      } dock={
        workspace && <WorkspaceDock key={workspace.root} onOpenDiff={(path, staged) => void openDiff(path, staged)} />
      } agent={workspace ? <AgentPanel key={workspace.root} /> : (
        <aside className="agent-panel empty-right"><span>✦</span><p>The agent becomes available when a workspace is open.</p></aside>
      )} />

      {error && <div className="global-error" role="alert"><div className="error-copy"><PathText>{error}</PathText></div><button type="button" aria-label="Dismiss error" onClick={() => setError('')}>×</button></div>}
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
    </div></FileNavigationContext.Provider>
  );
}

function cleanError(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '');
}
