import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import type { ArtifactSetupSelection } from '../../shared/artifacts';

export interface ArtifactModelBrokerConnection { url: string; token: string }
function tokenMatches(value: string | undefined, expected: string): boolean { if (!value) return false; const left = Buffer.from(value), right = Buffer.from(expected); return left.length === right.length && timingSafeEqual(left, right); }
async function json(request: IncomingMessage): Promise<Record<string, unknown>> { const chunks: Buffer[] = []; let size = 0; for await (const raw of request) { const chunk = Buffer.isBuffer(raw) ? raw : Buffer.from(raw); size += chunk.length; if (size > 2 * 1024 * 1024) throw new Error('Model request exceeds 2 MB.'); chunks.push(chunk); } const value = JSON.parse(Buffer.concat(chunks).toString('utf8')); if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Model request must be an object.'); return value; }
function send(response: ServerResponse, status: number, value: object): void { response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' }); response.end(JSON.stringify(value)); }

export class ArtifactModelBroker {
  private server?: Server; private connection?: ArtifactModelBrokerConnection; private active = 0;
  constructor(private readonly selection: ArtifactSetupSelection, private readonly complete: (selection: ArtifactSetupSelection, payload: Record<string, unknown>) => Promise<Record<string, unknown>>) {}
  async start(): Promise<ArtifactModelBrokerConnection> {
    if (this.connection) return this.connection;
    const token = randomBytes(32).toString('base64url'); const server = createServer((request, response) => void this.handle(request, response, token)); this.server = server;
    const port = await new Promise<number>((resolve, reject) => { server.once('error', reject); server.listen(0, '0.0.0.0', () => { server.removeListener('error', reject); const address = server.address(); if (!address || typeof address === 'string') reject(new Error('Artifact model broker did not bind a TCP port.')); else resolve(address.port); }); });
    return this.connection = { url: `http://host.docker.internal:${port}/v1/complete`, token };
  }
  private async handle(request: IncomingMessage, response: ServerResponse, token: string): Promise<void> {
    if (request.method !== 'POST' || request.url !== '/v1/complete') { send(response, 404, { error: 'Unknown model broker route.' }); return; }
    if (!tokenMatches(request.headers['x-skillz-model-token'] as string | undefined, token)) { send(response, 401, { error: 'Invalid model broker capability.' }); return; }
    if (this.active >= 4) { send(response, 429, { error: 'Too many simultaneous artifact chatbot requests.' }); return; }
    this.active++;
    try { send(response, 200, await this.complete(this.selection, await json(request))); }
    catch (error) { send(response, 502, { error: error instanceof Error ? error.message : String(error) }); }
    finally { this.active--; }
  }
  async close(): Promise<void> { const server = this.server; this.server = undefined; this.connection = undefined; if (!server) return; server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); }
}
