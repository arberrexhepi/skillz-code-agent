import { useCallback, useEffect, useMemo, useState } from 'react';
import metadata from '../artifact.json';
import {
  manager,
  type CommandCandidate,
  type Inventory,
  type ManagedProcess,
  type ProcessStatus,
  type Repository,
} from './manager';

const activeStatuses: ProcessStatus[] = ['starting', 'running', 'stopping'];

function statusLabel(status: ProcessStatus): string {
  if (status === 'stopped') return 'Exited';
  return status[0].toUpperCase() + status.slice(1);
}

function latestProcess(repository: Repository): ManagedProcess | undefined {
  return [...repository.processes].sort((a, b) =>
    b.startedAt.localeCompare(a.startedAt)
  )[0];
}

function RepositoryCard({
  repository,
  busy,
  onStart,
  onStop,
}: {
  repository: Repository;
  busy: boolean;
  onStart: (repository: Repository, candidate: CommandCandidate) => void;
  onStop: (process: ManagedProcess) => void;
}) {
  const [logsOpen, setLogsOpen] = useState(false);
  const current = latestProcess(repository);
  const active = repository.processes.find((process) =>
    activeStatuses.includes(process.status)
  );
  const state = active?.status ?? current?.status ?? (repository.candidates.length ? 'detected' : 'unsupported');
  const processWithLogs = active ?? current;
  const activeCandidate = active && repository.candidates.find((candidate) => candidate.id === active.candidateId);
  const primary = repository.candidates.find((candidate) => candidate.primary);
  const secondary = repository.candidates.filter((candidate) => !candidate.primary);

  useEffect(() => {
    if (current?.status === 'failed') setLogsOpen(true);
  }, [current?.id, current?.status]);

  return (
    <article className="repo-card">
      <header className="repo-header">
        <div className="repo-identity">
          <span className={`status-dot status-${state}`} aria-hidden="true" />
          <div>
            <h2>{repository.label}</h2>
            <p className="repo-id">{repository.id}</p>
          </div>
        </div>
        <span className={`status-pill status-${state}`}>
          {state === 'detected' ? 'Ready to launch' : state === 'unsupported' ? 'Unsupported' : statusLabel(state)}
        </span>
      </header>

      {repository.error ? (
        <div className="notice notice-error" role="alert">
          <strong>Repository unavailable</strong>
          <span>{repository.error}</span>
        </div>
      ) : (
        <>
          {active?.ports.length ? (
            <div className="ports">
              {active.ports.map((port) => (
                <a
                  className="port"
                  href={`http://127.0.0.1:${port}`}
                  target="_blank"
                  rel="noreferrer"
                  key={port}
                >
                  <span className="port-led" />
                  <span>
                    <small>Listening port</small>
                    <strong>{port}</strong>
                  </span>
                  <span aria-hidden="true">↗</span>
                </a>
              ))}
            </div>
          ) : active ? (
            <div className="waiting">
              <span className="pulse" />
              {active.status === 'stopping'
                ? 'Waiting for process to exit…'
                : activeCandidate?.kind === 'task'
                  ? `Running ${activeCandidate.label}…`
                  : 'Waiting for the server to report a listening port…'}
            </div>
          ) : null}

          <div className="commands">
            <div className="section-label">
              <span>Detected commands</span>
              <span>{repository.candidates.length}</span>
            </div>

            {repository.candidates.length ? <div className="command-row command-primary">
              <div className="command-copy">
                {primary ? <><code>{primary.command} {primary.args.join(' ')}</code><span>{primary.args[1] === 'preview' ? 'Production build preview' : 'Recommended development command'}</span></> : <><strong>No launch script whitelisted</strong><span>Use the overflow for allowed utility scripts.</span></>}
              </div>
              <div className="command-actions">
                <button className="button button-primary" disabled={!primary || Boolean(active) || busy} onClick={() => primary && onStart(repository, primary)}>
                  {busy ? 'Working…' : active ? 'Process active' : 'Launch'}
                </button>
                {secondary.length > 0 && <details className="command-overflow">
                  <summary className="button button-ghost" aria-label="Other allowed package scripts">•••</summary>
                  <div>{secondary.map((candidate) => <button type="button" key={candidate.id} disabled={Boolean(active) || busy} onClick={() => onStart(repository, candidate)}><code>{candidate.label}</code><span>{candidate.kind === 'server' ? 'Server' : 'Task'}</span></button>)}</div>
                </details>}
              </div>
            </div> : (
              <div className="empty-inline">
                <strong>No package scripts whitelisted</strong>
                <span>Enable Process Proxy and move scripts into its whitelist in Skillz File access.</span>
              </div>
            )}
          </div>

          {processWithLogs && (
            <footer className="process-footer">
              <div className="process-meta">
                <span>PID {processWithLogs.pid ?? '—'}</span>
                <span>Started {new Date(processWithLogs.startedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                {processWithLogs.exitCode != null && <span>Exit {processWithLogs.exitCode}</span>}
              </div>
              <div className="footer-actions">
                <button
                  className="button button-ghost"
                  onClick={() => setLogsOpen((open) => !open)}
                  aria-expanded={logsOpen}
                >
                  {logsOpen ? 'Hide logs' : 'View logs'}
                </button>
                {active && (
                  <button
                    className="button button-stop"
                    disabled={busy || active.status === 'stopping'}
                    onClick={() => onStop(active)}
                  >
                    {active.status === 'stopping' ? 'Stopping…' : 'Stop'}
                  </button>
                )}
              </div>
            </footer>
          )}

          {logsOpen && processWithLogs && (
            <div className="log-panel">
              <div className="log-title">
                <span>Process output</span>
                <span>Latest retained output</span>
              </div>
              <pre>{processWithLogs.logs || 'No output received yet.'}</pre>
            </div>
          )}
        </>
      )}
    </article>
  );
}

export default function App() {
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [busyRoot, setBusyRoot] = useState('');

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      setInventory(await manager.inventory());
      setError('');
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : 'Could not reach Server Manager.');
    } finally {
      if (!quiet) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    document.title = metadata.title;
    void load();
    const timer = window.setInterval(() => void load(true), 2000);
    return () => window.clearInterval(timer);
  }, [load]);

  const active = useMemo(
    () => inventory?.repositories.flatMap((repository) =>
      repository.processes.filter((process) => activeStatuses.includes(process.status))
    ) ?? [],
    [inventory]
  );

  const ports = useMemo(
    () => active.flatMap((process) => process.ports),
    [active]
  );

  async function start(repository: Repository, candidate: CommandCandidate) {
    setBusyRoot(repository.id);
    try {
      await manager.start(repository.id, candidate.id);
      await load(true);
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : 'Launch failed.');
    } finally {
      setBusyRoot('');
    }
  }

  async function stop(process: ManagedProcess) {
    setBusyRoot(process.rootId);
    try {
      await manager.stop(process.id);
      await load(true);
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : 'Stop failed.');
    } finally {
      setBusyRoot('');
    }
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><i /><i /><i /></span>
          <span>Server Manager</span>
        </div>
        <button className="button button-ghost refresh" onClick={() => void load()} disabled={refreshing}>
          <span className={refreshing ? 'spin' : ''}>↻</span>
          Refresh
        </button>
      </header>

      <section className="hero">
        <div>
          <span className="eyebrow">Local development control</span>
          <h1>Your repositories,<br /><em>alive at a glance.</em></h1>
          <p>Detected commands, managed processes, and confirmed listening ports—all in one place.</p>
        </div>
        <div className="hero-orbit" aria-hidden="true">
          <div className="orbit-ring"><span /></div>
          <div className="orbit-core">{active.length}</div>
        </div>
      </section>

      <section className="summary" aria-label="Server summary">
        <div className="summary-item">
          <span className="summary-icon live">●</span>
          <div><strong>{active.length}</strong><span>Active servers</span></div>
        </div>
        <div className="summary-item">
          <span className="summary-icon">⌁</span>
          <div><strong>{ports.length}</strong><span>Listening ports</span></div>
        </div>
        <div className="summary-item">
          <span className="summary-icon">◇</span>
          <div><strong>{inventory?.repositories.length ?? '—'}</strong><span>Shared repositories</span></div>
        </div>
        <div className="capacity">
          <span>Managed capacity</span>
          <strong>{active.length} / {inventory?.limits.concurrentProcesses ?? '—'}</strong>
          <div><i style={{ width: `${inventory ? (active.length / inventory.limits.concurrentProcesses) * 100 : 0}%` }} /></div>
        </div>
      </section>

      {error && (
        <div className="global-error" role="alert">
          <span><strong>Something needs attention.</strong> {error}</span>
          <button onClick={() => setError('')} aria-label="Dismiss error">×</button>
        </div>
      )}

      <section className="repository-section">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Workspace</span>
            <h2>Repositories</h2>
          </div>
          {inventory && <span className="sync-state"><i /> Synced automatically</span>}
        </div>

        {!inventory && !error ? (
          <div className="state-card">
            <span className="loader" />
            <strong>Discovering repositories</strong>
            <p>Reading the folders currently shared with this artifact.</p>
          </div>
        ) : inventory?.repositories.length === 0 ? (
          <div className="state-card">
            <span className="state-icon">＋</span>
            <strong>No repositories shared yet</strong>
            <p>Add a folder grant in Skillz, then refresh this dashboard.</p>
          </div>
        ) : (
          <div className="repo-grid">
            {inventory?.repositories.map((repository) => (
              <RepositoryCard
                key={repository.id}
                repository={repository}
                busy={busyRoot === repository.id}
                onStart={start}
                onStop={stop}
              />
            ))}
          </div>
        )}
      </section>
    </main>
  );
}
