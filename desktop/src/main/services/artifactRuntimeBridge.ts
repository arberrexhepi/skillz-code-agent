import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import type { ArtifactRunSession } from './artifactRunSession';
import type { RunRepository } from './artifactRunRepositories';

async function body(request: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = []; let size = 0;
  for await (const chunk of request) { size += chunk.length; if (size > 24000) throw new Error('Request exceeds 24 KB.'); chunks.push(chunk); }
  const value = JSON.parse(Buffer.concat(chunks).toString());
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Expected a JSON object.');
  return value;
}
function send(response: ServerResponse, status: number, value: unknown): void {
  response.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' }); response.end(JSON.stringify(value));
}
export interface ArtifactRuntimeSetup {
  readiness?: () => Promise<{ ready: boolean; detail?: string }>;
  configure?: () => void;
  repositories?: () => Promise<RunRepository[]>;
}
export class ArtifactRuntimeBridge {
  private server?: Server;
  private cached?: { value: { ready: boolean; detail?: string }; expires: number };
  private checking?: Promise<{ ready: boolean; detail?: string }>;
  constructor(private readonly session: ArtifactRunSession, private readonly setup: ArtifactRuntimeSetup = {}) {}
  private async readiness(fresh = false): Promise<{ ready: boolean; detail?: string }> {
    if (!this.setup.readiness) return { ready: true };
    if (this.checking) { const value = await this.checking; if (!fresh) return value; }
    if (!fresh && this.cached && this.cached.expires > Date.now()) return this.cached.value;
    const checking = this.setup.readiness().catch(error => ({ ready: false, detail: String(error) }));
    this.checking = checking;
    try { const value = await checking; this.cached = { value, expires: Date.now() + 5000 }; return value; }
    finally { if (this.checking === checking) this.checking = undefined; }
  }
  private repositories(): Promise<RunRepository[]> {
    return this.setup.repositories?.() || Promise.resolve([{ id: 'repo', label: 'Observatory artifact', access: 'write' }]);
  }
  async start(): Promise<{ url: string; token: string }> {
    await this.session.load();
    const token = randomBytes(32).toString('base64url');
    const server = createServer((request, response) => void this.handle(request, response, token)); this.server = server;
    await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, '0.0.0.0', () => { server.off('error', reject); resolve(); }); });
    return { url: `http://host.docker.internal:${(server.address() as { port: number }).port}`, token };
  }
  private async handle(request: IncomingMessage, response: ServerResponse, token: string): Promise<void> {
    const supplied = request.headers['x-skillz-runtime-token'];
    if (typeof supplied !== 'string' || Buffer.byteLength(supplied) !== Buffer.byteLength(token) || !timingSafeEqual(Buffer.from(supplied), Buffer.from(token))) { send(response, 401, { error: 'Invalid runtime capability.' }); return; }
    // Browsers must use their own artifact server, which holds the capability. No CORS surface.
    if (request.headers.origin) { send(response, 403, { error: 'Use the artifact runtime gateway.' }); return; }
    try {
      const url = new URL(request.url || '/', 'http://runtime');
      if (request.method === 'GET' && url.pathname === '/v1/runtime') {
        const readiness = await this.readiness();
        send(response, 200, { ...this.session.summary(), repositories: await this.repositories(), ready: readiness.ready, setupMessage: readiness.detail, setupAvailable: Boolean(this.setup.configure) }); return;
      }
      const run = /^\/v1\/runs\/([a-f0-9-]{36})$/.exec(url.pathname);
      if (request.method === 'GET' && run) {
        const after = Number(url.searchParams.get('after') || 0);
        if (!Number.isSafeInteger(after) || after < 0) throw new Error('Invalid event cursor.');
        send(response, 200, this.session.get(run[1], after)); return;
      }
      if (request.method === 'POST' && url.pathname === '/v1/stop') { await this.session.stop(); send(response, 200, { ok: true }); return; }
      if (request.method !== 'POST') { send(response, 404, { error: 'Unknown runtime route.' }); return; }
      const input = await body(request);
      if (url.pathname === '/v1/configure' && this.setup.configure) {
        this.setup.configure(); this.cached = undefined; send(response, 200, { ok: true }); return;
      }
      const text = typeof input.text === 'string' ? input.text.trim() : '';
      if (url.pathname === '/v1/runs') {
        if (!text || text.length > 20000) throw new Error('Enter a prompt of 1–20,000 characters.');
        if (input.repositoryId !== undefined && typeof input.repositoryId !== 'string') throw new Error('Choose a repository from File Access.');
        const repository = (await this.repositories()).find(item => item.id === (input.repositoryId ?? 'repo'));
        if (!repository) throw new Error('This repository is not granted in File Access. Choose an available repository.');
        const readiness = await this.readiness(true);
        if (!readiness.ready) { send(response, 409, { error: readiness.detail || 'Configure the agent runtime in Workbench Setup before starting a run.' }); return; }
        send(response, 202, { id: await this.session.start(text, repository) }); return;
      }
      const continuation = /^\/v1\/runs\/([a-f0-9-]{36})\/continue$/.exec(url.pathname);
      if (continuation) {
        if (input.repositoryId !== undefined) throw new Error('A run is pinned to its working repository. Start a new run to switch repositories.');
        const action = typeof input.action === 'string' ? input.action : undefined;
        if ((!text && !action) || text.length > 20000 || (text && action)) throw new Error('Supply a message or one offered planner action.');
        await this.session.continue(continuation[1], { text, action }); send(response, 202, { ok: true }); return;
      }
      send(response, 404, { error: 'Unknown runtime route.' });
    } catch (error) { send(response, 400, { error: error instanceof Error ? error.message : String(error) }); }
  }
  async close(): Promise<void> {
    const server = this.server; this.server = undefined;
    try { await this.session.close(); }
    finally {
      server?.closeAllConnections();
      if (server) await new Promise<void>(resolve => server.close(() => resolve()));
    }
  }
}
