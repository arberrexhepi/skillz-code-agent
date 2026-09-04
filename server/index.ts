import express from 'express';
import { createServer } from 'node:http';
import path from 'node:path';
import { attachGateway } from './gateway';

const root = process.cwd();
const app = express();
const server = createServer(app);
app.use(express.json({ limit: '1mb' }));
const closeGateway = attachGateway(app, server, root);
let closeVite: (() => Promise<void>) | undefined;
if (process.argv.includes('--production')) {
  app.use(express.static(path.join(root, 'dist')));
  app.use((request, response) => { if (request.method === 'GET') response.sendFile(path.join(root, 'dist/index.html')); else response.sendStatus(404); });
} else {
  const { createServer: createViteServer } = await import('vite');
  const vite = await createViteServer({ root, server: { middlewareMode: true, hmr: { server } }, appType: 'spa' });
  app.use(vite.middlewares);
  closeVite = () => vite.close();
}
server.listen(Number(process.env.SKILLZ_ARTIFACT_PORT || 0), process.env.SKILLZ_ARTIFACT_HOST || '127.0.0.1', () => {
  const { port } = server.address() as { port: number };
  console.log('SKILLZ_ARTIFACT_READY ' + JSON.stringify({ url: `http://127.0.0.1:${port}` }));
});
async function stop() { closeGateway(); await closeVite?.(); server.closeAllConnections(); server.close(() => process.exit(0)); }
process.on('SIGTERM', () => void stop());
process.on('SIGINT', () => void stop());
