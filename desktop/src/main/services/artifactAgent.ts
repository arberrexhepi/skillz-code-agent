import { execFile, type ChildProcess, type ChildProcessWithoutNullStreams } from 'node:child_process';
import path from 'node:path';
import type { AgentStartOptions } from '../../shared/contracts';
import { ArtifactSandbox } from './artifactSandbox';
import { spawnModelHelper, terminate } from './artifactProcess';

export interface AgentExecution {
  launch(context: AgentLaunch): Promise<ChildProcessWithoutNullStreams>;
  message(payload: Record<string, unknown>): boolean;
  stop(): Promise<void>;
  prepareRuntime?(provider: string, model: string): Promise<() => void>;
}
export interface AgentLaunch { agentRoot: string; scriptName: string; python: { executable: string; args: string[] }; env: NodeJS.ProcessEnv; options: AgentStartOptions }
export class ArtifactAgentExecution implements AgentExecution {
  private sandbox?: ArtifactSandbox;
  private child?: ChildProcessWithoutNullStreams;
  private models = new Set<ChildProcess>();
  private context?: AgentLaunch;
  private baseContext?: AgentLaunch;
  private generation = 0;
  private selection?: { provider: string; model: string };
  constructor(private readonly createSandbox: () => Promise<ArtifactSandbox>, private readonly log: (text: string) => void, private readonly prepareHost?: (context: AgentLaunch) => Promise<AgentLaunch>) {}
  async launch(context: AgentLaunch): Promise<ChildProcessWithoutNullStreams> {
    const generation = this.generation;
    this.baseContext = context;
    if (this.prepareHost) context = await this.prepareHost(context);
    this.context = context; this.selection = context.options;
    await this.checkModel(context);
    if (generation !== this.generation) throw new Error('Agent start cancelled.');
    const sandbox = await this.createSandbox(); this.sandbox = sandbox;
    const prepared = await sandbox.prepare(this.log, context.agentRoot);
    if (generation !== this.generation) { await sandbox.stop(); throw new Error('Agent start cancelled.'); }
    const child = sandbox.spawn(prepared.docker, prepared.args, ['python3', '-u', '/opt/skillz/artifact_agent_entry.py', context.scriptName, '--provider', context.options.provider, '--model', context.options.model, '--root', '/repo', '--tools', '/opt/skillz/agent_tools.py', '--extension-bridge'], ['-i']);
    this.child = child;
    child.once('exit', () => { if (this.child === child) void this.stop().catch(() => {}); });
    return child;
  }
  private checkModel(context: AgentLaunch): Promise<void> {
    return new Promise((resolve, reject) => {
      const helper = execFile(context.python.executable, [...context.python.args, path.join(context.agentRoot, 'artifact_model_host.py'), '--check'], {
        cwd: context.agentRoot, env: context.env, windowsHide: true, encoding: 'utf8', timeout: 20_000, maxBuffer: 1024 * 1024,
      }, (error, stdout, stderr) => {
        this.models.delete(helper);
        if (error) { reject(new Error(`Could not check artifact model setup: ${stderr.trim() || error.message}`)); return; }
        try {
          const result = JSON.parse(stdout) as { ready?: boolean; error?: string };
          if (result.ready !== true) throw new Error(result.error || 'Artifact model setup check returned an invalid response.');
          resolve();
        } catch (cause) { reject(cause); }
      });
      this.models.add(helper);
      if (!helper.stdin) { helper.kill(); reject(new Error('Artifact setup check has no input stream.')); return; }
      helper.stdin.on('error', reject);
      helper.stdin.end(JSON.stringify({ provider: context.options.provider, model: context.options.model }));
    });
  }
  async prepareRuntime(provider: string, model: string): Promise<() => void> {
    const base = this.baseContext;
    const generation = this.generation;
    if (!base || !this.child) throw new Error('Artifact agent is not running.');
    let context = { ...base, options: { ...base.options, provider, model } };
    if (this.prepareHost) context = await this.prepareHost(context);
    await this.checkModel(context);
    if (generation !== this.generation) throw new Error('Runtime change cancelled.');
    // Keep the current broker and credentials until the bridge accepts the change.
    return () => {
      if (generation !== this.generation) throw new Error('Runtime change cancelled.');
      this.context = context;
      this.selection = { provider, model };
    };
  }
  message(payload: Record<string, unknown>): boolean {
    if (payload.type !== 'artifact_model_request') return false;
    const context = this.context, child = this.child;
    if (!context || !child || typeof payload.id !== 'string') return true;
    const reply = (value: Record<string, unknown>) => { if (this.child === child && !child.stdin.destroyed) child.stdin.write(JSON.stringify({ ...value, type: 'artifact_model_response', id: payload.id }) + '\n'); };
    if (payload.provider !== this.selection?.provider || payload.model !== this.selection?.model) { reply({ error: 'Model request does not match the selected runtime.' }); return true; }
    if (this.models.size >= 8) { reply({ error: 'Too many simultaneous model requests.' }); return true; }
    const helper = spawnModelHelper(context.python.executable, [...context.python.args, path.join(context.agentRoot, 'artifact_model_host.py')], { cwd: context.agentRoot, env: context.env, windowsHide: true });
    this.models.add(helper); let output = '', errors = ''; let final: Record<string, unknown> | undefined;
    const timeout = setTimeout(() => { void terminate(helper); reply({ error: 'Model request timed out.' }); }, 1200000);
    helper.stdout.setEncoding('utf8'); helper.stderr.setEncoding('utf8');
    helper.stdout.on('data', (text: string) => {
      output += text;
      if (output.length > 16 * 1024 * 1024) { void terminate(helper); return; }
      let newline: number;
      while ((newline = output.indexOf('\n')) >= 0) {
        const line = output.slice(0, newline); output = output.slice(newline + 1);
        try { const value = JSON.parse(line) as Record<string, unknown>; if ('progress' in value) reply(value); else final = value; }
        catch { errors = 'Model helper returned invalid JSON.'; }
      }
    });
    helper.stderr.on('data', (text: string) => { errors = (errors + text).slice(-3000); this.log(text); });
    helper.on('error', (error) => reply({ error: error.message }));
    helper.on('close', () => { clearTimeout(timeout); this.models.delete(helper); reply(final || { error: errors || 'Model helper returned an invalid response.' }); });
    helper.stdin.on('error', (error) => reply({ error: error.message }));
    helper.stdin.end(JSON.stringify(payload));
    return true;
  }
  async stop(): Promise<void> {
    this.generation++; this.child = undefined;
    await Promise.all([...this.models].map(terminate)); this.models.clear();
    const sandbox = this.sandbox;
    await sandbox?.stop();
    if (this.sandbox === sandbox) this.sandbox = undefined;
  }
}
