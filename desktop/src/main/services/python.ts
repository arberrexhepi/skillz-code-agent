import { execFile } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { hostEnvironment } from './hostEnvironment';

export interface PythonCommand {
  executable: string;
  args: string[];
}

// Keep launcher arguments separate so paths with spaces never need a shell.
export async function resolvePythonCommand(
  agentRoot: string,
  platform: NodeJS.Platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
): Promise<PythonCommand> {
  env = hostEnvironment(env, platform);
  const paths = platform === 'win32' ? path.win32 : path.posix;
  const configured = env.PYTHON_AGENT_PYTHON?.trim();
  const localPython = platform === 'win32'
    ? paths.join(agentRoot, '.venv', 'Scripts', 'python.exe')
    : paths.join(agentRoot, '.venv', 'bin', 'python');
  const preferred = configured || (existsSync(localPython) ? localPython : undefined);
  const candidates: PythonCommand[] = preferred
    ? [{ executable: preferred, args: [] }]
    : platform === 'win32'
      ? [{ executable: 'python', args: [] }, { executable: 'py', args: ['-3'] }, { executable: 'python3', args: [] }]
      : [{ executable: 'python3', args: [] }, { executable: 'python', args: [] }, ...(platform === 'darwin' ? [{ executable: '/opt/homebrew/bin/python3', args: [] }, { executable: '/usr/local/bin/python3', args: [] }] : [])];

  const failures: string[] = [];
  for (const candidate of candidates) {
    try {
      await new Promise<void>((resolve, reject) => {
        execFile(candidate.executable, [
          ...candidate.args, '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)',
        ], { cwd: agentRoot, env, timeout: 5_000, windowsHide: true, encoding: 'utf8' }, (error, _stdout, stderr) => {
          if (error) reject(new Error(String(stderr || error.message).trim()));
          else resolve();
        });
      });
      return candidate;
    } catch (error) {
      failures.push(`${[candidate.executable, ...candidate.args].join(' ')}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  const setup = platform === 'win32'
    ? 'Install Python 3.10 or newer with the py launcher, or run py -3 -m venv .venv in the repository root.'
    : 'Install Python 3.10 or newer, or run python3 -m venv .venv in the repository root.';
  throw new Error(
    `Could not find a working Python 3.10+ runtime. ${setup} `
    + 'Set PYTHON_AGENT_PYTHON to the Python executable path if needed (not a command with arguments). '
    + (preferred ? 'The configured or repository interpreter must be usable. ' : '')
    + `Tried: ${failures.join('; ')}`,
  );
}

export function pythonEnvironment(): NodeJS.ProcessEnv {
  // The bridge uses UTF-8 NDJSON even when Windows defaults to a legacy code page.
  return { ...hostEnvironment(), PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' };
}
