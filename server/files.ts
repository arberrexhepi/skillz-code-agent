import { createHash, randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { Router } from 'express';
import { chmod, lstat, open, opendir, realpath, rename, rm, stat } from 'node:fs/promises';
import path from 'node:path';

interface Root { id: string; label: string; path: string; access?: 'read' | 'write' }
const maxBytes = 5 * 1024 * 1024;
export function fileRouter(repository: string): Router {
  const granted: Root[] = JSON.parse(process.env.SKILLZ_ARTIFACT_READ_ROOTS || '[]');
  const roots: Root[] = [{ id: 'repo', label: 'Artifact repository', path: repository, access: 'read' }, ...granted];
  if (!Array.isArray(granted) || roots.some(root => !/^[a-z0-9][a-z0-9-]{0,63}$/.test(root.id) || !path.isAbsolute(root.path) || !['read', 'write'].includes(root.access || 'read')) || new Set(roots.map(root => root.id)).size !== roots.length) throw new Error('Invalid runtime folder grants.');
  const router = Router();
  router.get('/roots', (_request, response) => response.json(roots.map(({ id, label, access = 'read' }) => ({ id, label, readOnly: access !== 'write', access }))));
  async function target(id: string, relative: unknown): Promise<{ root: Root; path: string }> {
    const root = roots.find(item => item.id === id);
    if (!root) throw new Error('This folder is not shared.');
    if (typeof relative !== 'string' || relative.length > 4096 || relative.includes('\\') || relative.includes(':') || relative.includes('\0') || path.isAbsolute(relative) || relative.split('/').includes('..')) throw new Error('Use a relative path inside the shared folder.');
    const base = await realpath(root.path);
    const candidate = path.resolve(base, relative || '.');
    const lexical = path.relative(base, candidate);
    if (lexical === '..' || lexical.startsWith('..' + path.sep) || path.isAbsolute(lexical)) throw new Error('Path escapes the shared folder.');
    try {
      const resolved = await realpath(candidate);
      const rel = path.relative(base, resolved);
      if (rel === '..' || rel.startsWith('..' + path.sep) || path.isAbsolute(rel)) throw new Error('Path escapes the shared folder.');
      return { root, path: resolved };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      const parent = await realpath(path.dirname(candidate));
      const rel = path.relative(base, parent);
      if (rel === '..' || rel.startsWith('..' + path.sep) || path.isAbsolute(rel)) throw new Error('Path escapes the shared folder.');
      return { root, path: path.join(parent, path.basename(candidate)) };
    }
  }
  router.get('/:id/list', async (request, response) => {
    try {
      const directory = (await target(String(request.params.id), request.query.path ?? '')).path;
      if (!(await lstat(directory)).isDirectory()) throw new Error('Expected a directory.');
      const entries = [];
      for await (const item of await opendir(directory)) { entries.push({ name: item.name, kind: item.isSymbolicLink() ? 'link' : item.isDirectory() ? 'directory' : item.isFile() ? 'file' : 'other' }); if (entries.length > 500) break; }
      response.json({ entries: entries.slice(0, 500), truncated: entries.length > 500 });
    } catch (error) { response.status(403).json({ error: error instanceof Error ? error.message : 'Read denied.' }); }
  });
  router.get('/:id/read', async (request, response) => {
    try {
      const file = await open((await target(String(request.params.id), request.query.path)).path, constants.O_RDONLY | constants.O_NONBLOCK);
      try {
        const info = await file.stat();
        if (!info.isFile() || info.size > maxBytes) throw new Error('Choose a regular file smaller than 5 MB.');
        const buffer = Buffer.alloc(Math.min(info.size + 1, maxBytes + 1));
        const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
        if (bytesRead > maxBytes) throw new Error('File exceeds 5 MB.');
        response.type('application/octet-stream').send(buffer.subarray(0, bytesRead));
      } finally { await file.close(); }
    } catch (error) { response.status(403).json({ error: error instanceof Error ? error.message : 'Read denied.' }); }
  });
  router.put('/:id/write', async (request, response) => {
    let temporary = '';
    try {
      const resolved = await target(String(request.params.id), request.body?.path);
      if (resolved.root.access !== 'write') { response.status(403).json({ error: 'This folder is read only. Enable Allow changes in File access.' }); return; }
      const content = request.body?.content, expected = request.body?.expectedSha256;
      if (typeof content !== 'string' || Buffer.byteLength(content) > maxBytes || typeof expected !== 'string' || !/^[a-f0-9]{64}$/.test(expected)) throw new Error('Provide text smaller than 5 MB and the SHA-256 of the loaded file.');
      let mode = 0o600, digest = createHash('sha256').update('').digest('hex');
      try {
        const info = await stat(resolved.path);
        if (!info.isFile()) throw new Error('Choose a regular file.');
        mode = info.mode;
        const current = await open(resolved.path, constants.O_RDONLY | constants.O_NONBLOCK);
        try { digest = createHash('sha256').update(await current.readFile()).digest('hex'); } finally { await current.close(); }
      } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
      if (digest !== expected) { response.status(409).json({ error: 'The file changed after it was loaded. Refresh before saving.' }); return; }
      temporary = path.join(path.dirname(resolved.path), `.${path.basename(resolved.path)}.skillz-${randomUUID()}.tmp`);
      const output = await open(temporary, 'wx', mode);
      try { await output.writeFile(content, 'utf8'); await output.sync(); } finally { await output.close(); }
      await chmod(temporary, mode);
      await rename(temporary, resolved.path); temporary = '';
      response.json({ sha256: createHash('sha256').update(content).digest('hex') });
    } catch (error) { response.status(403).json({ error: error instanceof Error ? error.message : 'Write denied.' }); }
    finally { if (temporary) await rm(temporary, { force: true }); }
  });
  router.use((_request, response) => { response.status(404).json({ error: 'Unknown file operation.' }); });
  return router;
}
