import { execFile } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import type { GitCommit, GitDiscardResult, GitFileDiff, GitFileStatus, GitStatus } from '../../shared/contracts';
import { canDiscard, isUntracked } from '../../shared/gitStatus';
import { discardFileFingerprint } from './gitDiscardSafety';
import { languageForPath, type WorkspaceService } from './workspace';

interface GitResult {
  stdout: string;
  stderr: string;
}

export class GitService {
  constructor(private readonly workspace: WorkspaceService) {}

  async status(): Promise<GitStatus> {
    return this.statusAt(this.workspace.requireRoot());
  }

  async initialize(expectedRoot: string): Promise<GitStatus> {
    const root = this.workspace.requireRoot();
    if (root !== expectedRoot) throw new Error('Workspace changed. Open Source Control in the intended folder and try again.');
    const status = await this.statusAt(root);
    if (this.workspace.requireRoot() !== root) throw new Error('Workspace changed. Refresh Source Control before initializing.');
    // Existing repositories (including parent repositories and worktrees) need no init.
    if (status.isRepository) return status;
    await this.run(['init'], 0, root);
    return this.statusAt(root);
  }

  private async statusAt(root: string): Promise<GitStatus> {
    let output: GitResult;
    try {
      output = await this.run(['status', '--porcelain=v1', '-z', '--branch', '--untracked-files=all'], 0, root);
    } catch (error) {
      if (/^Error: fatal: not a git repository(?:[ :\(]|$)/i.test(String(error)) && !(await hasLocalGitMetadata(root))) {
        return { isRepository: false, branch: '', ahead: 0, behind: 0, files: [] };
      }
      throw error;
    }
    const records = output.stdout.split('\0').filter(Boolean);
    const header = records.shift() || '## HEAD';
    const branch = parseBranch(header);
    const files: GitFileStatus[] = [];

    for (let index = 0; index < records.length; index += 1) {
      const record = records[index];
      if (record.length < 4) continue;
      const indexStatus = record[0];
      const workTreeStatus = record[1];
      const filePath = record.slice(3);
      const item: GitFileStatus = { path: filePath, indexStatus, workTreeStatus };
      if (indexStatus === 'R' || indexStatus === 'C' || workTreeStatus === 'R' || workTreeStatus === 'C') {
        item.originalPath = records[index + 1];
        index += 1;
      }
      files.push(item);
    }
    return { isRepository: true, ...branch, files };
  }

  async fileDiff(relativePath: string, staged = false): Promise<GitFileDiff> {
    const target = this.workspace.resolve(relativePath);
    const modified = staged
      ? await this.tryGitShow(`:${relativePath}`)
      : await fs.readFile(target, 'utf8').catch(() => '');
    const original = await this.tryGitShow(staged ? `HEAD:${relativePath}` : `:${relativePath}`);
    return { path: relativePath, original, modified, language: languageForPath(relativePath) };
  }

  async history(limit = 50): Promise<GitCommit[]> {
    const count = Math.max(1, Math.min(200, Math.floor(limit)));
    try {
      const output = await this.run([
        'log', `-${count}`, '--date=iso-strict',
        '--pretty=format:%x1e%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%P%x1f%s%x1f%b',
      ]);
      return output.stdout.split('\x1e').map((record) => record.trim()).filter(Boolean).map((record) => {
        const [hash = '', shortHash = '', authorName = '', authorEmail = '', authoredAt = '', parents = '', subject = '', ...body] = record.split('\x1f');
        return {
          hash,
          shortHash,
          authorName,
          authorEmail,
          authoredAt,
          parents: parents.split(' ').filter(Boolean),
          subject,
          body: body.join('\x1f').trim(),
        };
      });
    } catch (error) {
      if (/does not have any commits|unknown revision|bad default revision/i.test(String(error))) return [];
      throw error;
    }
  }

  async stage(paths: string[]): Promise<GitStatus> {
    const safePaths = this.validatePaths(paths);
    await this.run(['add', '--', ...safePaths]);
    return this.status();
  }

  async stageAll(): Promise<GitStatus> {
    await this.run(['add', '-A', '--', '.']);
    return this.status();
  }

  async unstage(paths: string[]): Promise<GitStatus> {
    const safePaths = this.validatePaths(paths);
    try {
      await this.run(['restore', '--staged', '--', ...safePaths]);
    } catch {
      await this.run(['reset', '--', ...safePaths]);
    }
    return this.status();
  }

  async discard(
    relativePath: string,
    confirm: (file: GitFileStatus) => Promise<boolean>,
    trash: (absolutePath: string) => Promise<void>,
  ): Promise<GitDiscardResult> {
    const root = this.workspace.requireRoot();
    const target = this.workspace.resolve(relativePath);
    const snapshot = async (): Promise<{ file: GitFileStatus; fingerprint: string }> => {
      if (this.workspace.requireRoot() !== root) throw new Error('Workspace changed. Refresh Git status before discarding.');
      const status = await this.status();
      const file = status.files.find((item) => item.path === relativePath);
      if (!file || !canDiscard(file)) throw new Error('This file has no discardable unstaged changes. Unstage staged changes first; resolve conflicts separately.');
      const disk = await discardFileFingerprint(root, target);
      const index = (await this.run(['--literal-pathspecs', 'ls-files', '--stage', '-z', '--', relativePath], 0, root)).stdout;
      return { file, fingerprint: JSON.stringify([file, disk, index]) };
    };
    const before = await snapshot();
    if (!(await confirm(before.file))) return { status: await this.status(), discarded: false };
    const after = await snapshot();
    if (before.fingerprint !== after.fingerprint || this.workspace.requireRoot() !== root) {
      throw new Error('The file or its staged changes changed while confirmation was open. Review the diff and try again.');
    }
    if (isUntracked(after.file)) {
      await trash(target);
    } else {
      // Restore from the index, not HEAD: keep staged changes intact. Literal
      // pathspecs prevent names such as [draft].md from targeting other files.
      await this.run(['--literal-pathspecs', 'restore', '--worktree', '--', relativePath], 0, root);
    }
    return { status: await this.status(), discarded: true };
  }

  async commit(message: string): Promise<GitStatus> {
    const cleaned = message.trim();
    if (!cleaned) throw new Error('Commit message cannot be empty.');
    const status = await this.status();
    const staged = status.files.some((file) => file.indexStatus !== ' ' && file.indexStatus !== '?');
    if (!staged) throw new Error('Stage at least one changed file before committing.');
    await this.run(['commit', '-m', cleaned]);
    return this.status();
  }

  async push(): Promise<GitStatus> {
    const status = await this.status();
    if (status.behind > 0) {
      throw new Error(`This branch is ${status.behind} commit${status.behind === 1 ? '' : 's'} behind ${status.upstream || 'its upstream'}. Pull and resolve incoming changes before pushing.`);
    }

    const branch = await this.run(['symbolic-ref', '--quiet', '--short', 'HEAD'])
      .then((result) => result.stdout.trim())
      .catch(() => '');
    if (!branch) throw new Error('Create or switch to a branch before pushing; detached HEAD cannot be published safely.');

    if (status.upstream) {
      if (status.ahead === 0) throw new Error(`Branch ${branch} is already synced with ${status.upstream}.`);
      await this.run(['push'], 120_000);
      return this.status();
    }

    const remotes = (await this.run(['remote'])).stdout.split('\n').map((remote) => remote.trim()).filter(Boolean);
    if (!remotes.length) throw new Error('Add a Git remote before publishing this branch.');
    const remote = remotes.includes('origin') ? 'origin' : remotes.length === 1 ? remotes[0] : '';
    if (!remote) throw new Error('This repository has multiple remotes. Configure an upstream branch before pushing.');
    await this.run(['push', '--set-upstream', remote, branch], 120_000);
    return this.status();
  }

  private validatePaths(paths: string[]): string[] {
    if (paths.length === 0) throw new Error('Select at least one path.');
    return paths.map((item) => {
      this.workspace.resolve(item);
      return item.replaceAll('\\', '/');
    });
  }

  private async tryGitShow(revision: string): Promise<string> {
    try {
      return (await this.run(['show', revision])).stdout;
    } catch {
      return '';
    }
  }

  private run(args: string[], timeout = 0, root = this.workspace.requireRoot()): Promise<GitResult> {
    const env: NodeJS.ProcessEnv = { ...process.env, LC_ALL: 'C', LANG: 'C' };
    // Git commands must target the opened folder, even when the app was started
    // from a shell or hook with repository-location overrides.
    for (const name of ['GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES']) delete env[name];
    if (timeout) env.GIT_TERMINAL_PROMPT = '0';
    return new Promise((resolve, reject) => {
      execFile('git', args, {
        cwd: root,
        encoding: 'utf8',
        maxBuffer: 20 * 1024 * 1024,
        timeout: timeout || undefined,
        env,
        windowsHide: true,
      }, (error, stdout, stderr) => {
        if (error) {
          reject(new Error(String(stderr || error.message).trim()));
          return;
        }
        resolve({ stdout, stderr });
      });
    });
  }
}

async function hasLocalGitMetadata(root: string): Promise<boolean> {
  // Never turn a damaged local .git directory/file into an initialization offer.
  try {
    await fs.lstat(path.join(root, '.git'));
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false;
    throw error;
  }
}

function parseBranch(header: string): Omit<GitStatus, 'files' | 'isRepository'> {
  const value = header.replace(/^##\s*/, '');
  if (value.startsWith('Initial commit on ')) {
    return { branch: value.slice('Initial commit on '.length), ahead: 0, behind: 0 };
  }
  if (value.startsWith('No commits yet on ')) {
    return { branch: value.slice('No commits yet on '.length), ahead: 0, behind: 0 };
  }
  const match = /^(.*?)(?:\.\.\.([^\s]+))?(?: \[(.*?)\])?$/.exec(value);
  const branch = match?.[1] || value;
  const upstream = match?.[2];
  const tracking = match?.[3] || '';
  const ahead = Number(/ahead (\d+)/.exec(tracking)?.[1] || 0);
  const behind = Number(/behind (\d+)/.exec(tracking)?.[1] || 0);
  return { branch, upstream, ahead, behind };
}
