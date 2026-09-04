import { PathText } from './PathText';
import { useEffect, useRef, useState } from 'react';
import type { FileEntry } from '../../../shared/contracts';

interface FileExplorerProps {
  revision: number;
  activePath?: string;
  reveal?: { path: string; request: number };
  onOpenFile: (path: string) => void;
}

export function FileExplorer({ revision, activePath, reveal, onOpenFile }: FileExplorerProps): React.JSX.Element {
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [error, setError] = useState('');

  useEffect(() => {
    void window.workbench.workspace.list('').then(setEntries).catch((cause) => setError(cleanError(cause)));
  }, [revision]);

  if (error) return <div className="panel-message error-text"><PathText>{error}</PathText></div>;
  return (
    <div className="tree" role="tree">
      {entries.map((entry) => (
        <TreeEntry key={`${entry.kind}:${entry.path}`} entry={entry} depth={0} revision={revision} activePath={activePath} reveal={reveal} onOpenFile={onOpenFile} />
      ))}
    </div>
  );
}

interface TreeEntryProps extends FileExplorerProps {
  entry: FileEntry;
  depth: number;
}

function TreeEntry({ entry, depth, revision, activePath, reveal, onOpenFile }: TreeEntryProps): React.JSX.Element {
  const rowRef = useRef<HTMLButtonElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [children, setChildren] = useState<FileEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuError, setMenuError] = useState('');
  const [creatingFile, setCreatingFile] = useState(false);
  const [newFileName, setNewFileName] = useState('');
  const [creatingPending, setCreatingPending] = useState(false);

  useEffect(() => {
    if (!expanded || entry.kind !== 'directory') return;
    setLoading(true);
    void window.workbench.workspace.list(entry.path)
      .then(setChildren)
      .finally(() => setLoading(false));
  }, [entry.kind, entry.path, expanded, revision]);

  useEffect(() => {
    if (!reveal?.request) return;
    if (entry.kind === 'directory' && reveal.path.startsWith(`${entry.path}/`)) setExpanded(true);
    if (entry.kind === 'file' && reveal.path === entry.path) rowRef.current?.scrollIntoView?.({ block: 'center', inline: 'nearest' });
  }, [entry.kind, entry.path, reveal?.path, reveal?.request]);

  const activate = (): void => {
    if (entry.kind === 'directory') setExpanded((value) => !value);
    else onOpenFile(entry.path);
  };
  const showContextMenu = (event: React.MouseEvent): void => {
    event.preventDefault();
    setMenuError('');
    setMenuOpen(true);
    void window.workbench.workspace.showEntryMenu(entry, expanded)
      .then((action) => {
        if (action === 'open' && entry.kind === 'file') onOpenFile(entry.path);
        if (action === 'toggle' && entry.kind === 'directory') setExpanded((value) => !value);
        if (action === 'new-file' && entry.kind === 'directory') {
          setExpanded(true);
          setCreatingFile(true);
          setNewFileName('');
        }
      })
      .catch((cause) => setMenuError(cleanError(cause)))
      .finally(() => setMenuOpen(false));
  };
  const createFile = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault();
    if (entry.kind !== 'directory' || creatingPending) return;
    setCreatingPending(true);
    setMenuError('');
    try {
      const created = await window.workbench.workspace.createFile(entry.path, newFileName);
      setChildren(await window.workbench.workspace.list(entry.path));
      setCreatingFile(false);
      setNewFileName('');
      onOpenFile(created.path);
    } catch (cause) {
      setMenuError(cleanError(cause));
    } finally {
      setCreatingPending(false);
    }
  };

  return (
    <>
      <button
        ref={rowRef}
        type="button"
        className={`tree-row ${entry.kind === 'file' && activePath === entry.path ? 'active' : ''} ${menuOpen ? 'context-target' : ''}`}
        style={{ paddingLeft: 10 + depth * 14 }}
        onClick={activate}
        onContextMenu={showContextMenu}
        role="treeitem"
        aria-current={entry.kind === 'file' && activePath === entry.path ? 'page' : undefined}
        aria-expanded={entry.kind === 'directory' ? expanded : undefined}
      >
        <span className="tree-chevron">{entry.kind === 'directory' ? (expanded ? '⌄' : '›') : ''}</span>
        <span className={`tree-icon ${entry.kind}`}>{entry.kind === 'directory' ? '▰' : '·'}</span>
        <span className="tree-label">{entry.name}</span>
      </button>
      {menuError && <div className="inline-error" style={{ paddingLeft: 32 + depth * 14 }}><PathText>{menuError}</PathText></div>}
      {expanded && (
        <>
          {creatingFile && (
            <form className="tree-new-file" style={{ paddingLeft: 24 + depth * 14 }} onSubmit={(event) => void createFile(event)}>
              <span className="tree-icon file">·</span>
              <input
                autoFocus
                aria-label={`New file name in ${entry.path}`}
                value={newFileName}
                onChange={(event) => setNewFileName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape' && !creatingPending) {
                    event.preventDefault();
                    setCreatingFile(false);
                    setNewFileName('');
                    setMenuError('');
                  }
                }}
                placeholder="file name"
                spellCheck={false}
                disabled={creatingPending}
              />
              <button type="submit" aria-label="Create file" title="Create file" disabled={creatingPending || !newFileName}>✓</button>
              <button type="button" aria-label="Cancel new file" title="Cancel" disabled={creatingPending} onClick={() => { setCreatingFile(false); setNewFileName(''); setMenuError(''); }}>×</button>
            </form>
          )}
          {loading
            ? <div className="tree-loading" style={{ paddingLeft: 32 + depth * 14 }}>Loading…</div>
            : children.map((child) => (
            <TreeEntry
              key={`${child.kind}:${child.path}`}
              entry={child}
              depth={depth + 1}
              revision={revision}
              activePath={activePath}
              reveal={reveal}
              onOpenFile={onOpenFile}
            />
            ))}
        </>
      )}
    </>
  );
}

function cleanError(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '').replace(/^Error: /, '');
}
