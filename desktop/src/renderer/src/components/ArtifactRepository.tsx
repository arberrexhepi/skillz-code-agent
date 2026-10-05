import { DiffEditor, type DiffOnMount, type Monaco } from '@monaco-editor/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { GitFileDiff, GitStatus } from '../../../shared/contracts';
import { FileNavigationContext, PathChip } from './PathText';
import { GitPanel, type GitPanelApi } from './GitPanel';

export function ArtifactRepository({ id, root, onStatus }: { id: string; root: string; onStatus: (status: GitStatus | null) => void }): React.JSX.Element {
  const [revision, setRevision] = useState(0);
  const [diff, setDiff] = useState<GitFileDiff | null>(null);
  const [staged, setStaged] = useState(false);
  const [error, setError] = useState('');
  const diffEditor = useRef<Parameters<DiffOnMount>[0] | null>(null);
  const api = useMemo<GitPanelApi>(() => {
    const artifacts = window.workbench.artifacts;
    return {
      status: () => artifacts.gitStatus(id),
      initialize: () => artifacts.gitInitialize(id),
      history: (limit) => artifacts.gitHistory(id, limit),
      fileDiff: (path, isStaged) => artifacts.gitFileDiff(id, path, isStaged),
      stage: (paths) => artifacts.gitStage(id, paths),
      stageAll: () => artifacts.gitStageAll(id),
      unstage: (paths) => artifacts.gitUnstage(id, paths),
      discard: (path) => artifacts.gitDiscard(id, path),
      commit: (message) => artifacts.gitCommit(id, message),
      push: () => artifacts.gitPush(id),
    };
  }, [id]);
  const openDiff = useCallback(async (path: string, isStaged: boolean): Promise<void> => {
    setError('');
    try { setDiff(await api.fileDiff(path, isStaged)); setStaged(isStaged); }
    catch (cause) { setError(clean(cause)); }
  }, [api]);
  const discarded = useCallback(async (path: string): Promise<void> => {
    if (diff?.path === path) setDiff(null);
    setRevision(value => value + 1);
  }, [diff?.path]);
  useEffect(() => window.workbench.editor.onCommand((command) => {
    if (command !== 'find' || !diffEditor.current) return;
    const editor = diffEditor.current.getModifiedEditor();
    editor.focus();
    void editor.getAction('actions.find')?.run();
  }), []);
  const navigation = useMemo(() => ({ root, open: (reference: { path: string }) => { void openDiff(reference.path, false); } }), [root, openDiff]);
  return <FileNavigationContext.Provider value={navigation}>
    <div className="artifact-repository">
      <aside className="artifact-repository-control">
        <GitPanel
          api={api}
          workspaceRoot={root}
          revision={revision}
          onOpenDiff={(path, isStaged) => void openDiff(path, isStaged)}
          onStatus={onStatus}
          onBeforeDiscard={() => true}
          onDiscard={discarded}
        />
      </aside>
      <section className="artifact-repository-diff" aria-label="Artifact repository diff">
        {error && <div className="artifacts-error" role="alert">{error}</div>}
        {!diff && <div className="artifact-repository-empty"><span>Δ</span><h2>Review artifact changes</h2><p>Select a changed file to inspect its working or staged diff.</p></div>}
        {diff && <><header><PathChip path={diff.path} /><span>{staged ? 'Staged changes' : 'Working tree changes'}</span><button type="button" aria-label="Close diff" onClick={() => setDiff(null)}>×</button></header><div className="artifact-repository-editor"><DiffEditor key={diff.path + String(staged)} original={diff.original} modified={diff.modified} language={diff.language} theme="skillz-dark" beforeMount={defineTheme} onMount={editor => { diffEditor.current = editor; }} options={{ automaticLayout: true, readOnly: true, renderSideBySide: true, minimap: { enabled: false }, fontFamily: "'SFMono-Regular', 'Cascadia Code', 'JetBrains Mono', monospace", fontSize: 12, lineHeight: 20, scrollBeyondLastLine: false }} /></div></>}
      </section>
    </div>
  </FileNavigationContext.Provider>;
}

function defineTheme(monaco: Monaco): void {
  monaco.editor.defineTheme('skillz-dark', {
    base: 'vs-dark',
    inherit: true,
    rules: [],
    colors: {
      'editor.background': '#0d1014',
      'editorGutter.background': '#0d1014',
      'editorLineNumber.foreground': '#3f4651',
      'editorLineNumber.activeForeground': '#8d96a3',
      'editor.selectionBackground': '#23405d99',
      'editor.lineHighlightBackground': '#11161c',
    },
  });
}

function clean(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '').replace(/^Error: /, '');
}
