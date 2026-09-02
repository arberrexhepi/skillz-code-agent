import { execFile, spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { app } from 'electron';
import type {
  AgentBridgeState,
  AgentEvent,
  AgentResponse,
  AgentStartOptions,
} from '../../shared/contracts';
import type { CodexSubscriptionStatus, JsonMap, RuntimeOptionsPayload } from '../../shared/agentTypes';
import type { WorkspaceService } from './workspace';
import { pythonEnvironment, resolvePythonCommand } from './python';
import type { RuntimeSettingsService } from './runtimeSettings';

interface PendingRequest {
  resolve: (response: AgentResponse) => void;
  reject: (error: Error) => void;
}

const BACKEND_SCRIPTS = new Set(['main.py', 'main_v2.py', 'live_test_loop.py']);

export class AgentService {
  private process: ChildProcessWithoutNullStreams | null = null;
  private buffer = '';
  private processCodexPath = '';
  private stopping = false;
  private readonly pending = new Map<string, PendingRequest>();
  private state: AgentBridgeState = { planner: {}, transcript: [] };

  constructor(
    private readonly workspace: WorkspaceService,
    private readonly emit: (event: AgentEvent) => void,
    private readonly runtimeSettings?: RuntimeSettingsService,
  ) {}

  async start(options: AgentStartOptions): Promise<AgentResponse> {
    await this.stop();
    const agentRoot = this.agentRoot();
    const scriptName = options.backendScript || 'main.py';
    if (!BACKEND_SCRIPTS.has(scriptName)) throw new Error('Unsupported Python agent backend.');
    const script = path.join(agentRoot, scriptName);
    const tools = path.join(agentRoot, 'agent_tools.py');
    if (!existsSync(script) || !existsSync(tools)) {
      throw new Error(`Python agent resources were not found at ${agentRoot}.`);
    }

    const { env } = await this.launchEnvironment();
    const python = await resolvePythonCommand(agentRoot, process.platform, env);
    this.stopping = false;
    this.buffer = '';
    this.state = { planner: {}, transcript: [] };
    this.emit({ type: 'status', status: 'starting' });
    const child = spawn(python.executable, [
      ...python.args,
      script,
      '--provider', options.provider,
      '--model', options.model,
      '--root', this.workspace.requireRoot(),
      '--tools', tools,
      '--extension-bridge',
    ], {
      cwd: agentRoot,
      env,
      windowsHide: true,
    });
    this.process = child;
    this.processCodexPath = env.CODEX_CLI_PATH || '';

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => this.handleStdout(chunk));
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (chunk: string) => {
      const message = chunk.trim();
      if (message) this.emit({ type: 'stderr', message });
    });
    child.on('error', (error) => this.handleExit(child, new Error(`Could not start Python agent: ${error.message}`)));
    child.on('exit', (code, signal) => {
      const suffix = code !== null ? ` with code ${code}` : signal ? ` (${signal})` : '';
      this.handleExit(child, new Error(`Python agent exited${suffix}.`));
    });

    const response = await this.request('initialize', {});
    this.emit({ type: 'status', status: 'running' });
    return response;
  }

  submit(text: string): Promise<AgentResponse> {
    return this.request('submit', { text });
  }

  plannerAction(action: string, extras: Record<string, unknown> = {}): Promise<AgentResponse> {
    return this.request('planner_action', { action, ...extras });
  }

  workerAction(action: JsonMap): Promise<AgentResponse> {
    return this.request('worker_action', { action });
  }

  reconfigureRuntime(provider: string, model: string): Promise<AgentResponse> {
    return this.request('reconfigure_runtime', { provider, model });
  }

  configureBackoff(enabled: boolean, tokenLimitK: number): Promise<AgentResponse> {
    return this.request('configure_backoff', { enabled, token_limit_k: tokenLimitK });
  }

  async runtimeOptions(provider = '', model = ''): Promise<RuntimeOptionsPayload> {
    if (this.process) {
      const response = await this.request('runtime_options', {});
      return response.runtime_options || {};
    }
    const agentRoot = this.agentRoot();
    const { env } = await this.launchEnvironment();
    const python = await resolvePythonCommand(agentRoot, process.platform, env);
    const script = [
      'import json, sys',
      'from runtime_catalog import runtime_options_payload',
      'print(json.dumps(runtime_options_payload(current_provider=sys.argv[1], current_model=sys.argv[2])))',
    ].join('; ');
    return new Promise<RuntimeOptionsPayload>((resolve, reject) => {
      execFile(python.executable, [...python.args, '-c', script, provider, model], {
        cwd: agentRoot,
        env,
        windowsHide: true,
        encoding: 'utf8',
        maxBuffer: 2 * 1024 * 1024,
      }, (error, stdout, stderr) => {
        if (error) {
          reject(new Error(`Could not load Python runtime catalog: ${String(stderr || error.message).trim()}`));
          return;
        }
        try {
          resolve(JSON.parse(stdout) as RuntimeOptionsPayload);
        } catch (cause) {
          reject(new Error(`Python runtime catalog returned invalid JSON: ${String(cause)}`));
        }
      });
    });
  }

  codexSubscriptionStatus(): Promise<CodexSubscriptionStatus> {
    return this.runCodexSubscriptionCommand('status', 20_000);
  }

  codexSubscriptionLogin(): Promise<CodexSubscriptionStatus> {
    return this.runCodexSubscriptionCommand('login', 5 * 60_000);
  }

  async setCodexCliPath(candidate: string | null): Promise<CodexSubscriptionStatus> {
    if (!this.runtimeSettings) throw new Error('Runtime settings storage is unavailable.');
    await this.runtimeSettings.setCodexCliPath(candidate);
    try {
      return await this.codexSubscriptionStatus();
    } catch (error) {
      const { env, savedPath } = await this.launchEnvironment();
      return {
        available: false, authenticated: false, configured_cli_path: savedPath,
        cli_path_source: savedPath ? 'settings' : env.CODEX_CLI_PATH ? 'environment' : 'discovery',
        restart_required: Boolean(this.process && this.processCodexPath !== (env.CODEX_CLI_PATH || '')),
        error: `The path setting was saved, but status could not be checked: ${String(error)}`,
      };
    }
  }

  private async launchEnvironment(): Promise<{ env: NodeJS.ProcessEnv; savedPath: string }> {
    const savedPath = await this.runtimeSettings?.codexCliPath() || '';
    const env = pythonEnvironment();
    if (savedPath) env.CODEX_CLI_PATH = savedPath;
    return { env, savedPath };
  }

  async stop(): Promise<void> {
    if (!this.process) return;
    this.stopping = true;
    this.process.kill();
    this.process = null;
    this.buffer = '';
    this.rejectPending(new Error('Python agent stopped.'));
    this.state = { planner: {}, transcript: [] };
    this.emit({ type: 'status', status: 'stopped' });
  }

  private request(type: string, payload: Record<string, unknown>): Promise<AgentResponse> {
    if (!this.process) return Promise.reject(new Error('Start the Python agent first.'));
    const id = `${Date.now()}-${crypto.randomUUID()}`;
    return new Promise<AgentResponse>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.process?.stdin.write(`${JSON.stringify({ id, type, ...payload })}\n`);
    });
  }

  private handleStdout(chunk: string): void {
    this.buffer += chunk;
    while (true) {
      const newline = this.buffer.indexOf('\n');
      if (newline < 0) return;
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (!line) continue;
      try {
        const payload = JSON.parse(line) as Record<string, unknown>;
        if (isBridgeState(payload.state)) {
          this.state = payload.state;
          this.emit({ type: 'state', state: this.state });
        }
        if (payload.type === 'progress' || payload.type === 'goal_start' || payload.type === 'goal_finish') {
          this.emit({ type: 'progress', payload: payload as import('../../shared/agentTypes').AgentProgressMessage });
          continue;
        }
        const response = payload as unknown as AgentResponse;
        const id = typeof payload.id === 'string' ? payload.id : '';
        const pending = id ? this.pending.get(id) : undefined;
        if (pending) {
          this.pending.delete(id);
          pending.resolve({ ...response, state: isBridgeState(response.state) ? response.state : this.state });
        }
      } catch (error) {
        this.emit({ type: 'status', status: 'error', message: `Invalid agent response: ${String(error)}` });
      }
    }
  }

  private handleExit(source: ChildProcessWithoutNullStreams, error: Error): void {
    if (this.process !== source) return;
    const intentional = this.stopping;
    this.process = null;
    this.buffer = '';
    this.rejectPending(error);
    if (!intentional) this.emit({ type: 'status', status: 'error', message: error.message });
  }

  private rejectPending(error: Error): void {
    for (const request of this.pending.values()) request.reject(error);
    this.pending.clear();
  }

  private agentRoot(): string {
    return app.isPackaged
      ? path.join(process.resourcesPath, 'python-agent')
      : path.resolve(app.getAppPath(), '..');
  }

  private async runCodexSubscriptionCommand(
    command: 'status' | 'login',
    timeout: number,
  ): Promise<CodexSubscriptionStatus> {
    const agentRoot = this.agentRoot();
    const helper = path.join(agentRoot, 'codex_subscription.py');
    if (!existsSync(helper)) return Promise.reject(new Error('Codex subscription helper is missing.'));
    const { env, savedPath } = await this.launchEnvironment();
    const python = await resolvePythonCommand(agentRoot, process.platform, env);
    return new Promise<CodexSubscriptionStatus>((resolve, reject) => {
      execFile(python.executable, [...python.args, helper, command], {
        cwd: agentRoot,
        env,
        windowsHide: true,
        encoding: 'utf8',
        timeout,
        maxBuffer: 2 * 1024 * 1024,
      }, (error, stdout, stderr) => {
        let payload: CodexSubscriptionStatus | undefined;
        try {
          payload = JSON.parse(stdout) as CodexSubscriptionStatus;
        } catch {
          // The process error below carries the useful diagnostic.
        }
        if (payload) {
          resolve({
            ...payload,
            configured_cli_path: savedPath,
            cli_path_source: savedPath ? 'settings' : env.CODEX_CLI_PATH ? 'environment' : 'discovery',
            restart_required: Boolean(this.process && this.processCodexPath !== (env.CODEX_CLI_PATH || '')),
          });
          return;
        }
        const detail = String(stderr || error?.message || 'Codex subscription helper returned invalid JSON.').trim();
        reject(new Error(detail));
      });
    });
  }
}

function isBridgeState(value: unknown): value is AgentBridgeState {
  if (!value || typeof value !== 'object') return false;
  const state = value as Partial<AgentBridgeState>;
  return Boolean(state.planner && typeof state.planner === 'object' && Array.isArray(state.transcript));
}
