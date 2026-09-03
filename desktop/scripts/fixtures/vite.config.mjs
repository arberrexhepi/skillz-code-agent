import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import {createServer} from 'node:http';
import {readFileSync} from 'node:fs';

export default defineConfig({
  plugins: [react(), {
    name: 'artifact-preview-fixture',
    configureServer(server) {
      const page = readFileSync(new URL('./artifact-preview.html', import.meta.url));
      const preview = createServer((_request, response) => {
        response.setHeader('Content-Type', 'text/html; charset=utf-8'); response.end(page);
      });
      const ready = new Promise(resolve => preview.listen(0, '127.0.0.1', resolve));
      server.middlewares.use('/__fixture-artifact-url', async (_request, response) => {
        await ready;
        response.setHeader('Content-Type', 'application/json');
        response.end(JSON.stringify({url: `http://127.0.0.1:${preview.address().port}`}));
      });
      server.httpServer?.once('close', () => { preview.closeAllConnections(); preview.close(); });
    },
  }],
  server: { host: '127.0.0.1', port: 5181, strictPort: true },
});
