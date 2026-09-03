import { promises as fs } from 'node:fs';
import path from 'node:path';
import { parseRepoFacts, REPO_FACTS_PATH, type RepoFactsSnapshot } from '../../shared/repoFacts';

/** A fixed, read-only workspace file; no Python process or provider credentials required. */
export async function readRepoFacts(root: string): Promise<RepoFactsSnapshot> {
  const base = { workspaceRoot: root, path: REPO_FACTS_PATH } as const;
  const target = path.join(root, REPO_FACTS_PATH);
  try {
    const stat = await fs.lstat(target);
    if (stat.isSymbolicLink() || !stat.isFile()) throw new Error('repo_facts.md must be a regular file in this workspace.');
    if (stat.size > 5 * 1024 * 1024) throw new Error('repo_facts.md is larger than the 5 MB viewer limit.');
    const buffer = await fs.readFile(target);
    if (buffer.includes(0)) throw new Error('repo_facts.md contains binary data.');
    let content: string;
    try { content = new TextDecoder('utf-8', { fatal: true }).decode(buffer); }
    catch { throw new Error('repo_facts.md is not valid UTF-8. Save it as UTF-8 and refresh.'); }
    try { return { ...base, status: 'ready', modifiedAt: stat.mtimeMs, ledger: parseRepoFacts(content) }; }
    catch (cause) { return { ...base, status: 'invalid', modifiedAt: stat.mtimeMs, error: cause instanceof Error ? cause.message : String(cause) }; }
  } catch (cause) {
    if ((cause as NodeJS.ErrnoException).code === 'ENOENT') return { ...base, status: 'missing' };
    throw cause;
  }
}
