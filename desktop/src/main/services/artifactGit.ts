import path from 'node:path';
import { lstatSync, realpathSync } from 'node:fs';
import type { GitFileStatus } from '../../shared/contracts';
import { GitService, type GitWorkspace } from './git';
import type { ArtifactLibraryService } from './artifactLibrary';

class ArtifactGitWorkspace implements GitWorkspace {
  constructor(private readonly root: string) {}
  requireRoot(): string { return this.root; }
  resolve(relativePath = ''): string {
    const normalized = relativePath.replaceAll('\\', '/').replace(/^\/+/, '');
    const target = path.resolve(this.root, normalized);
    const relation = path.relative(this.root, target);
    if (relation.startsWith('..') || path.isAbsolute(relation)) throw new Error('Path is outside the artifact repository.');
    let existing = target;
    for (;;) {
      try {
        const stat = lstatSync(existing);
        if (stat.isSymbolicLink()) throw new Error('Linked paths cannot be managed from the artifact repository.');
        const real = realpathSync(existing);
        const realRelation = path.relative(this.root, real);
        if (realRelation.startsWith('..') || path.isAbsolute(realRelation)) throw new Error('Path resolves outside the artifact repository.');
        break;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
        const parent = path.dirname(existing);
        if (parent === existing) throw error;
        existing = parent;
      }
    }
    return target;
  }
}

export class ArtifactGitService {
  constructor(private readonly library: ArtifactLibraryService) {}
  private async repository(id: string): Promise<{ root: string; git: GitService }> {
    const root = (await this.library.find(id)).root;
    return { root, git: new GitService(new ArtifactGitWorkspace(root)) };
  }
  async status(id: string) { return (await this.repository(id)).git.status(); }
  async initialize(id: string) { const { root, git } = await this.repository(id); return git.initialize(root); }
  async history(id: string, limit?: number) { return (await this.repository(id)).git.history(limit); }
  async fileDiff(id: string, relativePath: string, staged = false) { return (await this.repository(id)).git.fileDiff(relativePath, staged); }
  async stage(id: string, paths: string[]) { return (await this.repository(id)).git.stage(paths); }
  async stageAll(id: string) { return (await this.repository(id)).git.stageAll(); }
  async unstage(id: string, paths: string[]) { return (await this.repository(id)).git.unstage(paths); }
  async discard(id: string, relativePath: string, confirm: (file: GitFileStatus) => Promise<boolean>, trash: (absolutePath: string) => Promise<void>) {
    return (await this.repository(id)).git.discard(relativePath, confirm, trash);
  }
  async commit(id: string, message: string) { return (await this.repository(id)).git.commit(message); }
  async push(id: string) { return (await this.repository(id)).git.push(); }
}
