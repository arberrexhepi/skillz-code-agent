import { useCallback, useEffect, useState } from 'react';
import type { GitCommit, GitFileStatus, GitStatus } from '../../../shared/contracts';

interface GitPanelProps {
  revision: number;
  onOpenDiff: (path: string, staged: boolean) => void;
  onStatus: (status: GitStatus | null) => void;
}

export function GitPanel({ revision, onOpenDiff, onStatus }: GitPanelProps): React.JSX.Element {
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [history, setHistory] = useState<GitCommit[]>([]);
  const [mode, setMode] = useState<'changes' | 'history'>('changes');
  const [commitMessage, setCommitMessage] = useState('');
  const [error, setError] = useState('');
  const [busyPath, setBusyPath] = useState('');

  const refresh = useCallback(async (): Promise<void> => {
    try {
      const [next, commits] = await Promise.all([window.workbench.git.status(), window.workbench.git.history(75)]);
      setStatus(next);
      setHistory(commits);
      onStatus(next);
      setError('');
    } catch (cause) {
      setStatus(null);
      onStatus(null);
      setError(cleanError(cause));
    }
  }, [onStatus]);

  useEffect(() => { void refresh(); }, [refresh, revision]);

  const mutate = async (path: string, operation: () => Promise<GitStatus>): Promise<boolean> => {
    setBusyPath(path);
    try {
      const next = await operation();
      setStatus(next);
      onStatus(next);
      setError('');
      return true;
    } catch (cause) {
      setError(cleanError(cause));
      return false;
    } finally {
      setBusyPath('');
    }
  };

  const commit = async (): Promise<void> => {
    if (!commitMessage.trim()) return;
    const stagedCount = status?.files.filter(isStaged).length || 0;
    if (!stagedCount) {
      setError('Stage at least one changed file before committing.');
      return;
    }
    if (!(await mutate('__commit__', () => window.workbench.git.commit(commitMessage)))) return;
    setCommitMessage('');
    try { setHistory(await window.workbench.git.history(75)); } catch { /* Status errors remain the primary Git signal. */ }
  };

  const stagedCount = status?.files.filter(isStaged).length || 0;
  const unstagedCount = status?.files.filter(isUnstaged).length || 0;
  const sync = syncState(status, history.length > 0, busyPath);

  if (error && !status) return <div className="panel-message error-text">{error}</div>;
  return (
    <div className="git-panel">
      <div className="git-summary">
        <span className="branch-mark">⑂</span>
        <span>{status?.branch || 'Repository'}</span>
        <button type="button" className="icon-button push-right" onClick={() => void refresh()} title="Refresh Git status">↻</button>
      </div>
      <div className="git-view-tabs">
        <button type="button" className={mode === 'changes' ? 'active' : ''} onClick={() => setMode('changes')}>Changes{status?.files.length ? <span>{status.files.length}</span> : null}</button>
        <button type="button" className={mode === 'history' ? 'active' : ''} onClick={() => setMode('history')}>History{history.length ? <span>{history.length}</span> : null}</button>
      </div>
      {mode === 'changes' && <>
      <div className="commit-box">
        <textarea
          value={commitMessage}
          onChange={(event) => setCommitMessage(event.target.value)}
          placeholder="Commit message"
          rows={2}
        />
        <div className="commit-actions">
          <button type="button" disabled={!unstagedCount || Boolean(busyPath)} onClick={() => void mutate('__stage_all__', () => window.workbench.git.stageAll())}>Stage all{unstagedCount ? ` (${unstagedCount})` : ''}</button>
          <button type="button" className="primary-button" disabled={!commitMessage.trim() || Boolean(busyPath)} onClick={() => void commit()}>Commit staged{stagedCount ? ` (${stagedCount})` : ''}</button>
          <button type="button" className={`sync-button ${sync.pending ? 'pending' : ''}`} disabled={sync.disabled} title={sync.title} onClick={() => void mutate('__push__', () => window.workbench.git.push())}>{sync.label}</button>
        </div>
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
      </>}
      {mode === 'history' && <GitHistory commits={history} />}
    </div>
  );
}

