import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { hostEnvironment } from './hostEnvironment';
import { nodeRuntime } from './artifactProcess';
import type { ReadDirectory } from '../../shared/artifacts';

const MAX_BODY_BYTES = 32 * 1024;
const MAX_PROCESSES = 4;
const START_SCRIPT = /^(?:dev|start|serve|preview)(?::[A-Za-z0-9._-]+)?$/;
const PACKAGE_SCRIPT = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/;
const PORT = /(?:https?:\/\/)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::\])(?::|\s+)(\d{2,5})\b/gi;
const ANSI = /\u001b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g;

interface ProxyRequest { cwd: string; args: string[] }
interface ActiveProcess { child: ChildProcessWithoutNullStreams; ports: Set<number>; scanTail: string }
export interface ArtifactProcessProxyConnection { url: string; token: string }

function safeToken(received: string | undefined, expected: string): boolean {
  if (!received) return false;
  const left = Buffer.from(received), right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

async function requestJson(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const raw of request) {
    const chunk = Buffer.isBuffer(raw) ? raw : Buffer.from(raw);
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new Error('Process Proxy request is too large.');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function send(response: ServerResponse, status: number, payload: object): void {
  response.writeHead(status, { 'content-type': 'application/x-ndjson; charset=utf-8', 'cache-control': 'no-store' });
  response.end(JSON.stringify(payload) + '\n');
}

function stopGroup(child: ChildProcessWithoutNullStreams, signal: NodeJS.Signals = 'SIGTERM'): void {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === 'win32') { child.kill(signal); return; }
  try { process.kill(-child.pid, signal); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') child.kill(signal); }
}

function within(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative));
}

export class ArtifactProcessProxy {
  private server?: Server;
  private connection?: ArtifactProcessProxyConnection;
  private readonly active = new Set<ActiveProcess>();
  private closing = false;

  constructor(private readonly roots: ReadDirectory[]) {}

