import { spawn, type ChildProcessByStdio } from 'node:child_process';
import type { Readable } from 'node:stream';
import { randomUUID } from 'node:crypto';
import { access, readFile, realpath } from 'node:fs/promises';
import path from 'node:path';
import { Router } from 'express';

interface Root {
  id: string;
  label: string;
  path: string;
  access?: 'read' | 'write';
  allowProcessProxy?: boolean;
  processProxyAllowlist?: string[];
}

interface CommandCandidate {
  id: string;
  label: string;
  command: string;
  args: string[];
  source: 'package.json';
  kind: 'server' | 'task';
  primary: boolean;
}

interface ManagedProcess {
  id: string;
  rootId: string;
  candidateId: string;
  child: ChildProcessByStdio<null, Readable, Readable>;
  status: 'starting' | 'running' | 'stopping' | 'stopped' | 'failed';
  startedAt: string;
  stoppedAt?: string;
  exitCode?: number | null;
  signal?: NodeJS.Signals | null;
  ports: Set<number>;
  logs: string;
}

const MAX_PROCESSES = 4;
const MAX_LOG_BYTES = 128 * 1024;
const MAX_MANIFEST_BYTES = 512 * 1024;
const SCRIPT_NAME = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/;
const SERVER_SCRIPT = /^(?:dev|start|serve|preview)(?::[A-Za-z0-9._-]+)?$/;
const SCRIPT_PRIORITY = ['dev', 'start', 'serve', 'preview'];
const URL_PORT = /(?:https?:\/\/)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::\])(?::|\s+)(\d{2,5})\b/gi;
const PORT_PHRASE = /\b(?:listening|server|started|running|ready|available)\b[^\n]{0,80}?\bport\s*(?:[:=]|\bat\b)?\s*(\d{2,5})\b/gi;

function configuredRoots(repository: string): Root[] {
  const value: unknown = JSON.parse(process.env.SKILLZ_READ_ROOTS ?? process.env.SKILLZ_ARTIFACT_READ_ROOTS ?? '[]');
  if (!Array.isArray(value)) throw new Error('Invalid runtime folder grants.');

  const roots: Root[] = value as Root[];

  if (
    roots.some((root) =>
      !root ||
      !/^[a-z0-9][a-z0-9-]{0,63}$/.test(root.id) ||
      typeof root.label !== 'string' ||
      !path.isAbsolute(root.path) ||
      !['read', 'write'].includes(root.access || 'read')
    ) ||
    new Set(roots.map((root) => root.id)).size !== roots.length
  ) {
    throw new Error('Invalid runtime folder grants.');
  }

  return roots;
}

async function rootPath(root: Root): Promise<string> {
  const configured = path.resolve(root.path);
  if (configured !== root.path) {
    throw new Error('Repository grant path must be absolute and normalized.');
  }
  return realpath(configured);
}

async function candidates(root: Root): Promise<CommandCandidate[]> {
  const directory = await rootPath(root);
  const manifestPath = path.join(directory, 'package.json');
  const metadata = await access(manifestPath)
    .then(async () => {
      const text = await readFile(manifestPath, 'utf8');
      if (Buffer.byteLength(text) > MAX_MANIFEST_BYTES) throw new Error('package.json exceeds 512 KB.');
      return JSON.parse(text) as { scripts?: Record<string, unknown> };
    })
    .catch((error: unknown) => {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
      throw error;
    });

  if (!metadata?.scripts || typeof metadata.scripts !== 'object') return [];

  const names = Object.entries(metadata.scripts)
    .filter((entry): entry is [string, string] => SCRIPT_NAME.test(entry[0]) && typeof entry[1] === 'string')
    .map(([name]) => name);
  const allowed = root.processProxyAllowlist ?? names.filter((name) => SERVER_SCRIPT.test(name));
  const visible = names.filter((name) => allowed.includes(name));
  const primaryName = SCRIPT_PRIORITY.find((name) => visible.includes(name)) ?? visible.find((name) => SERVER_SCRIPT.test(name));
  return visible.map((name) => ({
      id: `npm-${name}`,
      label: `npm run ${name}`,
      command: 'npm',
      args: ['run', name],
      source: 'package.json' as const,
      kind: SERVER_SCRIPT.test(name) ? 'server' as const : 'task' as const,
      primary: name === primaryName,
    })).sort((left, right) => Number(right.primary) - Number(left.primary) || Number(right.kind === 'server') - Number(left.kind === 'server') || left.label.localeCompare(right.label));
}

function appendLog(process: ManagedProcess, chunk: Buffer): void {
  const text = chunk.toString('utf8');
  process.logs = (process.logs + text).slice(-MAX_LOG_BYTES);

  for (const expression of [URL_PORT, PORT_PHRASE]) {
    expression.lastIndex = 0;
    for (let match = expression.exec(text); match; match = expression.exec(text)) {
      const port = Number(match[1]);
      if (port > 0 && port <= 65535) process.ports.add(port);
    }
  }
}

