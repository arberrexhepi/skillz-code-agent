import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import type { Express, Request, Response } from 'express';

const endpoint = '/_skillz/mcp';

export async function attachArtifactMcp(app: Express, root: string): Promise<() => void> {
  if (process.env.SKILLZ_ARTIFACT_MCP !== '1') {
    app.all([endpoint, endpoint + '/request'], (_request, response) => {
      response.status(404).json({ ok: false, error: 'Enable Artifact MCP in App capabilities and start the preview.' });
    });
    return () => {};
  }

  const child = spawn('/opt/skillz-mcp/bin/python', ['-u', '/opt/skillz/artifact_app_mcp.py', root],
    { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  const lines = createInterface({ input: child.stdout });
  let diagnostics = '';
  child.stderr.setEncoding('utf8').on('data', (buffer: string) => { diagnostics = (diagnostics + buffer).slice(-2500); });
  const port = await new Promise<number>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Artifact MCP did not finish starting.')), 20_000);
    const fail = (cause: unknown) => { clearTimeout(timeout); reject(cause); };
    child.once('error', fail);
    child.once('close', () => fail(new Error(diagnostics.trim() || 'Artifact MCP exited before it was ready.')));
    lines.on('line', (line) => {
      const match = /^SKILLZ_APP_MCP_READY \{"port": (\d+)\}$/.exec(line);
      if (!match) return;
      clearTimeout(timeout);
      resolve(Number(match[1]));
    });
  }).catch(error => {
    child.kill('SIGTERM');
    throw error;
  });
  const loopback = `http://127.0.0.1:${port}`;
  process.env.SKILLZ_APP_MCP_LOOPBACK = loopback;

  async function relay(request: Request, response: Response, internal: string, body?: unknown) {
    try {
      const headers = new Headers();
      for (const key of ['accept', 'mcp-protocol-version']) {
        const value = request.get(key);
        if (value) headers.set(key, value);
      }
      if (body !== undefined) headers.set('content-type', 'application/json');
      const result = await fetch(loopback + internal, {
        method: request.method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(270_000), redirect: 'error',
      });
      response.status(result.status).set('cache-control', 'no-store');
      for (const key of ['allow', 'content-type', 'mcp-protocol-version']) {
        const value = result.headers.get(key);
        if (value) response.set(key, value);
      }
      response.send(Buffer.from(await result.arrayBuffer()));
    } catch {
      response.status(502).json({ ok: false, error: 'Artifact MCP did not respond. Check the preview and server configuration.' });
    }
  }

  // The app gateway's same-origin middleware runs before either route.
  app.post(endpoint + '/request', (request, response) => {
    const { approveMutations, ...action } = request.body || {};
    return relay(request, response, '/request', { action, approveMutations: approveMutations === true });
  });
  app.all(endpoint, (request, response) => relay(request, response, '/mcp', request.method === 'POST' ? request.body : undefined));
  return () => {
    delete process.env.SKILLZ_APP_MCP_LOOPBACK;
    lines.close();
    child.kill('SIGTERM');
  };
}
