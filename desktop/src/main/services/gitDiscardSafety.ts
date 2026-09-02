import { promises as fs } from 'node:fs';
import path from 'node:path';

/** Refuse directories/submodules and symlink traversal before a destructive action. */
export async function discardFileFingerprint(root: string, target: string): Promise<string> {
  const realRoot = await fs.realpath(root);
  let parent = path.dirname(target);
  for (;;) {
    try {
      const realParent = await fs.realpath(parent);
      const relative = path.relative(realRoot, realParent);
      if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
        throw new Error('Cannot discard through a path outside the workspace.');
      }
      break;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      const next = path.dirname(parent);
      if (next === parent) throw error;
      parent = next;
    }
  }
  try {
    const stat = await fs.lstat(target, { bigint: true });
    if (!stat.isFile()) throw new Error('Discard supports regular files only, not directories, submodules, or symbolic links.');
    return [stat.dev, stat.ino, stat.size, stat.mtimeNs, stat.ctimeNs].join(':');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return 'missing';
    throw error;
  }
}
