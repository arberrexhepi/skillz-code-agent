import { execFile } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { safeStorage, shell } from 'electron';
import type { ArtifactCapabilities, ArtifactCapability, ArtifactSetupProgress, ArtifactSetupSelection } from '../../shared/artifacts';
import type { AgentLaunch } from './artifactAgent';
import type { ArtifactPreviewService } from './artifactPreview';
import { dockerCommand, ensureSandboxImage, harnessRoot, sandboxImageReady } from './artifactSandbox';
import { command, runLogged } from './artifactProcess';
import { pythonEnvironment, resolvePythonCommand, type PythonCommand } from './python';
import { readJson, writeJson } from './artifactLibrary';

const providerKeys: Record<string, string> = { gemini: 'GEMINI_API_KEY', openai: 'OPENAI_API_KEY', anthropic: 'ANTHROPIC_API_KEY', meta: 'META_AI_API_KEY' };
const packages: Record<string, string> = { gemini: 'google-genai', openai: 'openai', anthropic: 'anthropic', meta: 'openai', local: 'openai', ollama: 'openai', 'ollama-local': 'openai', 'ollama-runpod': 'openai' };
interface ProviderProbe { sdkReady: boolean; keyReady: boolean; keyName?: string; label: string; }
export class ArtifactCapabilitiesService {
  private progress: ArtifactSetupProgress = { running: false, step: '', log: '' };
  private installation?: Promise<ArtifactCapabilities>;
  private keyQueue: Promise<unknown> = Promise.resolve();
  constructor(private readonly home: string, private readonly preview: ArtifactPreviewService, private readonly emit: (progress: ArtifactSetupProgress) => void, private readonly source = harnessRoot, private readonly storage = safeStorage, private readonly tools = { resolvePythonCommand, command, runLogged, dockerCommand, sandboxImageReady, ensureSandboxImage }) {}
  snapshot(): ArtifactSetupProgress { return { ...this.progress }; }
  private update(change: Partial<ArtifactSetupProgress>): void { this.progress = { ...this.progress, ...change }; this.emit(this.snapshot()); }
  private log = (text: string): void => { this.update({ log: (this.progress.log + text).slice(-12000) }); };
  private managedPython(): string { return path.join(this.home, 'python', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'); }
  private async python(source: string, env: NodeJS.ProcessEnv): Promise<PythonCommand> {
    const managed = this.managedPython();
    if (await fs.stat(managed).then(stat => stat.isFile(), () => false)) return this.tools.resolvePythonCommand(source, process.platform, { ...env, PYTHON_AGENT_PYTHON: managed });
    return this.tools.resolvePythonCommand(source, process.platform, env);
  }
  private canSaveKey(): boolean { return Boolean(this.storage?.isEncryptionAvailable() && (process.platform !== 'linux' || this.storage.getSelectedStorageBackend() !== 'basic_text')); }
  private async keys(): Promise<Record<string, string>> {
    try { return await readJson(path.join(this.home, 'provider-keys.json')) as Record<string, string>; }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return {}; throw error; }
  }
  async saveKey(provider: string, key: string | null): Promise<void> {
    const name = providerKeys[provider];
    if (!name) throw new Error('This provider does not use a saved API key.');
    const pending = this.keyQueue.then(async () => {
      const keys = await this.keys();
      if (key) {
        if (!this.canSaveKey()) throw new Error('Secure credential storage is unavailable. Configure the API key in the environment that launches the workbench.');
        keys[name] = this.storage.encryptString(key).toString('base64');
      } else delete keys[name];
      await writeJson(path.join(this.home, 'provider-keys.json'), keys);
    });
    this.keyQueue = pending.catch(() => {});
    await pending;
  }
  private async environment(provider: string, base = pythonEnvironment()): Promise<NodeJS.ProcessEnv> {
    const name = providerKeys[provider];
    if (!name) return base;
    const saved = (await this.keys())[name];
    if (!saved) return base;
    if (!this.canSaveKey()) throw new Error('Unlock secure credential storage to use the saved artifact API key.');
    return { ...base, [name]: this.storage.decryptString(Buffer.from(saved, 'base64')) };
  }
  async hostContext(context: AgentLaunch): Promise<AgentLaunch> {
    const env = await this.environment(context.options.provider, context.env);
    return { ...context, env, python: await this.python(context.agentRoot, env) };
  }
  private probe(python: PythonCommand, env: NodeJS.ProcessEnv, selection: ArtifactSetupSelection): Promise<ProviderProbe> {
    return new Promise((resolve, reject) => {
      const child = execFile(python.executable, [...python.args, path.join(this.source(), 'artifact_model_host.py'), '--capabilities'], { cwd: this.source(), env, windowsHide: true, encoding: 'utf8', timeout: 15000, maxBuffer: 256000 }, (error, stdout, stderr) => {
        if (error) { reject(new Error(stderr.trim() || error.message)); return; }
        try { const result = JSON.parse(stdout); if (result.error) throw new Error(result.error); if (typeof result.sdkReady !== 'boolean') throw new Error('Invalid provider setup response.'); resolve(result); } catch (cause) { reject(cause); }
      });
      child.stdin?.on('error', reject);
      child.stdin?.end(JSON.stringify(selection));
    });
  }
  async status(selection: ArtifactSetupSelection): Promise<ArtifactCapabilities> {
    const source = this.source();
    const pythonItems = (async (): Promise<ArtifactCapability[]> => {
      let python: PythonCommand;
      try { python = await this.python(source, pythonEnvironment()); }
      catch { return [{ id: 'python', label: 'Python', ready: false, detail: 'Install Python 3.10 or newer, then recheck. Restart the workbench if its PATH changed.', download: 'python' }, { id: 'provider', label: 'Provider SDK', ready: false, detail: 'Python is required before checking provider support.' }]; }
      const items: ArtifactCapability[] = [{ id: 'python', label: 'Python', ready: true, detail: [python.executable, ...python.args].join(' ') }];
      try {
        const probe = await this.probe(python, await this.environment(selection.provider), selection);
        items.push({ id: 'provider', label: `${probe.label} support`, ready: probe.sdkReady, detail: packages[selection.provider] ? (probe.sdkReady ? 'Provider SDK installed.' : `Install ${packages[selection.provider]} in a workbench-managed Python environment.`) : 'Uses the local Codex CLI; sign in under Agent runtime below.', installable: !probe.sdkReady && Boolean(packages[selection.provider]) });
        if (probe.keyName) items.push({ id: 'credentials', label: 'API key', ready: probe.keyReady, detail: probe.keyReady ? 'Configured. The key has not been validated with the provider.' : `Add ${probe.keyName} below to connect this provider.` });
      } catch (error) { items.push({ id: 'provider', label: 'Provider setup', ready: false, detail: String(error) }); }
      return items;
    })();
    const dockerItems = (async (): Promise<ArtifactCapability[]> => {
      let docker: string;
      try { docker = await this.tools.dockerCommand(); }
      catch { return [{ id: 'docker', label: 'Docker', ready: false, detail: 'Install Docker Desktop, enable Linux containers, and start its engine.', download: 'docker' }, { id: 'runtime', label: 'Artifact runtime', ready: false, detail: 'Start Docker to prepare the isolated runtime.' }]; }
      try { if (await this.tools.command(docker, ['info', '--format', '{{.OSType}}'], source) !== 'linux') throw new Error('Linux containers required'); }
      catch { return [{ id: 'docker', label: 'Docker', ready: false, detail: 'Start Docker Desktop with Linux containers, wait for its engine, then recheck.', download: 'docker' }, { id: 'runtime', label: 'Artifact runtime', ready: false, detail: 'Waiting for Docker.' }]; }
      const ready = await this.tools.sandboxImageReady(docker, source);
      return [{ id: 'docker', label: 'Docker', ready: true, detail: 'Linux container engine running.' }, { id: 'runtime', label: 'Artifact runtime', ready, detail: ready ? 'Isolated runtime prepared.' : 'Download and prepare the runtime once for this workbench version.', installable: !ready }];
    })();
    const [python, docker, browser, git] = await Promise.all([pythonItems, dockerItems, this.preview.browserReady(), this.tools.command('git', ['--version'], source).then(() => true, () => false)]);
    const items: ArtifactCapability[] = [...python, { id: 'git', label: 'Git', ready: git, detail: git ? 'Ready to version your artifacts.' : 'Install Git, then recheck.', ...(!git ? { download: 'git' as const } : {}) }, ...docker, { id: 'browser', label: 'Playwright inspection browser', ready: browser, optional: true, detail: browser ? 'Available for browser inspection. Live previews use the built-in browser.' : 'Optional browser for agent inspection tooling. Live previews work without this download.', installable: !browser }];
    const keys = await this.keys();
    return { selection, items, ready: items.every(item => item.ready || item.optional), keyName: providerKeys[selection.provider], keySaved: Boolean(keys[providerKeys[selection.provider]]), canSaveKey: this.canSaveKey() };
  }
  install(selection: ArtifactSetupSelection): Promise<ArtifactCapabilities> {
    if (this.installation) return Promise.reject(new Error('Artifact capabilities are already being installed.'));
    this.update({ running: true, step: 'Checking capabilities', log: '', error: undefined });
    this.installation = this.performInstall(selection).catch(error => { this.update({ error: String(error), step: 'Setup needs attention' }); throw error; }).finally(() => { this.installation = undefined; this.update({ running: false }); });
    return this.installation;
  }
  private async performInstall(selection: ArtifactSetupSelection): Promise<ArtifactCapabilities> {
    const status = await this.status(selection);
    const needed = new Set(status.items.filter(item => item.installable && !item.optional).map(item => item.id));
    if (needed.has('provider')) {
      const pkg = packages[selection.provider];
      if (!pkg) throw new Error('No installable SDK for this provider.');
      this.update({ step: `Installing ${pkg}` });
      await fs.mkdir(this.home, { recursive: true });
      if (!(await fs.stat(this.managedPython()).then(stat => stat.isFile(), () => false))) {
        const base = await this.tools.resolvePythonCommand(this.source(), process.platform, pythonEnvironment());
        await this.tools.runLogged(base.executable, [...base.args, '-m', 'venv', path.join(this.home, 'python')], this.source(), this.log);
      }
      await this.tools.runLogged(this.managedPython(), ['-m', 'pip', 'install', '--disable-pip-version-check', pkg], this.source(), this.log);
    }
    if (needed.has('runtime')) { this.update({ step: 'Preparing artifact runtime' }); await this.tools.ensureSandboxImage(this.log, this.source()); }
    this.update({ step: 'Rechecking capabilities' });
    const result = await this.status(selection);
    this.update({ step: result.ready ? 'Capabilities ready' : 'Downloads complete — finish the remaining setup below' });
    return result;
  }
  async openDownload(tool: 'python' | 'git' | 'docker'): Promise<void> {
    const urls = { python: 'https://www.python.org/downloads/', git: 'https://git-scm.com/downloads/', docker: 'https://docs.docker.com/desktop/setup/install/' };
    await shell.openExternal(urls[tool]);
  }
}
