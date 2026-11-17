import type { Express } from 'express';

/** The capability stays server-side. Only fixed runtime routes can reach the host. */
export function attachRuntime(app: Express): void {
  app.use('/runtime', async (request, response) => {
    response.setHeader('Cache-Control', 'no-store');
    const url = process.env.SKILLZ_RUNTIME_URL;
    const token = process.env.SKILLZ_RUNTIME_TOKEN;
    if (!url || !token) {
      response.status(request.method === 'GET' && request.path === '/v1/runtime' ? 200 : 503).json({ available: false, runs: [], error: 'Enable Allow agent runtime in File access, select a provider/model when installing, then restart the preview.' }); return;
    }
    const route = request.path;
    const allowed = request.method === 'GET' ? route === '/v1/runtime' || /^\/v1\/runs\/[a-f0-9-]{36}$/.test(route) : request.method === 'POST' && (['/v1/runs', '/v1/stop', '/v1/configure'].includes(route) || /^\/v1\/runs\/[a-f0-9-]{36}\/continue$/.test(route));
    if (!allowed) { response.status(404).json({ error: 'Unknown runtime route.' }); return; }
    // Reject cross-origin form/fetch requests while retaining same-origin preview access.
    if ((request.headers.origin && request.headers.origin !== `${request.protocol}://${request.get('host')}`) || request.headers['sec-fetch-site'] === 'cross-site') { response.status(403).json({ error: 'Cross-origin runtime requests are blocked.' }); return; }
    if (request.method === 'POST' && !request.is('application/json')) { response.status(415).json({ error: 'Use application/json.' }); return; }
    try {
      const target = new URL(route, url);
      if (request.method === 'GET' && route !== '/v1/runtime') target.searchParams.set('after', typeof request.query.after === 'string' ? request.query.after : '0');
      const upstream = await fetch(target, { method: request.method, headers: { 'content-type': 'application/json', 'x-skillz-runtime-token': token }, body: request.method === 'POST' ? JSON.stringify(request.body) : undefined, signal: AbortSignal.timeout(30000), redirect: 'error' });
      response.status(upstream.status).json(await upstream.json());
    } catch (error) { response.status(502).json({ error: `Runtime connection failed: ${String(error)}` }); }
  });
}