function serialize(process: ManagedProcess) {
  return {
    id: process.id,
    rootId: process.rootId,
    candidateId: process.candidateId,
    pid: process.child.pid,
    status: process.status,
    startedAt: process.startedAt,
    stoppedAt: process.stoppedAt,
    exitCode: process.exitCode,
    signal: process.signal,
    ports: [...process.ports].sort((a, b) => a - b),
    logs: process.logs,
  };
}

export function processManager(repository: string): { router: Router; close: () => void } {
  const roots = configuredRoots(repository);
  const processes = new Map<string, ManagedProcess>();
  const router = Router();

  const stop = (managed: ManagedProcess): void => {
    if (managed.status === 'stopped' || managed.status === 'failed' || managed.status === 'stopping') return;
    managed.status = 'stopping';
    managed.child.kill('SIGTERM');
    const timeout = setTimeout(() => {
      if (!managed.child.killed || managed.status === 'stopping') managed.child.kill('SIGKILL');
    }, 5000);
    timeout.unref();
  };

  router.get('/repositories', async (_request, response) => {
    const inventory = await Promise.all(roots.map(async (root) => {
      try {
        return {
          id: root.id,
          label: root.label,
          readOnly: (root.access || 'read') !== 'write',
          candidates: await candidates(root),
          processes: [...processes.values()].filter((item) => item.rootId === root.id).map(serialize),
        };
      } catch (error) {
        return {
          id: root.id,
          label: root.label,
          readOnly: (root.access || 'read') !== 'write',
          candidates: [],
          processes: [...processes.values()].filter((item) => item.rootId === root.id).map(serialize),
          error: error instanceof Error ? error.message : 'Repository inspection failed.',
        };
      }
    }));
    response.json({ repositories: inventory, limits: { concurrentProcesses: MAX_PROCESSES, logBytes: MAX_LOG_BYTES } });
  });

  router.post('/repositories/:rootId/start', async (request, response) => {
    try {
      const root = roots.find((item) => item.id === request.params.rootId);
      if (!root) throw new Error('This repository is not shared.');
      if (typeof request.body?.candidateId !== 'string') throw new Error('Select a detected command candidate.');

      const active = [...processes.values()].filter((item) => item.status === 'starting' || item.status === 'running');
      if (active.length >= MAX_PROCESSES) throw new Error(`At most ${MAX_PROCESSES} managed servers may run concurrently.`);
      if (active.some((item) => item.rootId === root.id)) throw new Error('This repository already has a managed server.');

      const detected = await candidates(root);
      const candidate = detected.find((item) => item.id === request.body.candidateId);
      if (!candidate) throw new Error('That command is not a current validated candidate.');

      const directory = await rootPath(root);
      await access(path.join(directory, 'node_modules')).catch(() => {
        throw new Error('Dependencies are unavailable. Install them outside Server Manager before launching this read-only repository.');
      });

      const child = spawn(candidate.command, candidate.args, {
        cwd: directory,
        env: { ...process.env, FORCE_COLOR: '0' },
        shell: false,
        detached: process.platform !== 'win32',
        stdio: ['ignore', 'pipe', 'pipe'],
      });

      const managed: ManagedProcess = {
        id: randomUUID(),
        rootId: root.id,
        candidateId: candidate.id,
        child,
        status: 'starting',
        startedAt: new Date().toISOString(),
        ports: new Set(),
        logs: '',
      };
      processes.set(managed.id, managed);

      child.stdout.on('data', (chunk: Buffer) => appendLog(managed, chunk));
      child.stderr.on('data', (chunk: Buffer) => appendLog(managed, chunk));
      child.once('spawn', () => { managed.status = 'running'; });
      child.once('error', (error) => {
        appendLog(managed, Buffer.from(`\nLaunch failed: ${error.message}\n`));
        managed.status = 'failed';
        managed.stoppedAt = new Date().toISOString();
      });
      child.once('exit', (code, signal) => {
        managed.exitCode = code;
        managed.signal = signal;
        managed.status = code === 0 || managed.status === 'stopping' ? 'stopped' : 'failed';
        managed.stoppedAt = new Date().toISOString();
      });

      response.status(202).json({ process: serialize(managed) });
    } catch (error) {
      response.status(400).json({ error: error instanceof Error ? error.message : 'Unable to launch repository.' });
    }
  });

  router.post('/processes/:processId/stop', (request, response) => {
    const managed = processes.get(request.params.processId);
    if (!managed) {
      response.status(404).json({ error: 'Unknown managed process.' });
      return;
    }
    stop(managed);
    response.status(202).json({ process: serialize(managed) });
  });

  return {
    router,
    close: () => {
      for (const managed of processes.values()) {
        if (managed.status === 'starting' || managed.status === 'running') {
          managed.status = 'stopping';
          if (process.platform !== 'win32' && managed.child.pid) {
            try { process.kill(-managed.child.pid, 'SIGTERM'); } catch { managed.child.kill('SIGTERM'); }
          } else {
            managed.child.kill('SIGTERM');
          }
        }
      }
    },
  };
}