  async start(): Promise<ArtifactProcessProxyConnection> {
    if (this.connection) return this.connection;
    if (this.closing) throw new Error('Process Proxy is stopping.');
    const token = randomBytes(32).toString('base64url');
    const server = createServer((request, response) => void this.handle(request, response, token));
    this.server = server;
    const port = await new Promise<number>((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '0.0.0.0', () => {
        server.removeListener('error', reject);
        const address = server.address();
        if (!address || typeof address === 'string') { reject(new Error('Process Proxy did not bind a TCP port.')); return; }
        resolve(address.port);
      });
    });
    this.connection = { url: `http://host.docker.internal:${port}`, token };
    return this.connection;
  }

  origins(): string[] {
    return [...this.active].flatMap(({ ports }) => [...ports].map((port) => `http://127.0.0.1:${port}`));
  }

  private async hostDirectory(containerCwd: string): Promise<{ directory: string; root: ReadDirectory }> {
    const match = /^\/reads\/([a-z0-9][a-z0-9-]{0,63})(?:\/(.*))?$/.exec(containerCwd);
    if (!match) throw new Error('Commands must run inside a shared repository.');
    const root = this.roots.find((candidate) => candidate.id === match[1]);
    if (!root) throw new Error('This repository is not shared with the artifact.');
    if (!root.allowProcessProxy) throw new Error(`Process Proxy is not allowed for ${root.label}. Enable it in File access.`);
    const candidate = path.resolve(root.path, ...(match[2] ? match[2].split('/') : []));
    const directory = await fs.realpath(candidate);
    if (!within(root.path, directory) || !(await fs.stat(directory)).isDirectory()) throw new Error('Process Proxy working directory escaped the shared repository.');
    return { directory, root };
  }

  private async validate(raw: unknown): Promise<{ directory: string; args: string[] }> {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid Process Proxy request.');
    const { cwd, args } = raw as Partial<ProxyRequest>;
    if (typeof cwd !== 'string' || !Array.isArray(args) || args.some((item) => typeof item !== 'string' || item.length > 500 || /[\0\r\n]/.test(item))) throw new Error('Invalid npm command.');
    if (!['run', 'run-script'].includes(args[0] || '') || !PACKAGE_SCRIPT.test(args[1] || '') || args.length !== 2) throw new Error('Process Proxy requires one validated npm package script.');
    const { directory, root } = await this.hostDirectory(cwd);
    const manifestPath = path.join(directory, 'package.json');
    const stat = await fs.lstat(manifestPath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 512 * 1024) throw new Error('A regular package.json smaller than 512 KB is required.');
    const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8')) as { scripts?: Record<string, unknown> };
    if (typeof manifest.scripts?.[args[1]] !== 'string') throw new Error(`package.json no longer defines ${args[1]}.`);
    const allowlist = root.processProxyAllowlist ?? Object.keys(manifest.scripts || {}).filter((name) => START_SCRIPT.test(name));
    if (!allowlist.includes(args[1])) throw new Error(`${args[1]} is disallowed by the Process Proxy allowlist for ${root.label}.`);
    return { directory, args };
  }

  private async handle(request: IncomingMessage, response: ServerResponse, token: string): Promise<void> {
    if (request.method !== 'POST' || request.url !== '/v1/npm') { send(response, 404, { error: 'Unknown Process Proxy route.' }); return; }
    if (!safeToken(request.headers['x-skillz-process-token'] as string | undefined, token)) { send(response, 401, { error: 'Invalid Process Proxy capability.' }); return; }
    if (this.active.size >= MAX_PROCESSES) { send(response, 429, { error: `Process Proxy permits at most ${MAX_PROCESSES} concurrent processes per artifact.` }); return; }
    try {
      const command = await this.validate(await requestJson(request));
      const runtime = await nodeRuntime();
      response.writeHead(200, { 'content-type': 'application/x-ndjson; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
      const child = spawn(runtime.node, [runtime.npm, ...command.args], {
        cwd: command.directory,
        env: { ...hostEnvironment(), FORCE_COLOR: '0' },
        shell: false,
        detached: process.platform !== 'win32',
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      child.stdin.end();
      const active = { child, ports: new Set<number>(), scanTail: '' };
      this.active.add(active);
      let finished = false;
      const finish = (exitCode: number, signal: NodeJS.Signals | null): void => {
        if (finished) return;
        finished = true;
        this.active.delete(active);
        if (!response.destroyed) response.end(JSON.stringify({ exitCode, signal }) + '\n');
      };
      const write = (stream: 'stdout' | 'stderr', chunk: Buffer): void => {
        const text = (active.scanTail + chunk.toString('utf8')).replace(ANSI, '');
        active.scanTail = text.slice(-256);
        PORT.lastIndex = 0;
        for (let match = PORT.exec(text); match; match = PORT.exec(text)) {
          const port = Number(match[1]);
          if (port <= 0 || port > 65535 || active.ports.has(port)) continue;
          active.ports.add(port);
          if (!response.destroyed) response.write(JSON.stringify({ stream: 'stdout', data: Buffer.from(`\nProcess Proxy listening: http://127.0.0.1:${port}\n`).toString('base64') }) + '\n');
        }
        if (!response.destroyed) response.write(JSON.stringify({ stream, data: chunk.toString('base64') }) + '\n');
      };
      child.stdout.on('data', (chunk: Buffer) => write('stdout', chunk));
      child.stderr.on('data', (chunk: Buffer) => write('stderr', chunk));
      child.once('error', (error) => {
        if (!response.destroyed) response.write(JSON.stringify({ error: error.message }) + '\n');
        finish(1, null);
      });
      child.once('exit', (code, signal) => {
        finish(code ?? (signal ? 1 : 0), signal);
      });
      response.once('close', () => {
        if (response.writableEnded) return;
        stopGroup(child);
        const timeout = setTimeout(() => stopGroup(child, 'SIGKILL'), 5000);
        timeout.unref();
      });
    } catch (error) {
      send(response, 400, { error: error instanceof Error ? error.message : 'Process Proxy rejected the request.' });
    }
  }

  async close(): Promise<void> {
    if (this.closing) return;
    this.closing = true;
    const server = this.server;
    this.server = undefined;
    this.connection = undefined;
    const closed = server ? new Promise<void>((resolve) => server.close(() => resolve())) : Promise.resolve();
    const children = [...this.active].map(({ child }) => child);
    for (const child of children) stopGroup(child);
    if (children.length) await Promise.race([
      Promise.all(children.map((child) => new Promise<void>((resolve) => child.once('close', () => resolve())))),
      new Promise<void>((resolve) => setTimeout(resolve, 5000)),
    ]);
    for (const child of children) stopGroup(child, 'SIGKILL');
    this.active.clear();
    server?.closeAllConnections();
    await closed;
  }
}
