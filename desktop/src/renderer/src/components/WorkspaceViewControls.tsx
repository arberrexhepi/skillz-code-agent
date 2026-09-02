export function WorkspaceViewControls({ editorVisible, onToggleEditor, onReset }: {
  editorVisible: boolean;
  onToggleEditor: () => void;
  onReset: () => void;
}): React.JSX.Element {
  return <div className="workspace-view-controls" role="group" aria-label="Workspace view">
    <button type="button" aria-label="Show code editor" aria-pressed={editorVisible} aria-controls="workspace-editor" title={editorVisible ? 'Hide code editor (keep tabs and unsaved edits)' : 'Show code editor'} onClick={onToggleEditor}>
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><rect x="1.5" y="2.5" width="13" height="11" rx="1.5" /><path d="M10 3v10M4 6l-2 2 2 2m3-4 2 2-2 2" /></svg>
      <span>{editorVisible ? 'Hide editor' : 'Show editor'}</span>
    </button>
    <button type="button" className="view-reset" aria-label="Reset workspace layout" title="Show editor and restore default agent width" onClick={onReset}>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><path d="M3 5a5 5 0 1 1-.5 5M3 1v4h4" /></svg>
    </button>
  </div>;
}
