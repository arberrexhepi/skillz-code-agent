import { promises as fs } from 'node:fs';
import path from 'node:path';
import type { GitFileStatus, GitLineChanges } from '../../shared/contracts';
import { isConflicted, isUntracked } from '../../shared/gitStatus';

/** --numstat -z keeps tabs/newlines in filenames and rename paths unambiguous. */
export function parseNumstat(output: string): Map<string, GitLineChanges> {
  const changes = new Map<string, GitLineChanges>();
  const records = output.split('\0');
  for (let index = 0; index < records.length; index++) {
    const record = records[index], first = record.indexOf('\t'), second = record.indexOf('\t', first + 1);
    if (first < 0 || second < 0) continue;
    const added = record.slice(0, first), removed = record.slice(first + 1, second);
    let name = record.slice(second + 1);
    if (!name) { index += 2; name = records[index]; } // old path, destination path
    if (!name) continue;
    if (added === '-' || removed === '-') changes.set(name, { binary: true });
    else if (/^\d+$/.test(added) && /^\d+$/.test(removed)) changes.set(name, { additions: Number(added), deletions: Number(removed) });
  }
  return changes;
}

export function lineChangesForFile(tracked: Map<string, GitLineChanges>, file: GitFileStatus): GitLineChanges {
  const current = tracked.get(file.path) || { additions: 0, deletions: 0 };
  const original = file.originalPath && (file.indexStatus === 'R' || file.workTreeStatus === 'R') ? tracked.get(file.originalPath) : undefined;
  return !original ? current : 'binary' in current || 'binary' in original
    ? { binary: true } : { additions: current.additions + original.additions, deletions: current.deletions + original.deletions };
}

export async function populateLineChanges(
  root: string, files: GitFileStatus[], numstat: string | undefined, unborn: boolean,
  readRename?: (file: GitFileStatus) => Promise<GitLineChanges | undefined>,
): Promise<void> {
  const tracked = numstat === undefined ? undefined : parseNumstat(numstat);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(8, files.length) }, async () => {
    while (next < files.length) {
      const file = files[next++];
      if (isConflicted(file)) continue;
      if (isUntracked(file) || (unborn && file.indexStatus === 'A')) {
        file.lineChanges = await addedFileLines(root, file.path);
      } else if (tracked) {
        file.lineChanges = readRename && file.originalPath && (file.indexStatus === 'R' || file.workTreeStatus === 'R')
          ? await readRename(file) : lineChangesForFile(tracked, file);
      }
    }
  }));
}

function contained(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative);
}

async function addedFileLines(root: string, relative: string): Promise<GitLineChanges | undefined> {
  try {
    const target = path.resolve(root, relative);
    if (!contained(root, target)) return undefined;
    const realRoot = await fs.realpath(root), parent = await fs.realpath(path.dirname(target));
    if (!contained(realRoot, parent)) return undefined;
    const stat = await fs.lstat(target);
    // Count the link's own Git blob, never the file it points to.
    if (stat.isSymbolicLink()) {
      const link = Buffer.from(await fs.readlink(target));
      return { additions: countLineFeeds(link) + (link.length && link.at(-1) !== 10 ? 1 : 0), deletions: 0 };
    }
    if (!stat.isFile()) return undefined;
    const resolved = await fs.realpath(target);
    if (!contained(realRoot, resolved)) return undefined;
    const stream = await fs.open(resolved, 'r');
    try {
      const buffer = Buffer.allocUnsafe(64 * 1024);
      let additions = 0, lastByte = -1, first = true;
      for (;;) {
        const { bytesRead } = await stream.read(buffer, 0, buffer.length, null);
        if (!bytesRead) break;
        const chunk = buffer.subarray(0, bytesRead);
        if (first && chunk.subarray(0, 8000).includes(0)) return { binary: true };
        first = false;
        additions += countLineFeeds(chunk);
        lastByte = chunk[bytesRead - 1];
      }
      return { additions: additions + (lastByte >= 0 && lastByte !== 10 ? 1 : 0), deletions: 0 };
    } finally { await stream.close(); }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return { additions: 0, deletions: 0 };
    return undefined;
  }
}

function countLineFeeds(buffer: Buffer): number {
  let count = 0;
  for (const byte of buffer) if (byte === 10) count++;
  return count;
}
