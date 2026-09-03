import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { createHash, randomUUID } from 'node:crypto';
import { app } from 'electron';
import { hostEnvironment } from './hostEnvironment';
import { command, git, runLogged } from './artifactProcess';
import type { ReadDirectory } from '../../shared/artifacts';

export const containerRoot = '/repo';
const builds = new Map<string, Promise<string>>();
export function harnessRoot(): string { return app?.isPackaged ? path.join(process.resourcesPath, 'python-agent') : path.resolve(app?.getAppPath() || path.resolve(__dirname, '../../..'), '..'); }
export async function dockerCommand(): Promise<string> {
  const candidates = process.platform === 'win32' ? ['docker.exe', path.join(process.env.LOCALAPPDATA || '', 'Programs/DockerDesktop/resources/bin/docker.exe'), path.join(process.env.ProgramFiles || 'C:/Program Files', 'Docker/Docker/resources/bin/docker.exe')] : ['docker', '/usr/local/bin/docker', '/opt/homebrew/bin/docker'];
  for (const candidate of candidates) {
    try { await command(candidate, ['--version'], process.cwd()); return candidate; } catch { /* Try desktop installation locations. */ }
  }
  throw new Error('Docker was not found. Install Docker Desktop, enable Linux containers, and start it before running artifacts.');
}
async function sandboxDefinition(source: string) {
  // Copy only distributed harness code. Never send the checkout, .env, credentials, or Git data to the Docker builder.
  const files: { name: string; content: Buffer }[] = [];
  async function collect(directory: string, prefix = ''): Promise<void> {
    for (const item of await fs.readdir(directory, { withFileTypes: true })) {
      const name = prefix + item.name;
      if (item.isFile() && (item.name.endsWith('.py') || (prefix.startsWith('skills/') && item.name.endsWith('.md')))) files.push({ name, content: await fs.readFile(path.join(directory, item.name)) });
      else if (item.isDirectory() && (prefix || ['discovery', 'diagnostics', 'mutations', 'skills'].includes(item.name)) && !['__pycache__', '.git', 'node_modules'].includes(item.name)) await collect(path.join(directory, item.name), name + '/');
    }
  }
  await collect(source);
  const dockerfile = `FROM node:22.20.0-bookworm
RUN mkdir -p /repo/node_modules && chmod 1777 /repo/node_modules
COPY harness /opt/skillz
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp/skillz-home
WORKDIR /repo
`;
  const hash = createHash('sha256').update(dockerfile);
  for (const file of files.sort((a, b) => a.name.localeCompare(b.name))) hash.update(file.name).update(file.content);
  const image = 'skillz-artifact:' + hash.digest('hex').slice(0, 20);
  return { image, files, dockerfile };
}
export async function sandboxImageReady(docker: string, source = harnessRoot()): Promise<boolean> {
  const { image } = await sandboxDefinition(source);
  return command(docker, ['image', 'inspect', image], source).then(() => true, () => false);
}
export async function ensureSandboxImage(log: (text: string) => void, source = harnessRoot()): Promise<{ docker: string; image: string }> {
  const docker = await dockerCommand();
  try { if (await command(docker, ['info', '--format', '{{.OSType}}'], source) !== 'linux') throw new Error('Linux containers required'); }
  catch { throw new Error('Docker is not ready. Start Docker Desktop with Linux containers, wait for its engine to be running, and retry. Artifact execution remains stopped.'); }
  const { image, files, dockerfile } = await sandboxDefinition(source);
  if (!builds.has(image)) builds.set(image, (async () => {
    try { await command(docker, ['image', 'inspect', image], source); return image; } catch { /* Build the trusted runtime. */ }
    const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'skillz-sandbox-build-'));
    try {
      for (const file of files) { const target = path.join(temp, 'harness', file.name); await fs.mkdir(path.dirname(target), { recursive: true }); await fs.writeFile(target, file.content); }
      await fs.writeFile(path.join(temp, 'Dockerfile'), dockerfile);
      log('Preparing the Docker runtime (first start may take a few minutes).\n');
      await runLogged(docker, ['build', '--tag', image, temp], source, log);
      return image;
    } finally { if (path.dirname(temp) === os.tmpdir() && path.basename(temp).startsWith('skillz-sandbox-build-')) await fs.rm(temp, { recursive: true, force: true }); }
  })().catch((error) => { builds.delete(image); throw error; }));
  await builds.get(image);
  return { docker, image };
}
export function bindMount(source: string, target: string, readOnly: boolean): string {
  if (/[\r\n,]/.test(source)) throw new Error('Docker folder paths cannot contain commas or line breaks. Choose another folder.');
  // Submounts are excluded, avoiding writable nested mounts under a read-only grant.
  return `type=bind,src=${source},dst=${target},bind-recursive=disabled${readOnly ? ',readonly' : ''}`;
}
function rootLabel(root: string): string { return createHash('sha256').update(root).digest('hex'); }
async function removeContainer(docker: string, name: string, root: string): Promise<void> {
  try { await command(docker, ['rm', '--force', name], root); }
  catch (error) {
    if (/No such container/.test(String(error))) return;
    if (/removal of container .* is already in progress/.test(String(error))) {
      await command(docker, ['wait', name], root).catch((cause) => { if (!/No such container/.test(String(cause))) throw cause; });
      return;
    }
    throw error;
  }
}
export async function stopArtifactContainers(root: string): Promise<void> {
  const docker = await dockerCommand();
  const canonical = await fs.realpath(root);
  const ids = (await command(docker, ['ps', '--all', '--quiet', '--filter', 'label=agency.aiam.skillz.artifact=true', '--filter', 'label=agency.aiam.skillz.root=' + rootLabel(canonical)], canonical)).split(/\s+/).filter(Boolean);
  for (const id of ids) {
    if (!/^[0-9a-f]{12,64}$/.test(id)) throw new Error('Docker returned an invalid artifact container ID.');
    await removeContainer(docker, id, canonical);
  }
}
export class ArtifactSandbox {
  readonly name = 'skillz-artifact-' + randomUUID();
  private docker = '';
  private cancelled = false;
  private stopping?: Promise<void>;
  constructor(readonly root: string, readonly reads: ReadDirectory[], readonly context: string) {}
  async prepare(log: (text: string) => void, source?: string): Promise<{ docker: string; args: string[] }> {
    const { docker, image } = await ensureSandboxImage(log, source);
    this.docker = docker;
    if (this.cancelled) throw new Error('Artifact start cancelled.');
    const realRoot = await fs.realpath(this.root);
    const args = ['run', '--rm', '--init', '--name', this.name, '--label', 'agency.aiam.skillz.artifact=true', '--label', 'agency.aiam.skillz.root=' + rootLabel(realRoot), '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only', '--pids-limit=256', '--memory=2g', '--cpus=2', '--tmpfs', '/tmp:rw,nosuid,size=512m', '--add-host', 'host.docker.internal:host-gateway', '--workdir', containerRoot, '--mount', bindMount(realRoot, containerRoot, false)];
    if (process.platform !== 'win32' && process.getuid && process.getgid) args.push('--user', `${process.getuid()}:${process.getgid()}`);
    // Isolate Linux dependencies from packages installed on the host OS.
    const volume = 'skillz-artifact-deps-' + createHash('sha256').update(realRoot).digest('hex').slice(0, 20);
    args.push('--mount', `type=volume,src=${volume},dst=/repo/node_modules`);
    const gitDir = await git(realRoot, 'rev-parse', '--absolute-git-dir');
    args.push('--mount', bindMount(await fs.realpath(gitDir), '/artifact-git', false), '--env', 'GIT_DIR=/artifact-git', '--env', 'GIT_WORK_TREE=/repo', '--env', 'GIT_CONFIG_COUNT=1', '--env', 'GIT_CONFIG_KEY_0=safe.directory', '--env', 'GIT_CONFIG_VALUE_0=/repo');
    const reads: ReadDirectory[] = [];
    for (const directory of this.reads) {
      const real = await fs.realpath(directory.path);
      if (real !== directory.path || !(await fs.stat(real)).isDirectory()) throw new Error(`Shared folder changed. Select it again: ${directory.label}`);
      const target = '/reads/' + directory.id;
      args.push('--mount', bindMount(real, target, directory.access !== 'write')); reads.push({ ...directory, path: target });
    }
    args.push('--mount', bindMount(this.context, '/context', true));
    args.push('--env', 'SKILLZ_READ_ROOTS=' + JSON.stringify(reads), '--env', 'SKILLZ_WRITE_ROOTS=' + JSON.stringify(reads.filter(root => root.access === 'write')), '--env', 'SKILLZ_ARTIFACT_READ_ROOTS=' + JSON.stringify(reads), '--env', 'SKILLZ_OBSERVABILITY_PATH=/repo/memory_observability.md', '--env', 'SKILLZ_CONTEXT_ROOT=/context');
    return { docker, args: [...args, image] };
  }
  spawn(docker: string, args: string[], commandArgs: string[], options: string[] = []): ChildProcessWithoutNullStreams {
    if (this.cancelled) throw new Error('Artifact start cancelled.');
    return spawn(docker, [...args.slice(0, -1), ...options, args.at(-1)!, ...commandArgs], { env: hostEnvironment(), windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  }
  async port(containerPort: number): Promise<string> {
    const address = await command(this.docker, ['port', this.name, String(containerPort)], this.root);
    if (!/^127\.0\.0\.1:\d+$/.test(address)) throw new Error('Docker did not publish an isolated loopback port.');
    return 'http://' + address;
  }
  async stop(): Promise<void> {
    this.cancelled = true;
    if (!this.docker) return;
    if (!this.stopping) this.stopping = removeContainer(this.docker, this.name, this.root).catch((error) => { this.stopping = undefined; throw error; });
    return this.stopping;
  }
}
