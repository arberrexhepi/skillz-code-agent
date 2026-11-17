import type { useRuntime } from '../runtime/useRuntime';

export function RunRepositoryPicker({ runtime }: { runtime: ReturnType<typeof useRuntime> }) {
  const { status, repositoryId, selectRepository, pending } = runtime;
  const repositories = status.repositories || [{ id: 'repo', label: 'Observatory artifact', access: 'write' as const }];
  const active = status.runs.find(item => item.id === status.activeRunId);
  const selectedId = status.activeRunId ? active?.repository?.id || 'repo' : repositoryId;
  const selected = repositories.find(item => item.id === selectedId);
  return <div className="repository-picker">
    <label htmlFor="run-repository">Working repository</label>
    <select id="run-repository" value={selectedId} onChange={event => selectRepository(event.target.value)} disabled={!status.available || pending || Boolean(status.activeRunId)} aria-describedby="repository-help">
      {!selected && <option value={selectedId} disabled>Previously selected repository is unavailable</option>}
      {repositories.map(repository => <option key={repository.id} value={repository.id}>{repository.label} · {repository.access === 'read' ? 'Read only' : 'Read & write'}</option>)}
    </select>
    <p id="repository-help">{status.activeRunId ? 'This run is pinned to its repository. Finish or stop it before switching.' : selected?.access === 'read' ? 'This run can inspect the repository. Editing requires write permission in File Access.' : 'New runs work in this repository and use its File Access permissions.'} {!status.activeRunId && 'Add or change repositories in the Workbench File Access tab, then restart preview.'}</p>
  </div>;
}
