import { promises as fs } from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import type { RecentWorkspace } from '../../shared/contracts';

interface StoredWorkspace { root: string; lastOpenedAt: string }
const limit = 12;

export class WorkspaceHistoryService {
  constructor(readonly filePath: string) {}

  async recent(): Promise<RecentWorkspace[]> {
    const stored = await this.read();
    return Promise.all(stored.map(async item => ({
      ...item,
      name: path.basename(item.root) || item.root,
      available: await fs.stat(item.root).then(stat => stat.isDirectory(), () => false),
    })));
  }

  async record(root: string): Promise<void> {
    const canonical = await fs.realpath(root);
    const key = process.platform === 'win32' ? canonical.toLocaleLowerCase() : canonical;
    const repositories = (await this.read()).filter(item => (process.platform === 'win32' ? item.root.toLocaleLowerCase() : item.root) !== key);
    repositories.unshift({ root: canonical, lastOpenedAt: new Date().toISOString() });
    await this.write(repositories.slice(0, limit));
  }

  private async read(): Promise<StoredWorkspace[]> {
    try {
      const markdown = await fs.readFile(this.filePath, 'utf8');
      const match = markdown.match(/```json\s*([\s\S]*?)\s*```/i);
      if (!match) return [];
      const value = JSON.parse(match[1]) as { version?: unknown; repositories?: unknown };
      if (value.version !== 1 || !Array.isArray(value.repositories)) return [];
      return value.repositories.flatMap(item => {
        if (!item || typeof item !== 'object') return [];
        const candidate = item as Partial<StoredWorkspace>;
        if (typeof candidate.root !== 'string' || !path.isAbsolute(candidate.root) || candidate.root.length > 4096) return [];
        if (typeof candidate.lastOpenedAt !== 'string' || Number.isNaN(Date.parse(candidate.lastOpenedAt))) return [];
        return [{ root: candidate.root, lastOpenedAt: candidate.lastOpenedAt }];
      }).slice(0, limit);
    } catch { return []; }
  }

  private async write(repositories: StoredWorkspace[]): Promise<void> {
    const body = `# Recent repositories\n\n<!-- Managed by skillz Workbench. Local paths stay on this computer. -->\n\n\`\`\`json\n${JSON.stringify({ version: 1, repositories }, null, 2)}\n\`\`\`\n`;
    await fs.mkdir(path.dirname(this.filePath), { recursive: true });
    const temporary = `${this.filePath}.${randomUUID()}.tmp`;
    try { await fs.writeFile(temporary, body, { encoding: 'utf8', flag: 'wx', mode: 0o600 }); await fs.rename(temporary, this.filePath); }
    finally { await fs.rm(temporary, { force: true }); }
  }
}
