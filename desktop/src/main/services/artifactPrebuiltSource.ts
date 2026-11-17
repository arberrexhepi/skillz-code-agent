import { execFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { hostEnvironment } from './hostEnvironment';

export interface SourceFile { data: string; executable: boolean }
export type SourceSnapshot = Record<string, SourceFile>;
const maxFile = 2 * 1024 * 1024;
const maxTotal = 16 * 1024 * 1024;
const excluded = new Set(['.git', 'node_modules', 'dist', 'build', 'out', 'coverage', '.vite', '.ds_store', '__pycache__', '.artifact-data', '.context', '.artifact-contexts', 'artifact.json', '.artifact-local.json']);
const appDependencies = ['sequelize', 'sqlite3', 'pg', 'mysql2', 'mariadb', 'tedious'];

export function managedPrebuiltPath(relative: string): boolean {
  const parts = relative.split('/');
  return parts.every(part => part && part !== '.' && part !== '..' && !['__proto__', 'constructor', 'prototype'].includes(part) && !/[\\:\0]/.test(part) && !excluded.has(part.toLowerCase())
    && !/^\.env(?:$|\.)/i.test(part) && !/\.(?:tsbuildinfo|log)$/i.test(part))
    && !['.artifact/app.json', '.artifact/apis.json'].includes(relative.toLowerCase());
}
export function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]));
  return value;
}
function sourceFile(relative: string, bytes: Buffer, executable: boolean, upstream: boolean): SourceFile {
  const text = bytes.toString('utf8');
  if (!bytes.includes(0) && Buffer.from(text).equals(bytes)) bytes = Buffer.from(text.replace(/\r\n/g, '\n'));
  if (relative === 'package.json' || relative === 'package-lock.json') {
    try {
      const value = JSON.parse(bytes.toString('utf8'));
      // Database drivers belong to the installed app's capability selection.
      if (upstream && relative === 'package.json' && value.dependencies) for (const name of appDependencies) delete value.dependencies[name];
      bytes = Buffer.from(JSON.stringify(canonical(value), null, 2) + '\n');
    } catch { /* Invalid manifests stay visible as source files in the review. */ }
  }
  return { data: bytes.toString('base64'), executable };
}
export function sourceHash(snapshot: SourceSnapshot): string {
  return createHash('sha256').update(JSON.stringify(Object.entries(snapshot).sort(([a], [b]) => a.localeCompare(b)))).digest('hex');
}
export function sameSource(left?: SourceFile, right?: SourceFile): boolean {
  return left?.data === right?.data && (process.platform === 'win32' || left?.executable === right?.executable);
}
export async function checkedSourcePath(root: string, relative: string): Promise<string> {
  if (!managedPrebuiltPath(relative)) throw new Error(`Invalid prebuilt source path: ${relative}`);
  let current = root;
  const parts = relative.split('/');
  for (let index = 0; index < parts.length; index++) {
    current = path.join(current, parts[index]);
    try {
      const stat = await fs.lstat(current);
      if (stat.isSymbolicLink() || (index < parts.length - 1 ? !stat.isDirectory() : !stat.isFile())) throw new Error(`Prebuilt source path is not a regular file: ${relative}`);
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  }
  return current;
}
export async function readInstalledSource(root: string, paths: string[], normalize = true): Promise<SourceSnapshot> {
  const result: SourceSnapshot = {};
  let total = 0;
  for (const relative of paths.sort()) {
    const file = await checkedSourcePath(root, relative);
    try {
      const stat = await fs.lstat(file);
      total += stat.size;
      if (stat.size > maxFile || total > maxTotal) throw new Error('Prebuilt source exceeds the update size limit.');
      const bytes = await fs.readFile(file);
      result[relative] = normalize ? sourceFile(relative, bytes, Boolean(stat.mode & 0o111), false) : { data: bytes.toString('base64'), executable: Boolean(stat.mode & 0o111) };
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  }
  return result;
}
export async function readBundledSource(root: string): Promise<SourceSnapshot> {
  const rootStat = await fs.lstat(root);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) throw new Error('Bundled prebuilt source is unavailable.');
  const files: string[] = [];
  async function walk(directory: string, prefix = ''): Promise<void> {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      const relative = prefix + entry.name;
      if (!managedPrebuiltPath(relative)) continue;
      if (entry.isSymbolicLink()) throw new Error(`Bundled source contains a symbolic link: ${relative}`);
      if (entry.isDirectory()) await walk(path.join(directory, entry.name), relative + '/');
      else if (entry.isFile()) files.push(relative);
      else throw new Error(`Bundled source is not a regular file: ${relative}`);
      if (files.length > 1000) throw new Error('Prebuilt source contains too many files.');
    }
  }
  await walk(root);
  if (!files.includes('package.json')) throw new Error('The bundled prebuilt is incomplete: package.json is missing.');
  const snapshot = await readInstalledSource(root, files);
  for (const [relative, file] of Object.entries(snapshot)) snapshot[relative] = sourceFile(relative, Buffer.from(file.data, 'base64'), file.executable, true);
  return snapshot;
}
function rawGit(root: string, args: string[], limit = maxFile): Promise<Buffer> {
  return new Promise((resolve, reject) => execFile('git', args, { cwd: root, env: hostEnvironment(), windowsHide: true, timeout: 30000, maxBuffer: limit, encoding: 'buffer' }, (error, stdout) => error ? reject(error) : resolve(stdout)));
}
export async function originalPrebuiltSource(root: string): Promise<SourceSnapshot> {
  const first = (await rawGit(root, ['rev-list', '--max-parents=0', 'HEAD'])).toString().trim().split('\n')[0];
  if (!/^[a-f0-9]{40,64}$/.test(first) || (await rawGit(root, ['show', '-s', '--format=%s', first])).toString().trim() !== 'Install prebuilt artifact') throw new Error('This prebuilt has no original install baseline. Reinstall an editable copy to enable updates.');
  const entries = (await rawGit(root, ['ls-tree', '-rz', first])).toString().split('\0').filter(Boolean);
  const result: SourceSnapshot = {};
  let total = 0;
  for (const entry of entries) {
    const match = /^(\d+) blob ([a-f0-9]+)\t(.*)$/s.exec(entry);
    if (!match || !managedPrebuiltPath(match[3])) continue;
    if (!['100644', '100755'].includes(match[1])) throw new Error('The original prebuilt baseline contains a symbolic link.');
    const bytes = await rawGit(root, ['cat-file', 'blob', match[2]]);
    total += bytes.length;
    if (total > maxTotal || Object.keys(result).length >= 1000) throw new Error('The original prebuilt baseline exceeds the update size limit.');
    result[match[3]] = sourceFile(match[3], bytes, match[1] === '100755', true);
  }
  return result;
}
export async function writeSourceSnapshot(file: string, value: unknown): Promise<void> {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const temp = file + '.' + randomUUID() + '.tmp';
  try { await fs.writeFile(temp, JSON.stringify(value), { mode: 0o600, flag: 'wx' }); await fs.rename(temp, file); }
  finally { await fs.rm(temp, { force: true }); }
}
export async function readSourceSnapshot(file: string): Promise<{ version: 1; root: string; prebuiltId: string; files: SourceSnapshot }> {
  const stat = await fs.lstat(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > maxTotal * 2) throw new Error('Invalid prebuilt source baseline.');
  const value = JSON.parse(await fs.readFile(file, 'utf8'));
  if (value.version !== 1 || typeof value.root !== 'string' || typeof value.prebuiltId !== 'string' || !value.files || typeof value.files !== 'object' || Array.isArray(value.files)) throw new Error('Invalid prebuilt source baseline.');
  let total = 0;
  for (const [relative, file] of Object.entries(value.files) as [string, SourceFile][]) {
    if (!managedPrebuiltPath(relative) || !file || typeof file.data !== 'string' || typeof file.executable !== 'boolean' || file.data.length > maxFile * 2) throw new Error('Invalid prebuilt source baseline.');
    total += Buffer.from(file.data, 'base64').length;
  }
  if (total > maxTotal || Object.keys(value.files).length > 1000) throw new Error('Invalid prebuilt source baseline.');
  return value;
}
export function prebuiltBaselineFile(home: string, root: string): string {
  return path.join(home, 'prebuilt-updates', createHash('sha256').update(root).digest('hex'), 'baseline.json');
}
export async function replaceSourceFile(root: string, relative: string, file?: SourceFile): Promise<void> {
  const target = await checkedSourcePath(root, relative);
  if (!file) { await fs.rm(target, { force: true }); return; }
  await fs.mkdir(path.dirname(target), { recursive: true });
  const temp = target + '.' + randomUUID() + '.tmp';
  try { await fs.writeFile(temp, Buffer.from(file.data, 'base64'), { flag: 'wx', mode: file.executable ? 0o755 : 0o644 }); await fs.rename(temp, target); }
  finally { await fs.rm(temp, { force: true }); }
}
export function preserveAppDependencies(result: SourceFile, installed?: SourceFile): SourceFile {
  if (!installed) return result;
  try {
    const next = JSON.parse(Buffer.from(result.data, 'base64').toString('utf8'));
    const local = JSON.parse(Buffer.from(installed.data, 'base64').toString('utf8'));
    next.dependencies ||= {};
    for (const name of appDependencies) {
      delete next.dependencies[name];
      if (local.dependencies?.[name]) next.dependencies[name] = local.dependencies[name];
    }
    return { ...result, data: Buffer.from(JSON.stringify(canonical(next), null, 2) + '\n').toString('base64') };
  } catch { throw new Error('Cannot update an invalid package.json. Repair the manifest before updating.'); }
}
export async function mergeSourceFile(base: SourceFile, local: SourceFile, incoming: SourceFile): Promise<SourceFile | undefined> {
  const values = [local, base, incoming].map(file => Buffer.from(file.data, 'base64'));
  if (values.some(bytes => bytes.includes(0) || !Buffer.from(bytes.toString('utf8')).equals(bytes))) return undefined;
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'skillz-prebuilt-merge-'));
  try {
    const files = values.map((_, index) => path.join(directory, String(index)));
    await Promise.all(files.map((file, index) => fs.writeFile(file, values[index], { mode: 0o600 })));
    return await new Promise((resolve, reject) => execFile('git', ['merge-file', '--stdout', ...files], { env: hostEnvironment(), windowsHide: true, timeout: 30000, maxBuffer: maxFile, encoding: 'buffer' }, (error, stdout) => {
      if (error) { if (typeof error.code === 'number' && error.code > 0 && error.code <= 127) resolve(undefined); else reject(error); }
      else resolve({ data: stdout.toString('base64'), executable: incoming.executable === base.executable ? local.executable : incoming.executable });
    }));
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
}
