import { fileRouter } from './files';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import type { Server } from 'node:http';
import type { Express, Request } from 'express';
import { WebSocket, WebSocketServer } from 'ws';
import { Ajv, type ValidateFunction } from 'ajv';
import { artifactApisSchema, type ArtifactApiConfig } from './schema';
import { appConfig, databaseCommands, executeDatabaseCommand } from './database';

const ajv = new Ajv({ allErrors: true, strict: true });
function validate(schema: Record<string, unknown>, value: unknown): void {
  const check: ValidateFunction = ajv.compile(schema);
  if (!check(value)) throw new Error(`Payload does not match configured shape: ${ajv.errorsText(check.errors)}`);
}
export async function definitions(root: string): Promise<ArtifactApiConfig[]> {
  const text = await readFile(path.join(root, '.artifact/apis.json'), 'utf8');
  if (text.length > 256000) throw new Error('API configuration exceeds 256 KB.');
  return artifactApisSchema.parse(JSON.parse(text)).apis;
}
function headers(api: ArtifactApiConfig): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [name, setting] of Object.entries(api.headerEnv)) {
    if (/^(host|origin|connection|upgrade|content-length)$/i.test(name)) throw new Error(`Header ${name} cannot be overridden.`);
    const variable = typeof setting === 'string' ? setting : setting.env; const value = process.env[variable];
    if (!value) throw new Error(`Environment variable ${variable} is not set.`);
    result[name] = (typeof setting === 'string' ? '' : setting.prefix) + value;
  }
  return result;
}
function commandKind(api: ArtifactApiConfig): 'discovery' | 'mutation' { return api.commandKind || (api.method === 'GET' ? 'discovery' : 'mutation'); }
function valueAt(value: unknown, selector: string): unknown {
  if (!selector) return value;
  return selector.replace(/^\$\.?/, '').split('.').filter(Boolean).reduce<unknown>((current, part) => current && typeof current === 'object' ? (current as Record<string, unknown>)[part] : undefined, value);
}
function interpolate(text: string, values: Record<string, unknown>, used = new Set<string>()): string {
  return text.replace(/\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}/g, (_match, key: string) => {
    const value = valueAt(values, key); if (value == null) throw new Error(`Missing template value: ${key}`); used.add(key.split('.')[0]); return encodeURIComponent(String(value));
  });
}
function materialize(value: unknown, variables: Record<string, unknown>): unknown {
  if (typeof value === 'string') {
    const exact = /^\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}$/.exec(value);
    if (exact) return valueAt(variables, exact[1]);
    return value.replace(/\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}/g, (_match, key: string) => String(valueAt(variables, key) ?? ''));
  }
  if (Array.isArray(value)) return value.map(item => materialize(item, variables));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, materialize(item, variables)]));
  return value;
}
async function responseJson(upstream: Response): Promise<unknown> {
  if (!upstream.ok) throw new Error(`Upstream returned HTTP ${upstream.status}.`);
  const reader = upstream.body?.getReader(); if (!reader) throw new Error('Upstream returned no JSON body.');
  const chunks: Uint8Array[] = []; let size = 0;
  try { while (true) { const { done, value } = await reader.read(); if (done) break; size += value.length; if (size > 5_000_000) throw new Error('Upstream response exceeds 5 MB.'); chunks.push(value); } }
  finally { await reader.cancel(); }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
export async function executeHttpApi(api: ArtifactApiConfig, raw: unknown): Promise<{ status: number; data: unknown }> {
  const payload = (raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}) as Record<string, unknown>;
  validate(api.requestSchema, payload);
  const used = new Set<string>(); const target = new URL(interpolate(api.url, payload, used));
  if (api.method === 'GET') for (const [key, value] of Object.entries(payload)) {
    if (used.has(key)) continue;
    if (!['string', 'number', 'boolean'].includes(typeof value)) throw new Error('GET inputs must be scalar values.');
    target.searchParams.set(key, String(value));
  }
  const body = Object.fromEntries(Object.entries(payload).filter(([key]) => !used.has(key)));
  const upstream = await fetch(target, { method: api.method, headers: { 'Content-Type': 'application/json', ...headers(api) }, body: api.method === 'GET' ? undefined : JSON.stringify(body), redirect: 'error', signal: AbortSignal.timeout(15_000) });
  const data = await responseJson(upstream); validate(api.responseSchema, data); return { status: upstream.status, data };
}
export async function advertisedCommands(root: string): Promise<Array<Record<string, unknown>>> {
  const apis = await definitions(root);
  return [...apis.filter(api => api.transport === 'http').map(api => ({ name: `api_${api.id.replace(/-/g, '_')}`, source: 'api', apiId: api.id, kind: commandKind(api), description: api.description || api.title || `Call ${api.method} ${api.url}`, inputSchema: api.requestSchema })), ...databaseCommands(await appConfig(root))];
}
export async function executeAdvertisedCommand(root: string, name: string, input: unknown, mutationsAllowed: boolean): Promise<unknown> {
  const command = (await advertisedCommands(root)).find(item => item.name === name);
  if (!command) throw new Error('Unknown artifact command.');
  if (command.kind === 'mutation' && !mutationsAllowed) throw new Error('This mutation command requires explicit approval.');
  if (name === 'db_query' || name === 'db_execute') return executeDatabaseCommand(root, name, input);
  const api = (await definitions(root)).find(item => item.id === command.apiId);
  if (!api) throw new Error('API command no longer exists.');
  return (await executeHttpApi(api, input)).data;
}
function assertion(test: ArtifactApiConfig['tests'][number], result: { status: number; data: unknown }): { pass: boolean; message: string } {
  const actual = test.path === '$status' ? result.status : valueAt(result.data, test.path);
  const pass = test.operator === 'exists' ? actual !== undefined : test.operator === 'equals' ? JSON.stringify(actual) === JSON.stringify(test.expected) : String(actual ?? '').includes(String(test.expected ?? ''));
  return { pass, message: `${test.path || '$'} ${test.operator}${test.operator === 'exists' ? '' : ` ${JSON.stringify(test.expected)}`}` };
}
export async function runCollection(root: string, collection: string, variables: Record<string, unknown>, mutationsAllowed: boolean): Promise<Record<string, unknown>> {
  const apis = (await definitions(root)).filter(api => api.transport === 'http' && api.collection === collection);
  if (!apis.length) throw new Error('Unknown or empty API collection.');
  const context = { ...variables }; const results: Array<Record<string, unknown>> = [];
  for (const api of apis) {
    if (commandKind(api) === 'mutation' && !mutationsAllowed) throw new Error(`Collection contains mutation ${api.id}; approve mutations to run it.`);
    for (const script of api.scripts) if (script.phase === 'before' && script.action === 'set') context[script.name] = materialize(script.value, context);
    const input = materialize(api.example, context); const result = await executeHttpApi(api, input); const tests = api.tests.map(test => assertion(test, result));
    for (const script of api.scripts) if (script.phase === 'after' && script.action === 'assert') tests.push(assertion(script, result));
    for (const item of api.extract) context[item.name] = valueAt(result.data, item.path);
    for (const script of api.scripts) if (script.phase === 'after' && script.action === 'extract') context[script.name] = valueAt(result.data, script.path);
    results.push({ id: api.id, status: result.status, data: result.data, tests });
  }
  return { collection, variables: context, passed: results.every(result => (result.tests as Array<{ pass: boolean }>).every(test => test.pass)), results };
}
export function sameOrigin(request: Pick<Request, 'headers'>, port: number): boolean {
  const host = process.env.SKILLZ_ARTIFACT_PORT ? String(request.headers.host || '') : `127.0.0.1:${port}`;
  if (!/^127\.0\.0\.1:\d+$/.test(host)) return false;
  return request.headers.host === host && (!request.headers.origin || request.headers.origin === `http://${host}`);
}
export function attachGateway(app: Express, server: Server, root: string): () => void {
  const port = () => (server.address() as { port: number } | null)?.port || 0;
  app.use((request, response, next) => { if (!sameOrigin(request, port())) { response.status(403).json({ error: 'Only this artifact origin is allowed.' }); return; } next(); });
  app.use('/files', fileRouter(root));
  app.get('/context/:id', async (request, response) => {
    const names: Record<string, string> = { 'repo-facts': 'repo_facts.md', memory: 'memory_observability.md' }; const name = names[request.params.id];
    if (!name) { response.status(404).json({ error: 'Unknown context file.' }); return; }
    try { const text = await readFile(path.join(process.env.SKILLZ_CONTEXT_ROOT || path.join(root, '.context'), name), 'utf8'); if (text.length > 5_000_000) throw new Error('Context file exceeds 5 MB.'); response.type('text/plain').send(text); }
    catch { response.status(404).json({ error: 'This context file is not shared or does not exist yet.' }); }
  });
  app.get('/_skillz/commands', async (_request, response) => { try { response.json({ commands: await advertisedCommands(root) }); } catch (error) { response.status(500).json({ error: String(error) }); } });
  app.post('/_skillz/commands/:name', async (request, response) => { try { response.json(await executeAdvertisedCommand(root, request.params.name, request.body ?? {}, request.headers['x-skillz-approve-mutation'] === 'true')); } catch (error) { response.status(400).json({ error: error instanceof Error ? error.message : String(error) }); } });
  app.post('/_skillz/collections/:id/run', async (request, response) => { try { response.json(await runCollection(root, request.params.id, request.body?.variables || {}, request.body?.approveMutations === true)); } catch (error) { response.status(400).json({ error: error instanceof Error ? error.message : String(error) }); } });
  app.all('/api/:id', async (request, response) => {
    try { const api = (await definitions(root)).find(item => item.id === request.params.id && item.transport === 'http'); if (!api) { response.status(404).json({ error: 'Unknown API configuration ID.' }); return; } if (request.method !== api.method) { response.setHeader('Allow', api.method); response.status(405).json({ error: `Use ${api.method}.` }); return; } response.json((await executeHttpApi(api, api.method === 'GET' ? request.query : request.body)).data); }
    catch (error) { response.status(502).json({ error: error instanceof Error ? error.message : 'API request failed.' }); }
  });
  const sockets = new WebSocketServer({ noServer: true, maxPayload: 1024 * 1024 }); const upstreams = new Set<WebSocket>();
  server.on('upgrade', (request, socket, head) => {
    if (!request.url?.startsWith('/ws/')) return;
    void (async () => {
      if (!sameOrigin(request, port())) throw new Error('Origin rejected.'); const id = new URL(request.url!, 'http://localhost').pathname.slice(4); const api = (await definitions(root)).find(item => item.id === id && item.transport === 'websocket'); if (!api) throw new Error('Unknown WebSocket configuration ID.');
      const outgoing = ajv.compile(api.requestSchema), incoming = ajv.compile(api.responseSchema); const upstream = new WebSocket(api.url, { headers: headers(api), followRedirects: false, handshakeTimeout: 15_000, maxPayload: 1024 * 1024 }); upstreams.add(upstream);
      sockets.handleUpgrade(request, socket, head, client => {
        const queue: string[] = []; const sendError = (message: string) => { if (client.readyState === WebSocket.OPEN) client.send(JSON.stringify({ error: message })); };
        client.on('message', raw => { try { const value = JSON.parse(raw.toString()); if (!outgoing(value)) throw new Error('Outgoing message does not match requestSchema.'); const data = JSON.stringify(value); if (upstream.readyState === WebSocket.OPEN) upstream.send(data); else if (queue.length < 32) queue.push(data); else throw new Error('Upstream is not ready.'); } catch (error) { sendError(String(error)); } });
        upstream.on('open', () => { for (const data of queue) upstream.send(data); queue.length = 0; }); upstream.on('message', raw => { try { const value = JSON.parse(raw.toString()); if (!incoming(value)) throw new Error('Incoming message does not match responseSchema.'); if (client.readyState === WebSocket.OPEN) client.send(JSON.stringify(value)); } catch (error) { sendError(String(error)); } }); upstream.on('error', () => { sendError('Upstream WebSocket connection failed.'); client.close(1011); }); upstream.on('close', () => { upstreams.delete(upstream); client.close(); }); client.on('close', () => { upstream.terminate(); upstreams.delete(upstream); }); client.on('error', () => upstream.terminate());
      });
    })().catch(() => { socket.write('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'); socket.destroy(); });
  });
  return () => { for (const socket of upstreams) socket.terminate(); for (const client of sockets.clients) client.terminate(); sockets.close(); };
}
