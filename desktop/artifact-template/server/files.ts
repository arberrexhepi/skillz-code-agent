import { constants } from 'node:fs';
import { Router } from 'express';
import { lstat, open, opendir, realpath } from 'node:fs/promises';
import path from 'node:path';

interface Root { id: string; label: string; path: string }
const maxBytes = 5 * 1024 * 1024;
export function fileRouter(repository: string): Router {
  const router = Router();
  const granted: Root[] = JSON.parse(process.env.SKILLZ_ARTIFACT_READ_ROOTS || '[]');
  const roots: Root[] = [{ id: 'repo', label: 'Artifact repository', path: repository }, ...granted];
  if (!Array.isArray(granted) || roots.some((root) => !/^[a-z0-9][a-z0-9-]{0,63}$/.test(root.id) || !path.isAbsolute(root.path)) || new Set(roots.map((root) => root.id)).size !== roots.length) throw new Error('Invalid runtime folder grants.');
  router.use((request, response, next) => { if (request.method !== 'GET') { response.setHeader('Allow', 'GET'); response.status(405).json({ error: 'File access is read only.' }); return; } next(); });
  router.get('/roots', (_request, response) => { response.json(roots.map(({ id, label }) => ({ id, label, readOnly: true }))); });
  async function target(id: string, relative: unknown): Promise<string> {
    const root = roots.find((item) => item.id === id);
    if (!root) throw new Error('This folder is not shared.');
    if (typeof relative !== 'string' || relative.length > 4096 || relative.includes('\\') || relative.includes(':') || relative.includes('\0') || path.isAbsolute(relative) || relative.split('/').includes('..')) throw new Error('Use a relative path inside the shared folder.');
    const base = await realpath(root.path);
    const resolved = await realpath(path.join(base, relative));
    const rel = path.relative(base, resolved);
    if (rel === '..' || rel.startsWith('..' + path.sep) || path.isAbsolute(rel)) throw new Error('Path escapes the shared folder.');
    return resolved;
  }
  router.get('/:id/list', async (request, response) => {
    try {
      const directory = await target(String(request.params.id), request.query.path ?? '');
      if (!(await lstat(directory)).isDirectory()) throw new Error('Expected a directory.');
      const entries = [];
      for await (const item of await opendir(directory)) { entries.push({ name: item.name, kind: item.isSymbolicLink() ? 'link' : item.isDirectory() ? 'directory' : item.isFile() ? 'file' : 'other' }); if (entries.length > 500) break; }
      response.json({ entries: entries.slice(0, 500), truncated: entries.length > 500 });
    } catch (error) { response.status(403).json({ error: error instanceof Error ? error.message : 'Read denied.' }); }
  });
  router.get('/:id/read', async (request, response) => {
    try {
      const file = await open(await target(String(request.params.id), request.query.path), constants.O_RDONLY | constants.O_NONBLOCK);
      try {
        const stat = await file.stat();
        if (!stat.isFile() || stat.size > maxBytes) throw new Error('Choose a regular file smaller than 5 MB.');
        const buffer = Buffer.alloc(Math.min(stat.size + 1, maxBytes + 1));
        const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
        if (bytesRead > maxBytes) throw new Error('File exceeds 5 MB.');
        response.type('application/octet-stream').send(buffer.subarray(0, bytesRead));
      } finally { await file.close(); }
    } catch (error) { response.status(403).json({ error: error instanceof Error ? error.message : 'Read denied.' }); }
  });
  router.use((_request, response) => { response.status(404).json({ error: 'Unknown file operation.' }); });
  return router;
}
