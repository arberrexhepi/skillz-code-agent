import { type ChildProcess } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import type { ArtifactEvent, ArtifactRecord, ArtifactRuntime, CreateArtifact, PreviewFrame } from '../../shared/artifacts';
import type { AgentStartOptions } from '../../shared/contracts';
import { ArtifactCapabilitiesService } from './artifactCapabilities';
import { ArtifactLibraryService } from './artifactLibrary';
import { ArtifactPreviewService } from './artifactPreview';
import { AgentService } from './agent';
import { WorkspaceService } from './workspace';
import type { RuntimeSettingsService } from './runtimeSettings';
import { terminate } from './artifactProcess';
import { ArtifactSandbox, stopArtifactContainers } from './artifactSandbox';
import { ArtifactAgentExecution } from './artifactAgent';
import { createServer } from 'node:net';

interface Running { state: ArtifactRuntime; child?: ChildProcess; sandbox?: ArtifactSandbox; start?: Promise<ArtifactRuntime>; cancelled: boolean }
export class ArtifactsService {
  readonly preview = new ArtifactPreviewService();
  readonly capabilities: ArtifactCapabilitiesService;
  private runtimes = new Map<string, Running>();
  private agentCreations = new Map<string, Promise<AgentService>>();
  private agents = new Map<string, { workspace: WorkspaceService; agent: AgentService }>();
  private agentStarts = new Map<string, Promise<unknown>>();
  private contextTimer: NodeJS.Timeout;
  private syncing = false;
  private changingAccess = new Set<string>();
  constructor(readonly library: ArtifactLibraryService, private readonly settings: RuntimeSettingsService, private readonly emit: (event: ArtifactEvent) => void, private readonly activeWorkspace: () => string = () => '') {
    this.capabilities = new ArtifactCapabilitiesService(settings.artifactSetupDirectory(), this.preview, (progress) => this.emit({ type: 'setup', progress }));
    this.contextTimer = setInterval(() => { if (!this.syncing) void this.syncContexts(); }, 2000);
    this.contextTimer.unref();
  }
  private async syncContexts(): Promise<void> {
    this.syncing = true;
    try { for (const artifact of (await this.library.library()).artifacts) await this.library.syncContext(artifact); }
    catch { /* A missing source stays visible as unavailable context in the artifact. */ }
    finally { this.syncing = false; }
  }
  async configure(root: string) { await this.disposeSessions(); return this.library.configure(root); }
  create(options: CreateArtifact): Promise<ArtifactRecord> { return this.library.create(options); }
  async start(id: string): Promise<ArtifactRuntime> {
    if (this.changingAccess.has(id)) throw new Error('File permissions are changing. Retry when saving finishes.');
    const prior = this.runtimes.get(id);
    if (prior?.start) return prior.start;
    if (prior?.state.status === 'running') return prior.state;
    const runtime: Running = { state: { id, status: 'starting', logs: '' }, cancelled: false };
    this.runtimes.set(id, runtime);
    runtime.start = this.launch(id, runtime).catch((error) => { if (!runtime.cancelled) this.update(runtime, { status: 'error', error: String(error) }); throw error; }).finally(() => { runtime.start = undefined; });
    return runtime.start;
  }
  private update(runtime: Running, change: Partial<ArtifactRuntime>): void { Object.assign(runtime.state, change); this.emit({ type: 'runtime', runtime: { ...runtime.state } }); }
  private async launch(id: string, runtime: Running): Promise<ArtifactRuntime> {
    const artifact = await this.library.find(id);
    await this.library.syncContext(artifact);
    const sandbox = await this.sandbox(id); runtime.sandbox = sandbox;
    const log = (text: string) => this.update(runtime, { logs: (runtime.state.logs + text).slice(-30000) });
    const check = () => { if (runtime.cancelled) throw new Error('Artifact start cancelled.'); };
    check();
    this.update(runtime, { status: 'installing' });
    const prepared = await sandbox.prepare(log);
    check();
    const port = await new Promise<number>((resolve, reject) => { const server = createServer(); server.once('error', reject); server.listen(0, '127.0.0.1', () => { const port = (server.address() as { port: number }).port; server.close(() => resolve(port)); }); });
    const variables = new Set((await this.library.apis(id)).apis.flatMap((api) => Object.values(api.headerEnv)));
    const secretEnv = [...variables].filter((name) => process.env[name] !== undefined).flatMap((name) => ['--env', name]);
    check(); this.update(runtime, { status: 'starting', error: undefined });
    return new Promise<ArtifactRuntime>((resolve, reject) => {
      const child = sandbox.spawn(prepared.docker, prepared.args, ['sh', '-c', 'mkdir -p "$HOME" && npm install --no-audit --no-fund --fetch-retries=0 --fetch-timeout=30000 && exec node --import tsx server/index.ts'], [...secretEnv, '--publish', `127.0.0.1::${port}`, '--env', `SKILLZ_ARTIFACT_PORT=${port}`, '--env', 'SKILLZ_ARTIFACT_HOST=0.0.0.0']);
      runtime.child = child;
      let buffer = '', ready = false;
      const timeout = setTimeout(() => { void sandbox.stop(); reject(new Error('Artifact server did not become ready within three minutes. See logs.')); }, 180000);
      child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
      child.stdout.on('data', (text: string) => {
        log(text); buffer += text;
        let newline: number;
        while ((newline = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
          if (!line.startsWith('SKILLZ_ARTIFACT_READY ') || ready) continue;
          try {
            const { url } = JSON.parse(line.slice('SKILLZ_ARTIFACT_READY '.length)) as { url: string };
            if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(url)) throw new Error('Invalid artifact server address.');
            check(); ready = true; clearTimeout(timeout); void sandbox.port(port).then((url) => { check(); this.update(runtime, { status: 'running', url }); resolve({ ...runtime.state }); }).catch((error) => { void sandbox.stop(); reject(error); });
          } catch (error) { clearTimeout(timeout); void sandbox.stop(); reject(error); }
        }
        if (buffer.length > 64000) buffer = buffer.slice(-64000);
      });
      child.stderr.on('data', log);
      child.on('error', (error) => { clearTimeout(timeout); reject(error); });
      child.on('exit', (code) => {
        clearTimeout(timeout);
        if (runtime.cancelled) { if (!ready) reject(new Error('Artifact start cancelled.')); return; }
        const error = /EAI_AGAIN|ENOTFOUND/.test(runtime.state.logs) ? 'Docker could not resolve the package registry. Check Docker Desktop network/DNS settings and retry.' : `Artifact server exited with code ${code}. See Server logs.`;
        this.update(runtime, { status: 'error', url: undefined, error });
        if (!ready) reject(new Error(error));
      });
    });
  }
  async stop(id: string): Promise<void> {
    const runtime = this.runtimes.get(id);
    if (runtime) { runtime.cancelled = true; await runtime.sandbox?.stop(); if (runtime.child) await terminate(runtime.child); this.update(runtime, { status: 'stopped', url: undefined }); this.runtimes.delete(id); }
    await this.preview.close(id);
  }
  previewOrigins(): string[] {
    return [...this.runtimes.values()].flatMap(({ state, cancelled }) =>
      !cancelled && state.status === 'running' && state.url ? [new URL(state.url).origin] : []);
  }
  async frame(id: string): Promise<PreviewFrame> {
    const runtime = this.runtimes.get(id)?.state;
    if (runtime?.status !== 'running' || !runtime.url) throw new Error('Start the artifact before opening its preview.');
    return this.preview.frame(id, runtime.url);
  }
  async agent(id: string): Promise<AgentService> {
    const existing = this.agents.get(id); if (existing) return existing.agent;
    const inFlight = this.agentCreations.get(id); if (inFlight) return inFlight;
    const creation = (async () => {
      const artifact = await this.library.find(id);
      const workspace = new WorkspaceService(() => {}); await workspace.open(artifact.root);
      const execution = new ArtifactAgentExecution(() => this.sandbox(id), (message) => this.emit({ type: 'agent', id, event: { type: 'stderr', message } }), (context) => this.capabilities.hostContext(context));
      const agent = new AgentService(workspace, (event) => this.emit({ type: 'agent', id, event }), this.settings, execution);
      this.agents.set(id, { workspace, agent }); return agent;
    })();
    this.agentCreations.set(id, creation);
    try { return await creation; } finally { this.agentCreations.delete(id); }
  }
  async startAgent(id: string, options: AgentStartOptions) {
    if (this.changingAccess.has(id)) throw new Error('File permissions are changing. Retry when saving finishes.');
    if (this.agentStarts.has(id)) throw new Error('Artifact agent is already starting.');
    const pending = (async () => (await this.agent(id)).start(options))();
    this.agentStarts.set(id, pending);
    try { return await pending; } finally { this.agentStarts.delete(id); }
  }
  async submit(id: string, text: string) {
    const artifact = await this.library.find(id);
    const grants = [...(artifact.access?.directories || []).map(({ id, label }) => ({ path: `/reads/${id}`, label })), ...(artifact.access?.allowWorkspaceRead ? [{ path: '/reads/workspace', label: 'Workbench repository, when open at session start' }] : [])];
    const instruction = `You are working in an independent skillz artifact repository: /repo. Build or update the requested artifact here. Read AGENTS.md and artifact.json. Preserve the Express/Vite dynamic-port runtime, use configured /api/<id> and /ws/<id> connections, and run npm run build. Shared source context is read-only at /context. Additional read-only folders are listed in SKILLZ_READ_ROOTS and mounted at /reads/<id>; use list_files and read_file there. Default file access is limited to /repo. Granted read-only directories: ${JSON.stringify(grants)}. Read AGENTS.md for the /files API. Never edit the source repository or the parent library.\n\nUser request:\n${text}`;
    return (await this.agent(id)).submit(instruction);
  }
  private async sandbox(id: string): Promise<ArtifactSandbox> {
    const artifact = await this.library.find(id);
    const access = await this.library.access(id);
    const reads = [...access.directories];
    const workspace = access.allowWorkspaceRead ? this.activeWorkspace() : '';
    if (workspace) reads.push({ id: 'workspace', label: 'Workbench repository', path: await fs.realpath(workspace) });
    return new ArtifactSandbox(artifact.root, reads, await this.library.contextDirectory(id));
  }
  async saveAccess(id: string, access: import('../../shared/artifacts').ArtifactAccess): Promise<void> {
    if (this.changingAccess.has(id)) throw new Error('File permissions are already being saved.');
    this.changingAccess.add(id);
    try {
      // Stop both consumers before changing grants. A failed stop must not report successful revocation.
      await this.stop(id);
      await (await this.agentCreations.get(id))?.stop();
      await this.agents.get(id)?.agent.stop();
      await stopArtifactContainers((await this.library.find(id)).root);
      await this.library.saveAccess(id, access);
    } finally { this.changingAccess.delete(id); }
  }
  private async disposeSessions(): Promise<void> { await Promise.allSettled(this.agentCreations.values()); await Promise.allSettled([...this.runtimes.keys()].map((id) => this.stop(id))); for (const session of this.agents.values()) { await session.agent.stop(); session.workspace.dispose(); } this.agents.clear(); await this.preview.dispose(); }
  async dispose(): Promise<void> { clearInterval(this.contextTimer); await this.disposeSessions(); }
}
