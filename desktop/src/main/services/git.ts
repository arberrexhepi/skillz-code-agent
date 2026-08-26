import { execFile } from 'node:child_process';
import { promises as fs } from 'node:fs';
import type { GitFileDiff, GitFileStatus, GitStatus } from '../../shared/contracts';
import { languageForPath, type WorkspaceService } from './workspace';

interface GitResult {
  stdout: string;
  stderr: string;
}

export class GitService {
  constructor(private readonly workspace: WorkspaceService) {}

  async status(): Promise<GitStatus> {
    const output = await this.run(['status', '--porcelain=v1', '-z', '--branch']);
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
      if (indexStatus === 'R' || indexStatus === 'C') {
        item.originalPath = records[index + 1];
        index += 1;
      }
      files.push(item);
    }
    return { ...branch, files };
  }

  async fileDiff(relativePath: string, staged = false): Promise<GitFileDiff> {
    const target = this.workspace.resolve(relativePath);
    const modified = staged
      ? await this.tryGitShow(`:${relativePath}`)
      : await fs.readFile(target, 'utf8').catch(() => '');
    const original = await this.tryGitShow(`HEAD:${relativePath}`);
    return { path: relativePath, original, modified, language: languageForPath(relativePath) };
  }

  async stage(paths: string[]): Promise<GitStatus> {
    const safePaths = this.validatePaths(paths);
    await this.run(['add', '--', ...safePaths]);
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

  async commit(message: string): Promise<GitStatus> {
    const cleaned = message.trim();
    if (!cleaned) throw new Error('Commit message cannot be empty.');
    await this.run(['commit', '-m', cleaned]);
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

  private run(args: string[]): Promise<GitResult> {
    return new Promise((resolve, reject) => {
      execFile('git', args, {
        cwd: this.workspace.requireRoot(),
        encoding: 'utf8',
        maxBuffer: 20 * 1024 * 1024,
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

function parseBranch(header: string): Omit<GitStatus, 'files'> {
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
