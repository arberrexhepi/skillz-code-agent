import type { GitFileStatus } from '../../../shared/contracts';
import { canDiscard, gitStatusLabel, isStaged, isUnstaged, isUntracked } from '../../../shared/gitStatus';

interface GitChangeRowProps {
  file: GitFileStatus;
  busy: boolean;
  onDiff: (path: string, staged: boolean) => void;
  onStage: (path: string) => void;
  onUnstage: (path: string) => void;
  onDiscard: (path: string) => void;
}

export function GitChangeRow({ file, busy, onDiff, onStage, onUnstage, onDiscard }: GitChangeRowProps): React.JSX.Element {
  const staged = isStaged(file);
  const unstaged = isUnstaged(file);
  const status = gitStatusLabel(file);
  const discardLabel = isUntracked(file) ? 'Move untracked file to Trash' : 'Discard unstaged changes';
  return <div className="git-file-row">
    <button type="button" className="git-file-name" title={file.path} onClick={() => onDiff(file.path, staged && !unstaged)}>
      <span>{file.path.split('/').at(-1)}</span>
      <small>{file.path.includes('/') ? file.path.slice(0, file.path.lastIndexOf('/')) : ''}</small>
    </button>
    <span className="git-code" data-status={status.code} title={status.title} aria-label={status.title}>{status.code}</span>
    {canDiscard(file) && <button
      type="button" className="icon-button git-discard" disabled={busy}
      onClick={() => onDiscard(file.path)} title={discardLabel} aria-label={`${discardLabel}: ${file.path}`}
    >
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M5 3 2 6l3 3M2 6h7a4 4 0 0 1 0 8H7" />
      </svg>
    </button>}
    {staged && <button type="button" className="icon-button" disabled={busy} onClick={() => onUnstage(file.path)} title="Unstage" aria-label={`Unstage: ${file.path}`}>−</button>}
    {unstaged && <button type="button" className="icon-button" disabled={busy} onClick={() => onStage(file.path)} title="Stage" aria-label={`Stage: ${file.path}`}>＋</button>}
  </div>;
}
