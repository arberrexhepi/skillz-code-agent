import type { GitFileStatus } from './contracts';

export function isUntracked(file: GitFileStatus): boolean {
  return file.indexStatus === '?' && file.workTreeStatus === '?';
}

export function isConflicted(file: GitFileStatus): boolean {
  return ['DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'].includes(file.indexStatus + file.workTreeStatus);
}

export function isStaged(file: GitFileStatus): boolean {
  return file.indexStatus !== ' ' && file.indexStatus !== '?';
}

export function isUnstaged(file: GitFileStatus): boolean {
  return file.workTreeStatus !== ' ' || isUntracked(file);
}

export function canDiscard(file: GitFileStatus): boolean {
  return !isConflicted(file) && (isUntracked(file) || ['M', 'D', 'T'].includes(file.workTreeStatus));
}

export function gitStatusLabel(file: GitFileStatus): { code: string; title: string } {
  if (isConflicted(file)) return { code: '!', title: 'Merge conflict — resolve before discarding' };
  if (isUntracked(file)) return { code: 'U', title: 'Untracked — not yet added to Git' };
  const labels: Record<string, string> = { M: 'Modified', A: 'Added', D: 'Deleted', R: 'Renamed', C: 'Copied', T: 'Type changed' };
  const code = file.workTreeStatus.trim() || file.indexStatus.trim();
  const details = [
    isStaged(file) ? `Staged: ${labels[file.indexStatus] || file.indexStatus}` : '',
    isUnstaged(file) ? `Unstaged: ${labels[file.workTreeStatus] || file.workTreeStatus}` : '',
  ].filter(Boolean).join('; ');
  return { code, title: details };
}
