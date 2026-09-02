import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';

export class RuntimeSettingsService {
  constructor(private readonly filePath: string) {}

  async codexCliPath(): Promise<string> {
    try {
      const settings: unknown = JSON.parse(await fs.readFile(this.filePath, 'utf8'));
      if (!settings || typeof settings !== 'object' || Array.isArray(settings)) throw new Error('Invalid runtime settings.');
      const value = (settings as { codexCliPath?: unknown }).codexCliPath;
      if (value !== undefined && typeof value !== 'string') throw new Error('Invalid saved Codex CLI path.');
      return value || '';
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return '';
      throw error;
    }
  }

  async setCodexCliPath(candidate: string | null): Promise<void> {
    const executable = candidate?.trim() || '';
    if (executable) await validateCodexExecutable(executable);
    // A failed validation leaves the previous selection untouched.
    await fs.mkdir(path.dirname(this.filePath), { recursive: true });
    const temporary = `${this.filePath}.${randomUUID()}.tmp`;
    try {
      await fs.writeFile(temporary, JSON.stringify({ codexCliPath: executable }, null, 2) + '\n', { mode: 0o600 });
      await fs.rename(temporary, this.filePath);
    } finally {
      await fs.rm(temporary, { force: true });
    }
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
