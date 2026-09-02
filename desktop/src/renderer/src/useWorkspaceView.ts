import { useCallback, useEffect, useMemo, useState } from 'react';
import { DEFAULT_WORKSPACE_VIEW, readWorkspaceView, writeWorkspaceView, type WorkspaceView } from '../../shared/workspaceView';

function loadView(root: string): WorkspaceView {
  try { return root ? readWorkspaceView(window.localStorage, root) : { ...DEFAULT_WORKSPACE_VIEW }; }
  catch { return { ...DEFAULT_WORKSPACE_VIEW }; }
}

export function useWorkspaceView(root: string) {
  const initial = useMemo(() => loadView(root), [root]);
  const [saved, setSaved] = useState({ root, view: initial });
  const view = saved.root === root ? saved.view : initial;
  const update = useCallback((change: (current: WorkspaceView) => WorkspaceView): void => {
    setSaved((current) => ({ root, view: change(current.root === root ? current.view : initial) }));
  }, [initial, root]);

  useEffect(() => {
    if (!root || saved.root !== root) return;
    try { writeWorkspaceView(window.localStorage, root, saved.view); }
    catch { /* Access to localStorage itself can also be unavailable. */ }
  }, [root, saved]);

  const showEditor = useCallback(() => update((current) => current.editorVisible ? current : { ...current, editorVisible: true }), [update]);
  const toggleEditor = useCallback(() => update((current) => ({ ...current, editorVisible: !current.editorVisible })), [update]);
  const setAgentWidth = useCallback((agentWidth: number) => update((current) => ({ ...current, agentWidth })), [update]);
  const reset = useCallback(() => update(() => ({ ...DEFAULT_WORKSPACE_VIEW })), [update]);
  return { view, showEditor, toggleEditor, setAgentWidth, reset };
}
