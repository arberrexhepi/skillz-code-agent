import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import type { RunRepository } from './artifactRunRepositories';
import type { AgentBridgeState, AgentEvent, AgentResponse, AgentStartOptions } from '../../shared/contracts';

export interface RunEvent { seq: number; at: number; type: string; data: Record<string, unknown> }
export interface RecordedRun {
  id: string; prompt: string; startedAt: string; status: 'running' | 'awaiting-input' | 'completed' | 'failed' | 'interrupted';
  selection: AgentStartOptions; repository?: RunRepository; events: RunEvent[]; state?: AgentBridgeState; message?: string; endedAt?: string;
}
export interface RunAgent {
  start(options: AgentStartOptions): Promise<AgentResponse>;
  submit(text: string): Promise<AgentResponse>;
  plannerAction(action: string, extras?: Record<string, unknown>): Promise<AgentResponse>;
  stop(): Promise<void>;
}
const MAX_EVENTS = 6000;
const allowedActions = new Set(['approve_plan', 'reject_plan', 'retry_failed_goal', 'resume_execution', 'select_discovery_mode', 'discovery_quick', 'discovery_moderate', 'discovery_deep', 'skip_discovery', 'approve_discovery_extension', 'decline_discovery_extension']);
const terminal = (run?: RecordedRun) => !run || ['completed', 'failed', 'interrupted'].includes(run.status);

