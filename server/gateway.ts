import { fileRouter } from './files';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import type { Server } from 'node:http';
import type { Express, Request } from 'express';
import { WebSocket, WebSocketServer } from 'ws';
import { Ajv, type ValidateFunction } from 'ajv';
import { artifactApisSchema, type ArtifactApiConfig } from './schema';

const ajv = new Ajv({ allErrors: true, strict: true });
function validate(schema: Record<string, unknown>, value: unknown): void {
  const check: ValidateFunction = ajv.compile(schema);
  if (!check(value)) throw new Error(`Payload does not match configured shape: ${ajv.errorsText(check.errors)}`);
}
async function definitions(root: string): Promise<ArtifactApiConfig[]> {
  const text = await readFile(path.join(root, '.artifact/apis.json'), 'utf8');
  if (text.length > 256000) throw new Error('API configuration exceeds 256 KB.');
  return artifactApisSchema.parse(JSON.parse(text)).apis;
}
function headers(api: ArtifactApiConfig): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [name, variable] of Object.entries(api.headerEnv)) {
    if (/^(host|origin|connection|upgrade|content-length)$/i.test(name)) throw new Error(`Header ${name} cannot be overridden.`);
    const value = process.env[variable];
    if (!value) throw new Error(`Environment variable ${variable} is not set.`);
    result[name] = value;
  }
  return result;
}
export function sameOrigin(request: Pick<Request, 'headers'>, port: number): boolean {
  const host = process.env.SKILLZ_ARTIFACT_PORT ? String(request.headers.host || '') : `127.0.0.1:${port}`;
  if (!/^127\.0\.0\.1:\d+$/.test(host)) return false;
  return request.headers.host === host && (!request.headers.origin || request.headers.origin === `http://${host}`);
}
export function attachGateway(app: Express, server: Server, root: string): () => void {
  const port = () => (server.address() as { port: number } | null)?.port || 0;
  app.use((request, response, next) => {
    if (!sameOrigin(request, port())) { response.status(403).json({ error: 'Only this artifact origin is allowed.' }); return; }
    next();
  });
  app.use('/files', fileRouter(root));
  app.get('/context/:id', async (request, response) => {
    const names: Record<string, string> = { 'repo-facts': 'repo_facts.md', memory: 'memory_observability.md' };
    const name = names[request.params.id];
    if (!name) { response.status(404).json({ error: 'Unknown context file.' }); return; }
    try {
      const text = await readFile(path.join(process.env.SKILLZ_CONTEXT_ROOT || path.join(root, '.context'), name), 'utf8');
      if (text.length > 5000000) throw new Error('Context file exceeds 5 MB.');
      response.type('text/plain').send(text);
    } catch { response.status(404).json({ error: 'This context file is not shared or does not exist yet.' }); }
  });
  app.all('/api/:id', async (request, response) => {
    try {
      const api = (await definitions(root)).find((item) => item.id === request.params.id && item.transport === 'http');
      if (!api) { response.status(404).json({ error: 'Unknown API configuration ID.' }); return; }
      if (request.method !== api.method) { response.setHeader('Allow', api.method); response.status(405).json({ error: `Use ${api.method}.` }); return; }
      const payload = api.method === 'GET' ? request.query : request.body;
      validate(api.requestSchema, payload ?? {});
      const target = new URL(api.url);
      if (api.method === 'GET') for (const [key, value] of Object.entries(request.query)) {
        if (typeof value !== 'string') throw new Error('Query parameters must be strings.');
        target.searchParams.set(key, value);
      }
      const upstream = await fetch(target, { method: api.method, headers: { 'Content-Type': 'application/json', ...headers(api) }, body: api.method === 'GET' ? undefined : JSON.stringify(payload ?? {}), redirect: 'error', signal: AbortSignal.timeout(15000) });
      if (!upstream.ok) { response.status(502).json({ error: `Upstream returned HTTP ${upstream.status}.` }); return; }
      const reader = upstream.body?.getReader();
      if (!reader) throw new Error('Upstream returned no JSON body.');
      const chunks: Uint8Array[] = []; let size = 0;
      try { while (true) { const { done, value } = await reader.read(); if (done) break; size += value.length; if (size > 5000000) throw new Error('Upstream response exceeds 5 MB.'); chunks.push(value); } }
      finally { await reader.cancel(); }
      const value: unknown = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      validate(api.responseSchema, value);
      response.json(value);
    } catch (error) { response.status(502).json({ error: error instanceof Error ? error.message : 'API request failed.' }); }
  });
  const sockets = new WebSocketServer({ noServer: true, maxPayload: 1024 * 1024 });
  const upstreams = new Set<WebSocket>();
  server.on('upgrade', (request, socket, head) => {
    // Leave Vite's HMR upgrade listener alone.
    if (!request.url?.startsWith('/ws/')) return;
    void (async () => {
      if (!sameOrigin(request, port())) throw new Error('Origin rejected.');
      const id = new URL(request.url!, 'http://localhost').pathname.slice(4);
      const api = (await definitions(root)).find((item) => item.id === id && item.transport === 'websocket');
      if (!api) throw new Error('Unknown WebSocket configuration ID.');
      // Compile both schemas before accepting a connection.
      const outgoing = ajv.compile(api.requestSchema), incoming = ajv.compile(api.responseSchema);
      const upstream = new WebSocket(api.url, { headers: headers(api), followRedirects: false, handshakeTimeout: 15000, maxPayload: 1024 * 1024 });
      upstreams.add(upstream);
      sockets.handleUpgrade(request, socket, head, (client) => {
        const queue: string[] = [];
        const sendError = (message: string) => { if (client.readyState === WebSocket.OPEN) client.send(JSON.stringify({ error: message })); };
        client.on('message', (raw) => {
          try { const value = JSON.parse(raw.toString()); if (!outgoing(value)) throw new Error('Outgoing message does not match requestSchema.'); const data = JSON.stringify(value); if (upstream.readyState === WebSocket.OPEN) upstream.send(data); else if (queue.length < 32) queue.push(data); else throw new Error('Upstream is not ready.'); }
          catch (error) { sendError(String(error)); }
        });
        upstream.on('open', () => { for (const data of queue) upstream.send(data); queue.length = 0; });
        upstream.on('message', (raw) => {
          try { const value = JSON.parse(raw.toString()); if (!incoming(value)) throw new Error('Incoming message does not match responseSchema.'); if (client.readyState === WebSocket.OPEN) client.send(JSON.stringify(value)); }
          catch (error) { sendError(String(error)); }
        });
        upstream.on('error', () => { sendError('Upstream WebSocket connection failed.'); client.close(1011); });
        upstream.on('close', () => { upstreams.delete(upstream); client.close(); });
        client.on('close', () => { upstream.terminate(); upstreams.delete(upstream); });
        client.on('error', () => upstream.terminate());
      });
    })().catch(() => { socket.write('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'); socket.destroy(); });
  });
  return () => { for (const socket of upstreams) socket.terminate(); for (const client of sockets.clients) client.terminate(); sockets.close(); };
}
