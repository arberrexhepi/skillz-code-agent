import { useCallback, useEffect, useState } from 'react';
import type { GitFileStatus, GitStatus } from '../../../shared/contracts';

interface GitPanelProps {
  revision: number;
  onOpenDiff: (path: string, staged: boolean) => void;
  onStatus: (status: GitStatus | null) => void;
}

export function GitPanel({ revision, onOpenDiff, onStatus }: GitPanelProps): React.JSX.Element {
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [commitMessage, setCommitMessage] = useState('');
  const [error, setError] = useState('');
  const [busyPath, setBusyPath] = useState('');

  const refresh = useCallback(async (): Promise<void> => {
    try {
      const next = await window.workbench.git.status();
      setStatus(next);
      onStatus(next);
      setError('');
    } catch (cause) {
      setStatus(null);
      onStatus(null);
      setError(cleanError(cause));
    }
  }, [onStatus]);

  useEffect(() => { void refresh(); }, [refresh, revision]);

  const mutate = async (path: string, operation: () => Promise<GitStatus>): Promise<void> => {
    setBusyPath(path);
    try {
      const next = await operation();
      setStatus(next);
      onStatus(next);
      setError('');
    } catch (cause) {
      setError(cleanError(cause));
    } finally {
      setBusyPath('');
    }
  };

  const commit = async (): Promise<void> => {
    if (!commitMessage.trim()) return;
    await mutate('__commit__', () => window.workbench.git.commit(commitMessage));
    setCommitMessage('');
  };

  if (error && !status) return <div className="panel-message error-text">{error}</div>;
  return (
    <div className="git-panel">
      <div className="git-summary">
        <span className="branch-mark">⑂</span>
        <span>{status?.branch || 'Repository'}</span>
        <button type="button" className="icon-button push-right" onClick={() => void refresh()} title="Refresh Git status">↻</button>
      </div>
      <div className="commit-box">
        <textarea
          value={commitMessage}
          onChange={(event) => setCommitMessage(event.target.value)}
          placeholder="Commit message"
          rows={2}
        />
        <button type="button" className="primary-button compact" disabled={!commitMessage.trim() || busyPath === '__commit__'} onClick={() => void commit()}>
          Commit staged
        </button>
      </div>
      {error && <div className="inline-error">{error}</div>}
      <div className="git-files">
        {status?.files.length === 0 && <div className="panel-message">Working tree is clean.</div>}
        {status?.files.map((file) => (
          <GitFile
            key={`${file.path}:${file.indexStatus}:${file.workTreeStatus}`}
            file={file}
            busy={busyPath === file.path}
            onDiff={onOpenDiff}
            onStage={(path) => void mutate(path, () => window.workbench.git.stage([path]))}
            onUnstage={(path) => void mutate(path, () => window.workbench.git.unstage([path]))}
          />
        ))}
      </div>
    </div>
  );
}

function GitFile({ file, busy, onDiff, onStage, onUnstage }: {
  file: GitFileStatus;
  busy: boolean;
  onDiff: (path: string, staged: boolean) => void;
  onStage: (path: string) => void;
  onUnstage: (path: string) => void;
}): React.JSX.Element {
  const staged = file.indexStatus !== ' ' && file.indexStatus !== '?';
  const unstaged = file.workTreeStatus !== ' ' || file.indexStatus === '?';
  return (
    <div className="git-file-row">
      <button type="button" className="git-file-name" title={file.path} onClick={() => onDiff(file.path, staged && !unstaged)}>
        <span>{file.path.split('/').at(-1)}</span>
        <small>{file.path.includes('/') ? file.path.slice(0, file.path.lastIndexOf('/')) : ''}</small>
      </button>
      <span className="git-code">{file.indexStatus}{file.workTreeStatus}</span>
      {staged && <button type="button" className="icon-button" disabled={busy} onClick={() => onUnstage(file.path)} title="Unstage">−</button>}
      {unstaged && <button type="button" className="icon-button" disabled={busy} onClick={() => onStage(file.path)} title="Stage">＋</button>}
    </div>
  );
}

function cleanError(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '');
}