// Bound provider output before putting it in a replay file or on the preview wire.
function bounded<T>(value: T): T {
  return JSON.parse(JSON.stringify(value, (key, value) => {
    if (key === 'thought') return undefined;
    if (typeof value === 'string') return value.slice(0, 16000);
    if (Array.isArray(value)) return value.slice(-100);
    return value;
  })) as T;
}
export class ArtifactRunSession {
  private runs: RecordedRun[] = [];
  private current?: RecordedRun;
  private agent?: RunAgent;
  private pending?: Promise<void>;
  private saving: Promise<void> = Promise.resolve();
  private persistenceError = '';
  private stateKey = '';
  private rawState?: AgentBridgeState;
  private readonly queuedSaves = new Set<string>();
  private closed = false;
  constructor(private readonly directory: string, readonly selection: AgentStartOptions, private readonly createAgent: (emit: (event: AgentEvent) => void, repository?: RunRepository) => Promise<RunAgent>) {}
  async load(): Promise<void> {
    await fs.mkdir(this.directory, { recursive: true });
    const entries = await fs.readdir(this.directory);
    for (const name of entries.filter(name => /^[a-f0-9-]+\.json$/.test(name))) {
      const file = path.join(this.directory, name);
      try {
        const stat = await fs.lstat(file);
        if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 32 * 1024 * 1024) continue;
        const run = JSON.parse(await fs.readFile(file, 'utf8')) as RecordedRun;
        if (run.id !== name.slice(0, -5) || !/^[a-f0-9-]{36}$/.test(run.id) || !Array.isArray(run.events) || typeof run.startedAt !== 'string' || !Number.isFinite(Date.parse(run.startedAt))) continue;
        if (!terminal(run)) { run.status = 'interrupted'; run.message = 'Runtime session ended before completion.'; run.endedAt = new Date().toISOString(); }
        this.runs.push(run);
      } catch { /* One damaged archive must not hide the other runs. */ }
    }
    this.runs.sort((a, b) => b.startedAt.localeCompare(a.startedAt));
    this.runs = this.runs.slice(0, 20);
  }
  summary() {
    return { available: !this.closed, selection: this.selection, busy: Boolean(this.pending), activeRunId: !terminal(this.current) ? this.current?.id : undefined, persistenceError: this.persistenceError, runs: this.runs.map(({ events, state, ...run }) => ({ ...run, eventCount: events.length })) };
  }
  get(id: string, after = 0) {
    const run = this.runs.find(run => run.id === id);
    if (!run) throw new Error('Run not found.');
    return { ...run, events: run.events.filter(event => event.seq > after), eventCount: run.events.length };
  }
  private record(type: string, data: Record<string, unknown>): void {
    const run = this.current;
    if (!run || terminal(run)) return;
    if (run.events.length >= MAX_EVENTS) {
      run.status = 'failed'; run.message = 'Recording reached its 6,000-event limit. The agent was stopped; the saved trace is partial.';
      run.endedAt = new Date().toISOString();
      void this.agent?.stop().catch(() => {}); this.save(run); return;
    }
    run.events.push({ seq: run.events.length + 1, at: Math.max(0, Date.now() - Date.parse(run.startedAt)), type, data: bounded(data) });
    this.save(run);
  }
  private receive = (event: AgentEvent): void => {
    if (event.type === 'state') {
      if (this.current && !terminal(this.current)) { this.rawState = event.state; this.current.state = bounded(event.state); }
      const planner = event.state.planner;
      const state = { status: planner.status, goal: planner.executing_goal_title, goalId: planner.executing_goal_id, pendingVerification: planner.worker_state?.pending_verification, editBatch: planner.worker_state?.edit_batch, facts: planner.worker_state?.current_run_facts?.length };
      const key = JSON.stringify(state);
      if (this.stateKey !== key) { this.stateKey = key; this.record('state', state); }
    } else if (event.type === 'progress') {
      const { state, ...payload } = event.payload;
      this.record('progress', payload);
    } else if (event.type === 'status') {
      this.record('runtime', { status: event.status, message: event.message });
      if (event.status === 'error' && this.current && !terminal(this.current)) {
        this.current.status = 'failed'; this.current.message = event.message || 'Runtime failed.'; this.current.endedAt = new Date().toISOString();
        this.save(this.current); void this.agent?.stop().catch(() => {});
      }
    }
  };
  async start(prompt: string, repository?: RunRepository): Promise<string> {
    if (this.closed) throw new Error('Runtime bridge is closed.');
    if (this.pending || !terminal(this.current)) throw new Error('Finish or stop the current run first.');
    const run: RecordedRun = { id: randomUUID(), prompt, startedAt: new Date().toISOString(), status: 'running', selection: this.selection, repository, events: [] };
    this.current = run; this.runs.unshift(run); this.stateKey = ''; this.rawState = undefined;
    const removed = this.runs.splice(20);
    for (const old of removed) this.saving = this.saving.then(() => fs.unlink(path.join(this.directory, old.id + '.json')).catch(() => {}));
    this.record('input', { prompt, repository });
    this.launch(async () => {
      await this.agent?.stop();
      if (terminal(run)) return;
      this.agent = await this.createAgent(event => { if (this.current?.id === run.id) this.receive(event); }, repository);
      if (terminal(run)) { await this.agent.stop(); return; }
      await this.agent.start({ ...this.selection, backendScript: 'main_v2.py' });
      if (terminal(run)) { await this.agent.stop(); return; }
      this.record('request', { action: 'submit', text: prompt });
      this.finish(await this.agent.submit(prompt));
    });
    return run.id;
  }
  async continue(id: string, input: { text?: string; action?: string }): Promise<void> {
    if (this.closed || this.pending || this.current?.id !== id || this.current.status !== 'awaiting-input' || !this.agent) throw new Error('This run is not awaiting input.');
    const agent = this.agent;
    const planner = this.rawState?.planner;
    const action = input.action ? planner?.suggested_next_actions?.find(item => item.type === input.action) : undefined;
    if (input.action && (!action || !allowedActions.has(input.action))) throw new Error('This action is not currently offered by the planner.');
    this.current.status = 'running';
    this.record('request', { action: input.action || 'submit', text: input.text });
    if (terminal(this.current)) return;
    this.launch(async () => {
      const extras: Record<string, unknown> = {};
      if (action?.mode) extras.mode = action.mode;
      if (action?.request_id) extras.request_id = action.request_id;
      if (input.action === 'approve_plan' || input.action === 'reject_plan') extras.expected_plan = planner?.pending_plan || planner?.paused_plan;
      this.finish(input.action ? await agent.plannerAction(input.action, extras) : await agent.submit(input.text!));
    });
  }
  private launch(operation: () => Promise<void>): void {
    this.pending = operation().catch(error => {
      if (!this.current || terminal(this.current)) return;
      this.record('error', { message: String(error) });
      this.current.status = 'failed'; this.current.message = String(error); this.current.endedAt = new Date().toISOString(); this.save(this.current);
    }).finally(() => { this.pending = undefined; });
  }
  private finish(response: AgentResponse): void {
    const run = this.current;
    if (!run || terminal(run)) return;
    this.rawState = response.state; run.state = bounded(response.state); run.message = response.message;
    const completed = response.state.planner.status === 'completed';
    const status = !response.ok ? 'failed' : completed ? 'completed' : 'awaiting-input';
    this.record('response', { ok: response.ok, message: response.message, status, plannerStatus: response.state.planner.status });
    if (terminal(run)) return;
    run.status = status;
    if (terminal(run)) run.endedAt = new Date().toISOString();
    this.save(run);
  }
  private save(run: RecordedRun): void {
    // Serialize snapshots so an older write cannot overwrite a later terminal event.
    if (this.queuedSaves.has(run.id)) return;
    this.queuedSaves.add(run.id);
    const file = path.join(this.directory, run.id + '.json');
    this.saving = this.saving.then(async () => {
      this.queuedSaves.delete(run.id);
      const content = JSON.stringify(run);
      await fs.writeFile(file + '.tmp', content, { mode: 0o600 });
      await fs.rename(file + '.tmp', file);
    }).catch(error => { this.persistenceError = `Could not save run history: ${String(error)}`; });
  }
  async stop(): Promise<void> {
    const run = this.current;
    if (run && !terminal(run)) { this.record('interrupted', { message: 'Run stopped.' }); run.status = 'interrupted'; run.message = 'Run stopped before completion.'; run.endedAt = new Date().toISOString(); this.save(run); }
    await this.agent?.stop();
    await this.pending;
    await this.saving;
  }
  async close(): Promise<void> { this.closed = true; await this.stop(); }
}
