import { execFile, spawn, type ChildProcess, type ChildProcessWithoutNullStreams, type SpawnOptionsWithoutStdio } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { hostEnvironment } from './hostEnvironment';

export function command(executable: string, args: string[], cwd: string): Promise<string> {
  return new Promise((resolve, reject) => execFile(executable, args, { cwd, env: hostEnvironment(), windowsHide: true, encoding: 'utf8', timeout: 30000, maxBuffer: 2 * 1024 * 1024 }, (error, stdout, stderr) => error ? reject(new Error(String(stderr || error.message).trim())) : resolve(stdout.trim())));
}
export function git(root: string, ...args: string[]): Promise<string> { return command('git', args, root); }
export async function nodeRuntime(): Promise<{ node: string; npm: string }> {
  const executable = process.platform === 'win32' ? 'node.exe' : 'node';
  const directories = (hostEnvironment().PATH || '').split(path.delimiter);
  if (process.platform === 'win32') directories.push(path.join(process.env.ProgramFiles || 'C:/Program Files', 'nodejs'));
  for (const directory of directories) {
    const node = path.join(directory.replace(/^"|"$/g, ''), executable);
    try {
      if (!(await fs.stat(node)).isFile()) continue;
      const real = await fs.realpath(node);
      const npmCandidates = [path.join(path.dirname(real), 'node_modules/npm/bin/npm-cli.js'), path.resolve(path.dirname(real), '../lib/node_modules/npm/bin/npm-cli.js')];
      for (const npm of npmCandidates) if (await fs.stat(npm).then((stat) => stat.isFile()).catch(() => false)) return { node: real, npm };
    } catch { /* Continue through PATH entries. */ }
  }
  throw new Error('Node.js with npm was not found. Install Node.js 22.12 or later, add it to PATH, and restart the desktop app.');
}
const modelGroups = new WeakSet<ChildProcess>();

/** Give model helpers their own POSIX process group so cancellation also stops the Codex CLI. */
export function spawnModelHelper(executable: string, args: string[], options: SpawnOptionsWithoutStdio): ChildProcessWithoutNullStreams {
  const child = spawn(executable, args, { ...options, stdio: 'pipe', windowsHide: true, detached: process.platform !== 'win32' });
  if (process.platform !== 'win32') {
    modelGroups.add(child);
    // A helper can exit while its CLI still holds inherited stdout/stderr open.
    child.once('exit', () => { void terminate(child).catch(() => {}); });
  }
  return child;
}
export async function terminate(child: ChildProcess): Promise<void> {
  if (!child.pid) return;
  if (modelGroups.has(child)) {
    // Only send group signals to helpers launched by spawnModelHelper, never to
    // an arbitrary child that might share Electron's own process group.
    try { process.kill(-child.pid, 'SIGKILL'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error; }
    modelGroups.delete(child);
    return;
  }
  if (child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === 'win32') {
    await command('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], process.cwd()).catch(() => { child.kill(); });
  } else child.kill('SIGTERM');
}
export function runLogged(executable: string, args: string[], cwd: string, onLog: (text: string) => void, onChild?: (child: ChildProcess) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { cwd, windowsHide: true, env: { ...hostEnvironment(), ELECTRON_RUN_AS_NODE: '1', FORCE_COLOR: '0' } });
    onChild?.(child);
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    let tail = '';
    const logged = (text: string) => { tail = (tail + text).slice(-3000); onLog(text); };
    child.stdout.on('data', logged); child.stderr.on('data', logged);
    child.on('error', reject);
    child.on('exit', (code) => code === 0 ? resolve() : reject(new Error(`Process exited with code ${code}. ${tail.trim()}`)));
  });
}
