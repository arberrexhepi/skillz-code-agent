import Editor, { DiffEditor, type Monaco, type OnMount } from '@monaco-editor/react';
import type { EditorTab, FileTab } from '../editorTypes';

interface EditorPaneProps {
  tabs: EditorTab[];
  activeId: string;
  onActivate: (id: string) => void;
  onClose: (id: string) => void;
  onChange: (id: string, content: string) => void;
  onSave: (id: string) => void;
}

export function EditorPane(props: EditorPaneProps): React.JSX.Element {
  const active = props.tabs.find((tab) => tab.id === props.activeId);
  const beforeMount = (monaco: Monaco): void => {
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
  };
  const mount: OnMount = (editor, monaco) => {
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      if (active?.kind === 'file') props.onSave(active.id);
    });
  };

  return (
    <section className="editor-pane">
      <div className="editor-tabs" role="tablist">
        {props.tabs.map((tab) => (
          <button
            type="button"
            key={tab.id}
            className={`editor-tab ${tab.id === props.activeId ? 'active' : ''}`}
            onClick={() => props.onActivate(tab.id)}
            role="tab"
          >
            <span>{tab.kind === 'diff' ? 'Δ ' : ''}{tab.title}</span>
            {tab.kind === 'file' && tab.dirty && <span className="dirty-dot">●</span>}
            <span className="tab-close" onClick={(event) => { event.stopPropagation(); props.onClose(tab.id); }}>×</span>
          </button>
        ))}
      </div>
      <div className="editor-surface">
        {!active && <EditorWelcome />}
        {active?.kind === 'file' && (
          <Editor
            key={active.id}
            path={`file:///${active.document.path}`}
            language={active.document.language}
            value={active.content}
            theme="skillz-dark"
            beforeMount={beforeMount}
            onMount={mount}
            onChange={(value) => props.onChange(active.id, value || '')}
            options={editorOptions}
          />
        )}
        {active?.kind === 'diff' && (
          <DiffEditor
            key={active.id}
            original={active.diff.original}
            modified={active.diff.modified}
            language={active.diff.language}
            theme="skillz-dark"
            beforeMount={beforeMount}
            options={{ ...editorOptions, readOnly: true, renderSideBySide: true }}
          />
        )}
      </div>
    </section>
  );
}

const editorOptions = {
  automaticLayout: true,
  fontFamily: "'SFMono-Regular', 'Cascadia Code', 'JetBrains Mono', monospace",
  fontSize: 13,
  lineHeight: 21,
  minimap: { enabled: false },
  padding: { top: 12 },
  smoothScrolling: true,
  cursorBlinking: 'smooth' as const,
  renderWhitespace: 'selection' as const,
  scrollBeyondLastLine: false,
};

function EditorWelcome(): React.JSX.Element {
  return (
    <div className="editor-welcome">
      <div className="welcome-mark">S</div>
      <h2>Agent workbench</h2>
      <p>Open a file from the explorer or ask the agent to start shaping the repository.</p>
      <div className="shortcut"><kbd>⌘</kbd><kbd>S</kbd><span>Save active file</span></div>
    </div>
  );
}

export function isFileTab(tab: EditorTab | undefined): tab is FileTab {
  return tab?.kind === 'file';
}
