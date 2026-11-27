import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';

export class RuntimeSettingsService {
  constructor(private readonly filePath: string) {}

  artifactSetupDirectory(): string { return path.join(path.dirname(this.filePath), 'artifact-setup'); }

  async environment(base: NodeJS.ProcessEnv): Promise<NodeJS.ProcessEnv> {
    const saved = await this.readSettings();
    const next: NodeJS.ProcessEnv = { ...base };
    if (saved.codexCliPath) next.CODEX_CLI_PATH = saved.codexCliPath;
    if (saved.geminiCliPath) next.GEMINI_CLI_PATH = saved.geminiCliPath;
    return next;
  }

  private async readSettings(): Promise<{ codexCliPath?: string; geminiCliPath?: string }> {
    try {
      const settings: unknown = JSON.parse(await fs.readFile(this.filePath, 'utf8'));
      if (!settings || typeof settings !== 'object' || Array.isArray(settings)) throw new Error('Invalid runtime settings.');
      const record = settings as { codexCliPath?: unknown; geminiCliPath?: unknown };
      if (record.codexCliPath !== undefined && typeof record.codexCliPath !== 'string') throw new Error('Invalid saved Codex CLI path.');
      if (record.geminiCliPath !== undefined && typeof record.geminiCliPath !== 'string') throw new Error('Invalid saved Gemini CLI path.');
      return {
        ...(record.codexCliPath !== undefined ? { codexCliPath: record.codexCliPath } : {}),
        ...(record.geminiCliPath !== undefined ? { geminiCliPath: record.geminiCliPath } : {}),
      };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return {};
      throw error;
    }
  }

  private async writeSettings(payload: { codexCliPath?: string; geminiCliPath?: string }): Promise<void> {
    await fs.mkdir(path.dirname(this.filePath), { recursive: true });
    const temporary = `${this.filePath}.${randomUUID()}.tmp`;
    try {
      await fs.writeFile(temporary, JSON.stringify(payload, null, 2) + '\n', { mode: 0o600 });
      await fs.rename(temporary, this.filePath);
    } finally {
      await fs.rm(temporary, { force: true });
    }
  }

  async codexCliPath(): Promise<string> {
    return (await this.readSettings()).codexCliPath || '';
  }

  async setCodexCliPath(candidate: string | null): Promise<void> {
    const executable = candidate?.trim() || '';
    if (executable) await validateCodexExecutable(executable);
    // A failed validation leaves the previous selection untouched.
    const existing = await this.readSettings();
    await this.writeSettings({ ...existing, codexCliPath: executable });
  }

  async geminiCliPath(): Promise<string> {
    return (await this.readSettings()).geminiCliPath || '';
  }

  async setGeminiCliPath(candidate: string | null): Promise<void> {
    const executable = candidate?.trim() || '';
    if (executable) await validateGeminiExecutable(executable);
    // A failed validation leaves the previous selection untouched.
    const existing = await this.readSettings();
    await this.writeSettings({ ...existing, geminiCliPath: executable });
  }
}

export async function validateCodexExecutable(executable: string): Promise<void> {
  if (!path.isAbsolute(executable)) throw new Error('Choose the full path to the Codex executable.');
  if (process.platform === 'win32' && path.extname(executable).toLowerCase() !== '.exe') {
    throw new Error('Choose codex.exe on Windows, rather than a .cmd or .ps1 launcher.');
  }
  let isFile = false;
  try { isFile = (await fs.stat(executable)).isFile(); } catch { /* Report a useful selection error below. */ }
  if (!isFile) throw new Error('The selected Codex executable does not exist or is not a file.');
  await new Promise<void>((resolve, reject) => {
    execFile(executable, ['--version'], { encoding: 'utf8', timeout: 5_000, windowsHide: true, maxBuffer: 64 * 1024 }, (error, stdout, stderr) => {
      if (error) reject(new Error(`Could not run the selected Codex executable: ${String(stderr || error.message).trim()}`));
      else if (!/^codex-cli\s+\S+/m.test(`${stdout}\n${stderr}`)) reject(new Error('The selected file does not identify itself as Codex CLI. Choose the Codex executable.'));
      else resolve();
    });
  });
}

export async function validateGeminiExecutable(executable: string): Promise<void> {
  if (!path.isAbsolute(executable)) throw new Error('Choose the full path to the Gemini CLI executable.');
  let isFile = false;
  try { isFile = (await fs.stat(executable)).isFile(); } catch { /* Report a useful selection error below. */ }
  if (!isFile) throw new Error('The selected Gemini CLI executable does not exist or is not a file.');
  const ext = path.extname(executable).toLowerCase();
  const command = ext === '.js' || ext === '.mjs' || ext === '.cjs'
    ? 'node'
    : process.platform === 'win32' && (ext === '.cmd' || ext === '.bat')
      ? (process.env.COMSPEC || 'cmd.exe')
      : executable;
  const args = ext === '.js' || ext === '.mjs' || ext === '.cjs'
    ? [executable, '--version']
    : process.platform === 'win32' && (ext === '.cmd' || ext === '.bat')
      ? ['/d', '/s', '/c', executable, '--version']
      : ['--version'];
  await new Promise<void>((resolve, reject) => {
    execFile(command, args, { encoding: 'utf8', timeout: 10_000, windowsHide: true, maxBuffer: 64 * 1024 }, (error, stdout, stderr) => {
      if (error) reject(new Error(`Could not run the selected Gemini CLI executable: ${String(stderr || error.message).trim()}`));
      else if (!/\d+\.\d+\.\d+/.test(`${stdout}\n${stderr}`)) reject(new Error('The selected file does not identify itself as Gemini CLI. Choose the Gemini CLI executable.'));
      else resolve();
    });
  });
}
