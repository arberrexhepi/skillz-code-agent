import { promises as fs, watch, type FSWatcher } from 'node:fs';
import path from 'node:path';
import { dialog, type BrowserWindow } from 'electron';
import type { FileDocument, FileEntry, WorkspaceInfo } from '../../shared/contracts';

const HIDDEN_DIRECTORIES = new Set(['.git', 'node_modules', '.venv', '__pycache__', 'out', 'dist', 'release']);
const MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024;

export class WorkspaceService {
  private root: string | null = null;
  private watcher: FSWatcher | null = null;
  private changedPaths = new Set<string>();
  private flushTimer: NodeJS.Timeout | null = null;

  constructor(private readonly onFilesChanged: (paths: string[]) => void) {}

  current(): WorkspaceInfo | null {
    if (!this.root) return null;
    return { root: this.root, name: path.basename(this.root) };
  }

  async choose(parent: BrowserWindow): Promise<WorkspaceInfo | null> {
    const selection = await dialog.showOpenDialog(parent, {
      title: 'Open repository',
      properties: ['openDirectory', 'createDirectory'],
    });
    if (selection.canceled || !selection.filePaths[0]) return null;
    return this.open(selection.filePaths[0]);
  }

  async open(candidate: string): Promise<WorkspaceInfo> {
    const resolved = path.resolve(candidate);
    const stat = await fs.stat(resolved);
    if (!stat.isDirectory()) throw new Error('Workspace must be a directory.');
    this.root = resolved;
    this.startWatcher();
    return { root: resolved, name: path.basename(resolved) };
  }

  requireRoot(): string {
    if (!this.root) throw new Error('Open a workspace first.');
    return this.root;
  }

  resolve(relativePath = ''): string {
    const root = this.requireRoot();
    const normalized = relativePath.replaceAll('\\', '/').replace(/^\/+/, '');
    const target = path.resolve(root, normalized);
    const relation = path.relative(root, target);
    if (relation.startsWith('..') || path.isAbsolute(relation)) {
      throw new Error('Path is outside the active workspace.');
    }
    return target;
  }

  async list(relativePath = ''): Promise<FileEntry[]> {
    const target = this.resolve(relativePath);
    const entries = await fs.readdir(target, { withFileTypes: true });
    return entries
      .filter((entry) => !entry.isSymbolicLink())
      .filter((entry) => !(entry.isDirectory() && HIDDEN_DIRECTORIES.has(entry.name)))
      .filter((entry) => !entry.name.endsWith('.pyc') && entry.name !== '.DS_Store')
      .map((entry) => ({
        name: entry.name,
        path: path.posix.join(relativePath.replaceAll('\\', '/'), entry.name),
        kind: entry.isDirectory() ? 'directory' as const : 'file' as const,
      }))
      .sort((left, right) => {
        if (left.kind !== right.kind) return left.kind === 'directory' ? -1 : 1;
        return left.name.localeCompare(right.name, undefined, { sensitivity: 'base' });
      });
  }

  async read(relativePath: string): Promise<FileDocument> {
    const target = this.resolve(relativePath);
    const stat = await fs.stat(target);
    if (!stat.isFile()) throw new Error('Only regular files can be opened.');
    if (stat.size > MAX_TEXT_FILE_BYTES) throw new Error('File is larger than the 5 MB editor limit.');
    const buffer = await fs.readFile(target);
    if (buffer.includes(0)) throw new Error('Binary files cannot be opened in the text editor.');
    return {
      path: relativePath,
      content: buffer.toString('utf8'),
      language: languageForPath(relativePath),
      modifiedAt: stat.mtimeMs,
    };
  }

  async write(relativePath: string, content: string): Promise<FileDocument> {
    const target = this.resolve(relativePath);
    const stat = await fs.stat(target);
    if (!stat.isFile()) throw new Error('Only existing regular files can be saved.');
    await fs.writeFile(target, content, 'utf8');
    return this.read(relativePath);
  }

  dispose(): void {
    this.watcher?.close();
    this.watcher = null;
    if (this.flushTimer) clearTimeout(this.flushTimer);
    this.flushTimer = null;
  }

  private startWatcher(): void {
    this.watcher?.close();
    const root = this.requireRoot();
    try {
      this.watcher = watch(root, { recursive: true }, (_event, filename) => {
        if (!filename) return;
        const relative = filename.toString().replaceAll('\\', '/');
        if (relative.split('/').some((part) => HIDDEN_DIRECTORIES.has(part))) return;
        this.changedPaths.add(relative);
        if (this.flushTimer) clearTimeout(this.flushTimer);
        this.flushTimer = setTimeout(() => {
          const paths = [...this.changedPaths];
          this.changedPaths.clear();
          this.onFilesChanged(paths);
        }, 120);
      });
    } catch {
      this.watcher = null;
    }
  }
}

export function languageForPath(filePath: string): string {
  const extension = path.extname(filePath).toLowerCase();
  return ({
    '.c': 'c', '.cpp': 'cpp', '.cs': 'csharp', '.css': 'css', '.go': 'go',
    '.html': 'html', '.java': 'java', '.js': 'javascript', '.jsx': 'javascript',
    '.json': 'json', '.md': 'markdown', '.php': 'php', '.py': 'python',
    '.rb': 'ruby', '.rs': 'rust', '.scss': 'scss', '.sh': 'shell',
    '.sql': 'sql', '.ts': 'typescript', '.tsx': 'typescript', '.xml': 'xml',
    '.yaml': 'yaml', '.yml': 'yaml',
  } as Record<string, string>)[extension] || 'plaintext';
}
