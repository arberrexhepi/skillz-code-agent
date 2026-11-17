import path from 'node:path';
import { ArtifactSandbox } from './artifactSandbox';

export interface RunRepository { id: string; label: string; access: 'read' | 'write' }
export function runRepositories(sandbox: ArtifactSandbox): RunRepository[] {
  return [{ id: 'repo', label: 'Observatory artifact', access: 'write' }, ...sandbox.reads.map(({ id, label, access, path: root }) => ({ id, label: id === 'workspace' ? `${label}: ${path.basename(root)}` : label, access }))];
}
export function scopeRunRepository(sandbox: ArtifactSandbox, id = 'repo') {
  const repository = runRepositories(sandbox).find(item => item.id === id);
  if (!repository) throw new Error('This repository is not granted in File Access. Choose an available repository.');
  const grant = sandbox.reads.find(item => item.id === id);
  return {
    repository,
    root: grant?.path || sandbox.root,
    // A recording targets one granted repository. The preview and editing agent keep their own mounts.
    sandbox: grant ? new ArtifactSandbox(sandbox.root, [grant], sandbox.context, undefined, undefined, grant) : sandbox,
  };
}