function GitHistory({ commits }: { commits: GitCommit[] }): React.JSX.Element {
  const [expandedHash, setExpandedHash] = useState('');
  if (!commits.length) return <div className="panel-message">No commits yet.</div>;
  return <div className="git-history">{commits.map((commit) => {
    const expanded = expandedHash === commit.hash;
    return <article className={`git-commit ${expanded ? 'expanded' : ''}`} key={commit.hash}>
      <button type="button" className="git-commit-toggle" aria-expanded={expanded} onClick={() => setExpandedHash((current) => current === commit.hash ? '' : commit.hash)}>
        <span className={`commit-node ${commit.parents.length > 1 ? 'merge' : ''}`} />
        <span className="commit-copy"><strong>{commit.subject || 'Untitled commit'}</strong><small>{commit.authorName} · {relativeDate(commit.authoredAt)}</small></span>
        <code>{commit.shortHash}</code>
      </button>
      {expanded && <div className="git-commit-details">
        {commit.body && <p>{commit.body}</p>}
        <dl><div><dt>Commit</dt><dd>{commit.hash}</dd></div><div><dt>Author</dt><dd>{commit.authorName} &lt;{commit.authorEmail}&gt;</dd></div><div><dt>Date</dt><dd>{formatDate(commit.authoredAt)}</dd></div>{commit.parents.length > 1 && <div><dt>Parents</dt><dd>{commit.parents.map((parent) => parent.slice(0, 8)).join(', ')}</dd></div>}</dl>
      </div>}
    </article>;
  })}</div>;
}

function GitFile({ file, busy, onDiff, onStage, onUnstage }: {
  file: GitFileStatus;
  busy: boolean;
  onDiff: (path: string, staged: boolean) => void;
  onStage: (path: string) => void;
  onUnstage: (path: string) => void;
}): React.JSX.Element {
  const staged = isStaged(file);
  const unstaged = isUnstaged(file);
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

function isStaged(file: GitFileStatus): boolean {
  return file.indexStatus !== ' ' && file.indexStatus !== '?';
}

function isUnstaged(file: GitFileStatus): boolean {
  return file.workTreeStatus !== ' ' || file.indexStatus === '?';
}

function cleanError(error: unknown): string {
  return String(error)
    .replace(/^Error:\s*/, '')
    .replace(/^Error invoking remote method '[^']+': Error:\s*/, '')
    .replace(/^Error:\s*/, '');
}

function syncState(status: GitStatus | null, hasCommits: boolean, busyPath: string): { label: string; title: string; disabled: boolean; pending: boolean } {
  const busy = Boolean(busyPath);
  if (busyPath === '__push__') return { label: 'Pushing…', title: 'Publishing committed changes', disabled: true, pending: true };
  if (!status || !hasCommits) return { label: 'Nothing to sync', title: 'Create a commit before publishing this branch', disabled: true, pending: false };
  if (!status.upstream) return { label: 'Publish branch', title: `Publish ${status.branch} and configure its upstream`, disabled: busy, pending: true };
  if (status.behind > 0) return { label: `Pull required ↓${status.behind}`, title: `${status.branch} is behind ${status.upstream}; pull incoming changes before pushing`, disabled: true, pending: false };
  if (status.ahead > 0) return { label: `Sync changes ↑${status.ahead}`, title: `Push ${status.ahead} commit${status.ahead === 1 ? '' : 's'} to ${status.upstream}`, disabled: busy, pending: true };
  return { label: 'Synced with remote', title: `${status.branch} is up to date with ${status.upstream}`, disabled: true, pending: false };
}

function relativeDate(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return value;
  const seconds = Math.round((timestamp - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, 'second');
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, 'minute');
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, 'hour');
  const days = Math.round(hours / 24);
  if (Math.abs(days) < 30) return formatter.format(days, 'day');
  const months = Math.round(days / 30);
  if (Math.abs(months) < 12) return formatter.format(months, 'month');
  return formatter.format(Math.round(months / 12), 'year');
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}
