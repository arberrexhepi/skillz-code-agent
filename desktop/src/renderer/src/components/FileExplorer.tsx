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
    void window.workbench.workspace.list('').then(setEntries).catch((cause) => setError(String(cause)));
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

  return (
    <>
      <button
        ref={rowRef}
        type="button"
        className={`tree-row ${entry.kind === 'file' && activePath === entry.path ? 'active' : ''}`}
        style={{ paddingLeft: 10 + depth * 14 }}
        onClick={activate}
        role="treeitem"
        aria-current={entry.kind === 'file' && activePath === entry.path ? 'page' : undefined}
        aria-expanded={entry.kind === 'directory' ? expanded : undefined}
      >
        <span className="tree-chevron">{entry.kind === 'directory' ? (expanded ? '⌄' : '›') : ''}</span>
        <span className={`tree-icon ${entry.kind}`}>{entry.kind === 'directory' ? '▰' : '·'}</span>
        <span className="tree-label">{entry.name}</span>
      </button>
      {expanded && (
        loading
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
          ))
      )}
    </>
  );
}
