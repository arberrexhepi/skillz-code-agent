import { execFile, spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { createInterface } from 'node:readline';
import type { McpProfile, McpServerSettings } from '../../shared/mcp';
import { harnessRoot } from './artifactSandbox';
import { pythonEnvironment, resolvePythonCommand, type PythonCommand } from './python';

export interface McpScope { root: string; profile: string }
type Payload = Record<string, unknown>;

export class McpHost {
  private setupTask?: Promise<boolean>;
  constructor(private readonly home: string, private readonly harness = harnessRoot) {}

  private managedPython(): string {
    return path.join(this.home, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  }

  private async python(): Promise<PythonCommand> {
    const managed = this.managedPython();
    return existsSync(managed) ? { executable: managed, args: [] } : resolvePythonCommand(this.harness());
  }

  private exec(python: PythonCommand, args: string[], timeout = 20_000): Promise<string> {
    return new Promise((resolve, reject) => execFile(python.executable, [...python.args, ...args],
      { cwd: this.harness(), env: pythonEnvironment(), windowsHide: true, timeout, maxBuffer: 1024 * 1024 },
      (error, stdout) => error && !stdout ? reject(error) : resolve(stdout)));
  }

  private args(scope: McpScope, args: string[]): string[] {
    return [path.join(this.harness(), 'skillz_mcp.py'), '--root', scope.root, '--scope', scope.profile, ...args];
  }

  private async request(scope: McpScope, args: string[], timeout?: number): Promise<Payload> {
    const result = JSON.parse(await this.exec(await this.python(), this.args(scope, args), timeout)) as Payload;
    if (result.ok !== true) throw new Error(String(result.error || 'MCP request failed.'));
    return result;
  }

  async ready(): Promise<boolean> {
    try {
      return (await this.exec(await this.python(), ['-c', "from mcp.client.auth import OAuthClientProvider; print('ready')"])).trim() === 'ready';
    } catch { return false; }
  }

  setup(): Promise<boolean> {
    if (!this.setupTask) this.setupTask = (async () => {
      if (await this.ready()) return true;
      await this.exec(await resolvePythonCommand(this.harness()), ['-m', 'venv', this.home], 60_000);
      await this.exec(await this.python(), ['-m', 'pip', 'install', '--disable-pip-version-check',
        '-r', path.join(this.harness(), 'requirements-mcp.txt')], 180_000);
      if (!await this.ready()) throw new Error('The MCP runtime did not install. Check connectivity and the Python setup.');
      return true;
    })().finally(() => { this.setupTask = undefined; });
    return this.setupTask;
  }

  async profile(scope: McpScope): Promise<McpProfile> {
    const [result, ready] = await Promise.all([this.request(scope, ['list']), this.ready()]);
    return { ready, servers: result.servers as McpProfile['servers'] };
  }

  async isEnabled(scope: McpScope, name: string): Promise<boolean> {
    const { servers } = await this.request(scope, ['list']);
    const server = (servers as McpProfile['servers'])[name];
    return Boolean(server && server.enabled !== false);
  }

  async save(scope: McpScope, name: string, settings: McpServerSettings): Promise<McpProfile> {
    await this.request(scope, ['add', name, JSON.stringify(settings)]);
    return this.profile(scope);
  }

  async manage(scope: McpScope, action: string, name: string): Promise<McpProfile> {
    await this.request(scope, [action, name], action === 'login' ? 285_000 : undefined);
    return this.profile(scope);
  }

  async enablePlaywright(scope: McpScope): Promise<void> {
    await this.request(scope, ['enable', 'playwright']);
  }

  async inspect(scope: McpScope, server: string, kind: string, query = '', offset = 0): Promise<Payload> {
    return this.request(scope, ['request', JSON.stringify({ operation: 'search', server, kind, query, offset })], 310_000);
  }

  broker(scope: McpScope): McpBroker {
    return new McpBroker(async () => {
      const python = await this.python();
      return spawn(python.executable, [...python.args, ...this.args(scope, ['broker'])], {
        cwd: this.harness(), env: pythonEnvironment(), windowsHide: true,
      });
    });
  }
}

export class McpBroker {
  private process?: Promise<ChildProcessWithoutNullStreams>;
  private stopped = false;
  private pending = new Map<string, (payload: Payload) => void>();
  constructor(private readonly launch: () => Promise<ChildProcessWithoutNullStreams>) {}

  private connect(): Promise<ChildProcessWithoutNullStreams> {
    if (this.process) return this.process;
    const session = this.launch().then((child) => {
      if (this.stopped) { child.stdin.end(); throw new Error('MCP broker stopped.'); }
      child.stderr.resume();
      createInterface({ input: child.stdout }).on('line', (line) => {
        try {
          const response = JSON.parse(line) as { id: string; payload: Payload };
          this.pending.get(response.id)?.(response.payload);
        } catch { /* Isolate malformed replies from other active requests. */ }
      });
      const disconnected = () => {
        if (this.process !== session) return;
        this.process = undefined;
        for (const settle of this.pending.values()) settle({ ok: false, error: 'MCP broker disconnected.' });
      };
      child.stdin.once('error', disconnected);
      child.once('error', disconnected);
      child.once('close', disconnected);
      return child;
    });
    this.process = session;
    void session.catch(() => { if (this.process === session) this.process = undefined; });
    return session;
  }

  async invoke(id: string, action: Payload): Promise<Payload> {
    if (this.stopped) return { ok: false, error: 'MCP broker stopped.' };
    if (this.pending.has(id) || this.pending.size >= 12) return { ok: false, error: 'Too many pending MCP requests.' };
    try {
      const child = await this.connect();
      if (this.stopped) return { ok: false, error: 'MCP broker stopped.' };
      if (this.pending.has(id) || this.pending.size >= 12) return { ok: false, error: 'Too many pending MCP requests.' };
      return await new Promise((resolve) => {
        const settle = (payload: Payload) => { clearTimeout(timer); this.pending.delete(id); resolve(payload); };
        const timer = setTimeout(() => settle({ ok: false, error: 'MCP broker timed out.' }), 310_000);
        this.pending.set(id, settle);
        child.stdin.write(JSON.stringify({ id, action }) + '\n');
      });
    } catch (error) { return { ok: false, error: error instanceof Error ? error.message : String(error) }; }
  }

  stop(): void {
    this.stopped = true;
    for (const settle of this.pending.values()) settle({ ok: false, error: 'MCP broker stopped.' });
    void this.process?.then((child) => {
      child.stdin.end();
      const timer = setTimeout(() => child.kill(), 5_000);
      timer.unref();
      child.once('close', () => clearTimeout(timer));
    }, () => {});
  }
}
