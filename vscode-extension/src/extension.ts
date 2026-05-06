import * as vscode from 'vscode';
import { ChildProcessWithoutNullStreams, execFile, spawn } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { describeBackendScript, extractJsonLikeStringSetting, preferredBackendScriptValue } from './backendConfig';

import {
  buildPlannerActionMessage,
  buildReviewReport,
  combineSuggestedActions,
  continuousModeOwnsLifecycle,
  groupLatestDiagnosticsByPath,
  isContinuousModeActive,
  progressTimelineTarget,
  primaryPathForReview,
} from './panelModel';

type JsonMap = Record<string, unknown>;

interface PlannerSuggestedAction extends JsonMap {
  type: string;
  label?: string;
  style?: string;
  mode?: string;
  issue_id?: string;
}

interface WorkerSuggestedAction extends JsonMap {
  type: string;
  label?: string;
  style?: string;
  requires_confirmation?: boolean;
}

interface DiagnosticItem extends JsonMap {
  path?: string;
  line?: number;
  column?: number;
  code?: string;
  message?: string;
}

interface LatestDiagnostics extends JsonMap {
  path?: string;
  message?: string;
  diagnostic_engine?: string;
  diagnostics?: DiagnosticItem[];
  step?: number;
  source?: string;
}

interface ReviewFile extends JsonMap {
  path?: string;
  risk?: string;
  validation?: string;
  added?: number;
  deleted?: number;
}

interface SkillSummary extends JsonMap {
  name?: string;
  description?: string;
  args_schema?: JsonMap;
  tags?: string[];
  category?: string;
  priority?: number;
  modes?: string[];
}

interface BackendInfo extends JsonMap {
  launched_script_path?: string;
  launched_script_name?: string;
  launched_runtime_key?: string;
  launched_runtime_label?: string;
  configured_script_path?: string;
  configured_script_name?: string;
  configured_runtime_key?: string;
  configured_runtime_label?: string;
  mismatch?: boolean;
  workspace_folder_path?: string;
  backend_script_effective_value?: string;
  backend_script_default_value?: string;
  backend_script_global_value?: string;
  backend_script_workspace_value?: string;
  backend_script_workspace_folder_value?: string;
  backend_script_selected_source?: string;
}

interface LatestReview extends JsonMap {
  action_type?: string;
  summary?: string;
  step?: number;
  path?: string;
  diff?: string;
  stat?: string;
  files?: Array<string | ReviewFile>;
  staged?: boolean;
  counts?: JsonMap;
  high_risk_paths?: string[];
  review_summary?: JsonMap;
}

interface WorkerState extends JsonMap {
  issue_state?: {
    active_issue_id?: string;
    active_issue?: {
      issue_id?: string;
      plan_summary?: string;
      request_summary?: string;
      status?: string;
    } | null;
    reopenable_issues?: Array<{
      issue_id?: string;
      plan_summary?: string;
      request_summary?: string;
      status?: string;
    }>;
    total_fact_count?: number;
  };
  runtime_config?: {
    provider?: string;
    model?: string;
    thinking_mode?: string;
    verbosity?: string;
  };
  active_error?: {
    message?: string;
    path?: string;
    error_type?: string;
    diagnostic_engine?: string;
    diagnostics?: DiagnosticItem[];
    suggested_next_actions?: WorkerSuggestedAction[];
  } | null;
  pending_verification?: {
    path?: string;
    mode?: string;
  } | null;
  edit_batch?: {
    active?: boolean;
    pending_paths?: string[];
    pending_count?: number;
    started_thought?: string;
  };
  current_run_facts?: Array<{ key?: string; value?: string }>;
  available_skills?: SkillSummary[];
  last_run_result?: {
    final_message?: string;
    validation_passed?: boolean;
    validation_ran?: boolean;
  } | null;
  latest_diagnostics?: LatestDiagnostics | null;
  latest_review?: LatestReview | null;
  suggested_next_actions?: WorkerSuggestedAction[];
  protected_paths?: string[];
}

interface ContinuousModeState extends JsonMap {
  enabled?: boolean;
  status?: string;
  cycle?: number;
  max_cycles?: number;
  active_issue_id?: string;
  selected_discovery_mode?: string;
  latest_review_decision?: string;
  stop_reason?: string;
  created_followup_issue_ids?: string[];
}

interface PlannerGoal extends JsonMap {
  goal_id?: string;
  title?: string;
  goal?: string;
  reason?: string;
  depends_on?: string[];
  estimated_scope?: string;
  success_signals?: string[];
  delegation_notes?: string[];
}

interface DiscoveryResult extends JsonMap {
  mode?: string;
  reason?: string;
  prompt?: string;
  final_message?: string;
  ok?: boolean;
  touched_paths?: string[];
  duration_s?: number;
  tool_calls_used?: number;
  tool_calls_max?: number;
  usage_summary?: string;
}

interface PlannerState extends JsonMap {
  runtime_config?: {
    provider?: string;
    model?: string;
    thinking_mode?: string;
    verbosity?: string;
  };
  status?: string;
  latest_request?: string;
  pending_plan?: {
    summary?: string;
    goals?: PlannerGoal[];
    assumptions?: string[];
    clarification_summary?: string;
    next_steps_preview?: string[];
    confirmation_prompt?: string;
  } | null;
  pending_discovery?: {
    reason?: string;
    prompt?: string;
    recommended_mode?: string;
  } | null;
  last_discovery?: DiscoveryResult | null;
  last_execution_summary?: string;
  last_presented_plan?: {
    summary?: string;
    goals?: PlannerGoal[];
    assumptions?: string[];
  } | null;
  last_completed_plan?: {
    summary?: string;
    goals?: PlannerGoal[];
  } | null;
  last_completed_results?: Array<{ title?: string; final_message?: string; status?: string }>;
  completed_results?: Array<{ title?: string; final_message?: string; status?: string; goal_id?: string }>;
  executing?: boolean;
  executing_goal_index?: number;
  executing_goal_id?: string;
  executing_goal_title?: string;
  executing_goal_count?: number;
  active_issue_id?: string;
  issue_state?: JsonMap;
  continuous_mode?: ContinuousModeState;
  repo_facts_status_lines?: string[];
  planner_usage_summary?: string;
  suggested_next_actions?: PlannerSuggestedAction[];
  worker_state?: WorkerState | null;
}

interface BridgeState {
  planner: PlannerState;
  transcript: Array<{ role: string; content: string }>;
  last_message?: string;
}

interface BridgeResponse {
  id?: string;
  ok: boolean;
  message?: string;
  state: BridgeState;
  backoff?: { enabled: boolean; token_limit_k: number; window_tokens_used: number };
}

interface RuntimeProviderOption extends JsonMap {
  key?: string;
  label?: string;
  package?: string | null;
  env_var?: string | null;
  default_model?: string;
  suggested_models?: string[];
  notes?: string;
  active?: boolean;
  active_model?: string;
  accepts_custom_model?: boolean;
  hidden?: boolean;
}

interface RuntimeOptionsPayload extends JsonMap {
  providers?: RuntimeProviderOption[];
  provider_keys?: string[];
  current_provider?: string;
  current_model?: string;
}

interface RuntimeOptionsResponse extends BridgeResponse {
  runtime_options?: RuntimeOptionsPayload;
}

const DEFAULT_MODEL_BY_PROVIDER: Record<string, string> = {
  openai: 'gpt-5.4',
  anthropic: 'claude-sonnet-4-6',
  gemini: 'gemini-3-flash-preview',
  local: 'gemma4',
  ollama: 'gemma4:e2b',
  'ollama-local': 'gemma4:e2b',
  'ollama-runpod': 'gemma4:latest',
};

function defaultModelForProvider(provider: string): string {
  const normalized = String(provider || '').trim().toLowerCase();
  return DEFAULT_MODEL_BY_PROVIDER[normalized] || DEFAULT_MODEL_BY_PROVIDER.gemini;
}

interface ProgressMessage extends JsonMap {
  type: 'progress' | 'goal_start' | 'goal_finish';
  domain?: string;
  step?: number;
  action_type?: string;
  skill_name?: string;
  skill_mode?: string;
  skill_count?: number;
  path?: string;
  ok?: boolean;
  elapsed_s?: number;
  thought?: string;
  summary?: string;
  diff?: string;
  replacements?: number;
  added_lines?: number;
  removed_lines?: number;
  search_excerpt?: string;
  replace_excerpt?: string;
  inspected_file_count?: number;
  inspected_files?: Array<{
    path?: string;
    start_line?: number | null;
    end_line?: number | null;
    ok?: boolean;
    error?: string;
  }>;
  goal_index?: number;
  goal_id?: string;
  goal_title?: string;
  state: BridgeState;
}

function primaryWorkspaceFolder(): vscode.WorkspaceFolder | undefined {
  return vscode.workspace.workspaceFolders?.[0];
}

function skillzAgentConfig(resource?: vscode.Uri): vscode.WorkspaceConfiguration {
  return resource
    ? vscode.workspace.getConfiguration('skillzAgent', resource)
    : vscode.workspace.getConfiguration('skillzAgent');
}

function backendScriptSettingSnapshot(config: vscode.WorkspaceConfiguration): {
  value: string;
  defaultValue: string;
  globalValue: string;
  workspaceValue: string;
  workspaceFolderValue: string;
  selectedValue: string;
  selectedSource: string;
} {
  const inspection = config.inspect<string>('backendScript');
  const value = String(config.get<string>('backendScript') || '');
  const defaultValue = String(inspection?.defaultValue || '');
  const globalValue = String(inspection?.globalValue || '');
  const workspaceValue = String(inspection?.workspaceValue || '');
  const workspaceFolderValue = String(inspection?.workspaceFolderValue || '');
  const selectedValue = String(preferredBackendScriptValue({
    value,
    defaultValue,
    globalValue,
    workspaceValue,
    workspaceFolderValue,
  }) || '');
  const selectedSource = globalValue.trim()
    ? 'global'
    : workspaceValue.trim()
      ? 'workspace'
      : workspaceFolderValue.trim()
        ? 'workspace-folder'
        : value.trim() && value.trim() !== defaultValue.trim()
          ? 'effective'
          : 'default';
  return {
    value,
    defaultValue,
    globalValue,
    workspaceValue,
    workspaceFolderValue,
    selectedValue,
    selectedSource,
  };
}

function readLegacyBackendScriptSetting(filePath: string): string {
  try {
    if (!fs.existsSync(filePath)) {
      return '';
    }
    const text = fs.readFileSync(filePath, 'utf8');
    return String(extractJsonLikeStringSetting(text, 'skillzAgent.backendScript') || '').trim();
  } catch {
    return '';
  }
}

class skillzAgentBridge implements vscode.Disposable {
  private process: ChildProcessWithoutNullStreams | undefined;
  private pending = new Map<string, { resolve: (value: BridgeResponse) => void; reject: (reason?: unknown) => void }>();
  private buffer = '';
  private currentState: BridgeState = { planner: {}, transcript: [] };
  private launchedBackendInfo: BackendInfo | undefined;
  private stopping = false;
  private readonly disposables: vscode.Disposable[] = [];
  private readonly stateEmitter = new vscode.EventEmitter<BridgeState>();
  private readonly progressEmitter = new vscode.EventEmitter<ProgressMessage>();

  public readonly onDidUpdateState = this.stateEmitter.event;
  public readonly onDidProgress = this.progressEmitter.event;

  public constructor(private readonly context: vscode.ExtensionContext) {}

  public getState(): BridgeState {
    return this.currentState;
  }

  public isRunning(): boolean {
    return !!this.process;
  }

  public async syncConfiguration(): Promise<void> {
    await this.migrateLegacyBackendScriptSetting();
  }

  private async migrateLegacyBackendScriptSetting(): Promise<void> {
    // Migration removed — writing backendScript to global User Settings caused
    // the global value to permanently override workspace .vscode/settings.json,
    // making per-workspace runtime switching impossible.
  }

  public getBackendInfo(): BackendInfo {
    const workspaceFolder = primaryWorkspaceFolder();
    const config = skillzAgentConfig(workspaceFolder?.uri);
    const repoRoot = path.resolve(this.context.extensionPath, '..');
    const backendScriptSettings = backendScriptSettingSnapshot(config);
    const configured = describeBackendScript(repoRoot, backendScriptSettings.selectedValue);
    const launched = this.launchedBackendInfo;
    const launchedPath = String(launched?.launched_script_path || configured.resolvedPath);
    return {
      launched_script_path: launchedPath,
      launched_script_name: String(launched?.launched_script_name || configured.scriptName),
      launched_runtime_key: String(launched?.launched_runtime_key || configured.runtimeKey),
      launched_runtime_label: String(launched?.launched_runtime_label || configured.runtimeLabel),
      configured_script_path: configured.resolvedPath,
      configured_script_name: configured.scriptName,
      configured_runtime_key: configured.runtimeKey,
      configured_runtime_label: configured.runtimeLabel,
      mismatch: launchedPath !== configured.resolvedPath,
      workspace_folder_path: workspaceFolder?.uri.fsPath || '',
      backend_script_effective_value: backendScriptSettings.value,
      backend_script_default_value: backendScriptSettings.defaultValue,
      backend_script_global_value: backendScriptSettings.globalValue,
      backend_script_workspace_value: backendScriptSettings.workspaceValue,
      backend_script_workspace_folder_value: backendScriptSettings.workspaceFolderValue,
      backend_script_selected_source: backendScriptSettings.selectedSource,
    };
  }

  public async ensureStarted(): Promise<void> {
    if (this.process) {
      return;
    }

    this.stopping = false;

    await this.migrateLegacyBackendScriptSetting();

    const workspaceFolder = primaryWorkspaceFolder();
    if (!workspaceFolder) {
      throw new Error('Open a workspace folder before starting the Python Agent extension.');
    }

    const config = skillzAgentConfig(workspaceFolder.uri);
    const repoRoot = path.resolve(this.context.extensionPath, '..');
    const pythonExecutable = this.resolvePythonExecutable(config, repoRoot);
    const backendScript = describeBackendScript(repoRoot, backendScriptSettingSnapshot(config).selectedValue);
    const mainScript = backendScript.resolvedPath;
    const toolScript = path.join(repoRoot, 'agent_tools.py');
    const provider = String(config.get<string>('provider') || 'gemini').trim();
    const model = String(config.get<string>('model') || defaultModelForProvider(provider)).trim() || defaultModelForProvider(provider);
    this.launchedBackendInfo = {
      launched_script_path: backendScript.resolvedPath,
      launched_script_name: backendScript.scriptName,
      launched_runtime_key: backendScript.runtimeKey,
      launched_runtime_label: backendScript.runtimeLabel,
    };

    this.process = spawn(
      pythonExecutable,
      [
        mainScript,
        '--provider', provider,
        '--model', model,
        '--root', workspaceFolder.uri.fsPath,
        '--tools', toolScript,
        '--extension-bridge'
      ],
      {
        cwd: repoRoot,
        env: process.env,
      }
    );

    this.process.stdout.setEncoding('utf8');
    this.process.stdout.on('data', (chunk: string) => this.handleStdout(chunk));
    this.process.stderr.setEncoding('utf8');
    this.process.stderr.on('data', (chunk: string) => {
      if (!this.stopping) {
        void vscode.window.showWarningMessage(`Python Agent backend stderr: ${chunk.trim()}`);
      }
    });
    this.process.on('exit', (code, signal) => {
      const message = `Python Agent backend exited${code !== null ? ` with code ${code}` : ''}${signal ? ` (${signal})` : ''}.`;
      const intentionalStop = this.stopping || signal === 'SIGTERM' || signal === 'SIGKILL';
      this.stopping = false;
      this.process = undefined;
      this.launchedBackendInfo = undefined;
      this.buffer = '';
      for (const { reject } of this.pending.values()) {
        reject(new Error(message));
      }
      this.pending.clear();
      if (!intentionalStop) {
        void vscode.window.showWarningMessage(message);
      }
    });

    const init = await this.request('initialize', {});
    this.currentState = init.state;
    this.stateEmitter.fire(this.currentState);
  }

  public async submit(text: string): Promise<BridgeResponse> {
    await this.ensureStarted();
    return this.request('submit', { text });
  }

  public async plannerAction(action: string, extras: JsonMap = {}): Promise<BridgeResponse> {
    await this.ensureStarted();
    return this.request('planner_action', { action, ...extras });
  }

  public async reconfigureRuntime(provider: string, model: string): Promise<BridgeResponse> {
    await this.ensureStarted();
    return this.request('reconfigure_runtime', { provider, model });
  }

  public async configureBackoff(enabled: boolean, tokenLimitK: number): Promise<BridgeResponse> {
    await this.ensureStarted();
    return this.request('configure_backoff', { enabled, token_limit_k: tokenLimitK });
  }

  public async getRuntimeOptions(): Promise<RuntimeOptionsPayload | undefined> {
    await this.ensureStarted();
    const response = await this.request('runtime_options', {}) as RuntimeOptionsResponse;
    return response.runtime_options;
  }

  public async workerAction(action: JsonMap): Promise<BridgeResponse> {
    await this.ensureStarted();
    return this.request('worker_action', { action });
  }

  public async stopBackend(): Promise<void> {
    if (!this.process) {
      this.currentState = { planner: {}, transcript: [] };
      this.stateEmitter.fire(this.currentState);
      return;
    }
    this.stopping = true;
    this.process.kill();
    this.process = undefined;
    this.launchedBackendInfo = undefined;
    this.buffer = '';
    this.currentState = { planner: {}, transcript: [] };
    this.stateEmitter.fire(this.currentState);
    for (const { reject } of this.pending.values()) {
      reject(new Error('Python Agent backend stopped.'));
    }
    this.pending.clear();
  }

  private request(type: string, payload: JsonMap): Promise<BridgeResponse> {
    if (!this.process) {
      return Promise.reject(new Error('Python Agent backend is not running.'));
    }

    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const request: JsonMap = { id, type, ...payload };
    return new Promise<BridgeResponse>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.process?.stdin.write(JSON.stringify(request) + '\n');
    });
  }

  private handleStdout(chunk: string): void {
    this.buffer += chunk;
    while (true) {
      const newlineIndex = this.buffer.indexOf('\n');
      if (newlineIndex < 0) {
        break;
      }
      const line = this.buffer.slice(0, newlineIndex).trim();
      this.buffer = this.buffer.slice(newlineIndex + 1);
      if (!line) {
        continue;
      }
      try {
        const parsed = JSON.parse(line) as JsonMap;

        // Handle progress messages (no id, type === 'progress')
        if (parsed.type === 'progress' || parsed.type === 'goal_start' || parsed.type === 'goal_finish') {
          const progress = parsed as unknown as ProgressMessage;
          if (progress.state) {
            this.currentState = progress.state;
            this.stateEmitter.fire(this.currentState);
          }
          this.progressEmitter.fire(progress);
          continue;
        }

        const response = parsed as unknown as BridgeResponse;
        this.currentState = response.state;
        this.stateEmitter.fire(this.currentState);
        const id = response.id;
        if (id && this.pending.has(id)) {
          const deferred = this.pending.get(id)!;
          this.pending.delete(id);
          deferred.resolve(response);
        }
      } catch (error) {
        void vscode.window.showErrorMessage(`Could not parse Python Agent bridge response: ${String(error)}`);
      }
    }
  }

  private resolvePythonExecutable(config: vscode.WorkspaceConfiguration, repoRoot: string): string {
    const configured = String(config.get<string>('pythonPath') || '').trim();
    if (configured) {
      return configured;
    }

    const venvPython = path.join(repoRoot, '.venv', 'bin', 'python');
    if (fs.existsSync(venvPython)) {
      return venvPython;
    }

    return process.platform === 'win32' ? 'python' : 'python3';
  }

  public dispose(): void {
    this.stateEmitter.dispose();
    this.progressEmitter.dispose();
    for (const disposable of this.disposables) {
      disposable.dispose();
    }
    void this.stopBackend();
  }
}

let suppressConfigDrivenRuntimeUpdateCount = 0;

function workspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function workspaceFile(relativePath: string): vscode.Uri | undefined {
  const root = workspaceRoot();
  if (!root || !relativePath) {
    return undefined;
  }
  return vscode.Uri.file(path.join(root, relativePath));
}

function inferLanguageFromPath(targetPath: string): string {
  const extension = path.extname(targetPath).toLowerCase();
  if (extension === '.py') {
    return 'python';
  }
  if (extension === '.ts' || extension === '.tsx') {
    return 'typescript';
  }
  if (extension === '.js' || extension === '.jsx' || extension === '.mjs' || extension === '.cjs') {
    return 'javascript';
  }
  if (extension === '.md') {
    return 'markdown';
  }
  if (extension === '.json') {
    return 'json';
  }
  if (extension === '.diff' || extension === '.patch') {
    return 'diff';
  }
  return 'plaintext';
}

function toDiagnostic(item: DiagnosticItem): vscode.Diagnostic | undefined {
  const message = String(item.message || '').trim();
  if (!message) {
    return undefined;
  }
  const line = Math.max(0, Number(item.line || 1) - 1);
  const column = Math.max(0, Number(item.column || 1) - 1);
  const range = new vscode.Range(line, column, line, column + 1);
  const diagnostic = new vscode.Diagnostic(range, message, vscode.DiagnosticSeverity.Error);
  const code = String(item.code || '').trim();
  if (code) {
    diagnostic.code = code;
  }
  diagnostic.source = 'python-agent';
  return diagnostic;
}

function syncDiagnosticsCollection(state: BridgeState, collection: vscode.DiagnosticCollection): void {
  collection.clear();
  const grouped = groupLatestDiagnosticsByPath(state);
  for (const [relativePath, items] of Object.entries(grouped)) {
    const uri = workspaceFile(relativePath);
    if (!uri) {
      continue;
    }
    const diagnosticsForUri = items.map(toDiagnostic).filter((item): item is vscode.Diagnostic => Boolean(item));
    if (diagnosticsForUri.length) {
      collection.set(uri, diagnosticsForUri);
    }
  }
}

async function openPathAtLocation(relativePath: string, line?: number, column?: number): Promise<void> {
  const target = workspaceFile(relativePath);
  if (!target) {
    return;
  }
  const document = await vscode.workspace.openTextDocument(target);
  const editor = await vscode.window.showTextDocument(document, { preview: false });
  if (typeof line === 'number' && line > 0) {
    const targetLine = Math.max(0, line - 1);
    const targetColumn = Math.max(0, (column || 1) - 1);
    const position = new vscode.Position(targetLine, targetColumn);
    editor.selection = new vscode.Selection(position, position);
    editor.revealRange(new vscode.Range(position, position), vscode.TextEditorRevealType.InCenter);
  }
}

async function openReportDocument(title: string, content: string, language: string): Promise<void> {
  const document = await vscode.workspace.openTextDocument({ content, language });
  await vscode.window.showTextDocument(document, { preview: false });
}

async function readGitHeadContent(relativePath: string): Promise<string> {
  const root = workspaceRoot();
  if (!root) {
    throw new Error('Open a workspace folder before opening file diffs.');
  }
  return new Promise<string>((resolve, reject) => {
    execFile('git', ['show', `HEAD:${relativePath}`], { cwd: root, maxBuffer: 1024 * 1024 }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(stderr.trim() || error.message));
        return;
      }
      resolve(stdout);
    });
  });
}

async function openFileDiff(relativePath: string): Promise<void> {
  const target = workspaceFile(relativePath);
  if (!target) {
    return;
  }
  try {
    const headContent = await readGitHeadContent(relativePath);
    const language = inferLanguageFromPath(relativePath);
    const left = await vscode.workspace.openTextDocument({ content: headContent, language });
    const title = `${relativePath} (HEAD ↔ Working Tree)`;
    await vscode.commands.executeCommand('vscode.diff', left.uri, target, title);
  } catch (error) {
    await vscode.window.showWarningMessage(`Could not open git diff for ${relativePath}: ${String(error)}`);
    await openPathAtLocation(relativePath);
  }
}

class AgentPanel implements vscode.Disposable {
  private panel: vscode.WebviewPanel | undefined;
  private readonly disposables: vscode.Disposable[] = [];

  public constructor(private readonly context: vscode.ExtensionContext, private readonly bridge: skillzAgentBridge) {
    this.disposables.push(
      this.bridge.onDidUpdateState((state) => {
        if (this.panel) {
          void this.panel.webview.postMessage({ type: 'state', state });
          void this.panel.webview.postMessage({ type: 'backendInfo', info: this.bridge.getBackendInfo() });
        }
      })
    );
    this.disposables.push(
      this.bridge.onDidProgress((progress) => {
        if (this.panel) {
          void this.panel.webview.postMessage({
            type: progress.type || 'progress',
            domain: progress.domain || '',
            step: progress.step ?? 0,
            action_type: progress.action_type || '',
            skill_name: (progress as JsonMap).skill_name || '',
            skill_mode: (progress as JsonMap).skill_mode || '',
            skill_count: (progress as JsonMap).skill_count ?? 0,
            path: progress.path || '',
            ok: progress.ok ?? true,
            elapsed_s: progress.elapsed_s ?? 0,
            thought: progress.thought || '',
            summary: progress.summary || '',
            diff: progress.diff || '',
            replacements: progress.replacements ?? 0,
            added_lines: progress.added_lines ?? 0,
            removed_lines: progress.removed_lines ?? 0,
            search_excerpt: progress.search_excerpt || '',
            replace_excerpt: progress.replace_excerpt || '',
            inspected_file_count: progress.inspected_file_count ?? 0,
            inspected_files: progress.inspected_files || [],
            goal_index: progress.goal_index ?? -1,
            goal_id: progress.goal_id || '',
            goal_title: progress.goal_title || '',
            state: progress.state,
          });
        }
      })
    );
  }

  public async show(): Promise<void> {
    if (!this.panel) {
      this.panel = vscode.window.createWebviewPanel(
        'skillzAgent.panel',
        'Python Agent',
        vscode.ViewColumn.Beside,
        {
          enableScripts: true,
          retainContextWhenHidden: true,
        }
      );
      this.panel.onDidDispose(() => {
        this.panel = undefined;
        void this.bridge.stopBackend();
      }, undefined, this.disposables);
      this.panel.webview.onDidReceiveMessage(async (message: JsonMap) => {
          try {
            await this.handleWebviewMessage(message);
          } catch (error) {
            const details = String(error instanceof Error ? error.message : error);
            void vscode.window.showErrorMessage(`Python Agent action failed: ${details}`);
            await this.panel?.webview.postMessage({ type: 'actionStatus', level: 'error', message: details });
          }
      }, undefined, this.disposables);
      this.panel.webview.html = this.renderHtml(this.panel.webview);
    } else {
      this.panel.reveal(vscode.ViewColumn.Beside);
    }

    await this.bridge.ensureStarted();
    await this.panel.webview.postMessage({ type: 'state', state: this.bridge.getState() });
    await this.panel.webview.postMessage({ type: 'backendInfo', info: this.bridge.getBackendInfo() });
    const runtimeOptions = await this.bridge.getRuntimeOptions();
    if (runtimeOptions) {
      await this.panel.webview.postMessage({ type: 'runtimeOptions', options: runtimeOptions });
    }
  }

  public async syncBackendInfo(): Promise<void> {
    if (!this.panel) {
      return;
    }
    await this.panel.webview.postMessage({ type: 'backendInfo', info: this.bridge.getBackendInfo() });
  }

  public isOpen(): boolean {
    return Boolean(this.panel);
  }

  public async reconnectBackend(): Promise<void> {
    if (!this.panel) {
      return;
    }
    await this.bridge.ensureStarted();
    await this.panel.webview.postMessage({ type: 'state', state: this.bridge.getState() });
    await this.panel.webview.postMessage({ type: 'backendInfo', info: this.bridge.getBackendInfo() });
    const runtimeOptions = await this.bridge.getRuntimeOptions();
    if (runtimeOptions) {
      await this.panel.webview.postMessage({ type: 'runtimeOptions', options: runtimeOptions });
    }
  }

  private async handleWebviewMessage(message: JsonMap): Promise<void> {
    const type = String(message.type || '');
    if (type === 'submitPrompt') {
      const text = String(message.text || '').trim();
      if (!text) {
        return;
      }
      const response = await this.bridge.submit(text);
      if (!response.ok) {
        throw new Error(String(response.message || 'Planner submit failed.'));
      }
      await this.panel?.webview.postMessage({ type: 'state', state: response.state });
      return;
    }

    if (type === 'closeIssue') {
      const issueId = String(message.issue_id || '').trim();
      if (!issueId) {
        return;
      }
      try {
        const response = await this.bridge.submit(`/close-issue ${issueId}`);
        await this.panel?.webview.postMessage({ type: 'state', state: response.state });
      } catch (error) {
        const details = String(error instanceof Error ? error.message : error);
        await this.panel?.webview.postMessage({ type: 'actionStatus', level: 'error', message: details });
      }
      return;
    }

    if (type === 'startContinuous' || type === 'start_continuous') {
      const maxCycles = Math.max(1, Math.floor(Number(message.maxCycles || 1)));
      await this.panel?.webview.postMessage({
        type: 'progress',
        step: 0,
        action_type: 'continuous_start_sent_ui',
        path: '',
        ok: true,
        elapsed_s: 0,
        thought: `Continuous mode start sent (${maxCycles} cycle${maxCycles === 1 ? '' : 's'}).`,
        summary: 'Extension host forwarding continuous mode start.',
        diff: '',
        replacements: 0,
        added_lines: 0,
        removed_lines: 0,
        search_excerpt: '',
        replace_excerpt: '',
        inspected_file_count: 0,
        inspected_files: [],
        state: this.bridge.getState(),
      });
      const response = await this.bridge.plannerAction('start_continuous', { max_cycles: maxCycles });
      if (!response.ok) {
        throw new Error(String(response.message || 'Continuous mode failed to start.'));
      }
      await this.panel?.webview.postMessage({ type: 'state', state: response.state });
      return;
    }

    if (type === 'stopContinuous' || type === 'stop_continuous') {
      const response = await this.bridge.plannerAction('stop_continuous', {});
      if (!response.ok) {
        throw new Error(String(response.message || 'Continuous mode failed to stop.'));
      }
      await this.panel?.webview.postMessage({ type: 'state', state: response.state });
      return;
    }

    if (type === 'plannerAction') {
      const action = String(message.action || '').trim();
      const extras = typeof message.payload === 'object' && message.payload !== null ? { ...(message.payload as JsonMap) } : {};
      if (typeof message.mode === 'string' && message.mode.trim() && !('mode' in extras)) {
        extras.mode = message.mode.trim();
      }
      await this.panel?.webview.postMessage({
        type: 'progress',
        step: 0,
        action_type: 'planner_action_sent_ui',
        path: '',
        ok: true,
        elapsed_s: 0,
        thought: `Planner action sent: ${action}`,
        summary: `Extension host forwarding planner action${extras.mode ? ` (mode=${String(extras.mode)})` : ''}.`,
        diff: '',
        replacements: 0,
        added_lines: 0,
        removed_lines: 0,
        search_excerpt: '',
        replace_excerpt: '',
        inspected_file_count: 0,
        inspected_files: [],
        state: this.bridge.getState(),
      });
      const response = await this.bridge.plannerAction(action, extras);
      if (!response.ok) {
        throw new Error(String(response.message || `Planner action failed: ${action}`));
      }
      await this.panel?.webview.postMessage({ type: 'state', state: response.state });
      return;
    }

    if (type === 'workerAction') {
      const action = typeof message.action === 'object' && message.action !== null ? message.action as JsonMap : null;
      if (!action) {
        return;
      }
      const actionType = String(action.type || '').trim();
      if (actionType === 'drop_context') {
        const confirmed = await vscode.window.showWarningMessage(
          'Drop worker context and clear active facts for the current task?',
          { modal: true },
          'Drop Context'
        );
        if (confirmed !== 'Drop Context') {
          return;
        }
      }
      const response = await this.bridge.workerAction(action);
      if (!response.ok) {
        throw new Error(String(response.message || `Worker action failed: ${String(action.type || 'unknown')}`));
      }
      await this.panel?.webview.postMessage({ type: 'state', state: response.state });
      return;
    }

    if (type === 'runtimeSwitch') {
      const provider = String(message.provider || '').trim().toLowerCase();
      const model = String(message.model || '').trim();
      if (!provider || !model) {
        return;
      }
      try {
        const response = await this.bridge.reconfigureRuntime(provider, model);
        if (!response.ok) {
          throw new Error(response.message || 'Unknown runtime update failure');
        }
        suppressConfigDrivenRuntimeUpdateCount = 2;
        const config = skillzAgentConfig(primaryWorkspaceFolder()?.uri);
        await config.update('provider', provider, vscode.ConfigurationTarget.Workspace);
        await config.update('model', model, vscode.ConfigurationTarget.Workspace);
        await this.panel?.webview.postMessage({ type: 'state', state: response.state });
        const runtimeOptions = await this.bridge.getRuntimeOptions();
        if (runtimeOptions) {
          await this.panel?.webview.postMessage({ type: 'runtimeOptions', options: runtimeOptions });
        }
        void vscode.window.showInformationMessage(`Python Agent runtime updated to ${provider} / ${model}.`);
      } catch (error) {
        await this.panel?.webview.postMessage({ type: 'runtimeStatus', level: 'error', message: String(error) });
        throw error;
      }
      return;
    }

    if (type === 'configureBackoff') {
      const enabled = Boolean(message.enabled);
      const tokenLimitK = Math.max(0, parseInt(String(message.tokenLimitK || '0'), 10));
      try {
        const response = await this.bridge.configureBackoff(enabled, tokenLimitK);
        await this.panel?.webview.postMessage({ type: 'backoffStatus', backoff: response.backoff ?? { enabled, token_limit_k: tokenLimitK, window_tokens_used: 0 } });
      } catch (error) {
        await this.panel?.webview.postMessage({ type: 'runtimeStatus', level: 'error', message: `Backoff config failed: ${error}` });
      }
      return;
    }

    if (type === 'switchBackend') {
      const target = String(message.target || '').trim();
      if (target !== 'stable' && target !== 'beta') {
        return;
      }
      const repoRoot = path.resolve(this.context.extensionPath, '..');
      const scriptName = target === 'stable' ? 'main.py' : 'live_test_loop.py';
      const scriptPath = path.join(repoRoot, scriptName);
      const config = skillzAgentConfig(primaryWorkspaceFolder()?.uri);
      // application-scoped settings can only be written to Global (User) settings.
      // Writing this setting will fire onDidChangeConfiguration, which handles the restart.
      await config.update('backendScript', scriptPath, vscode.ConfigurationTarget.Global);
      void vscode.window.showInformationMessage(`Python Agent switching to ${target === 'stable' ? 'Stable (main.py)' : 'Beta (live_test_loop.py)'}...`);
      return;
    }

    if (type === 'openPath') {
      const relativePath = String(message.path || '').trim();
      if (!relativePath) {
        return;
      }
      await openPathAtLocation(relativePath, Number(message.line || 0), Number(message.column || 0));
      return;
    }

    if (type === 'openReport') {
      const title = String(message.title || '').trim();
      const content = String(message.content || '');
      const language = String(message.language || 'plaintext');
      if (!title || !content.trim()) {
        return;
      }
      await openReportDocument(title, content, language);
      return;
    }

    if (type === 'openFileDiff') {
      const relativePath = String(message.path || '').trim();
      if (!relativePath) {
        return;
      }
      await openFileDiff(relativePath);
    }
  }

  private renderHtml(webview: vscode.Webview): string {
    const initialState = JSON.stringify(this.bridge.getState()).replace(/</g, '\\u003c');
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Python Agent</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: var(--vscode-editor-background);
      --panel: var(--vscode-sideBar-background);
      --border: var(--vscode-panel-border);
      --text: var(--vscode-editor-foreground);
      --muted: var(--vscode-descriptionForeground);
      --accent: var(--vscode-button-background);
      --accent-text: var(--vscode-button-foreground);
      --accent-hover: var(--vscode-button-hoverBackground, var(--accent));
      --danger: var(--vscode-inputValidation-errorBorder, #b00020);
      --warning: var(--vscode-inputValidation-warningBorder, #c18616);
      --surface: color-mix(in srgb, var(--panel) 60%, var(--bg));
      --divider: color-mix(in srgb, var(--border) 50%, transparent);
      --radius-sm: 5px;
      --radius-md: 8px;
      --radius-lg: 12px;
    }
    * { box-sizing: border-box; }
    body {
      font-family: var(--vscode-font-family);
      background: var(--bg);
      color: var(--text);
      margin: 0;
      display: flex;
      flex-direction: column;
      height: 100vh;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }

    /* ── Header ── */
    .header {
      padding: 10px 16px;
      border-bottom: 1px solid var(--divider);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-shrink: 0;
      backdrop-filter: blur(8px);
      background: color-mix(in srgb, var(--bg) 85%, transparent);
    }
    .header h1 { margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.01em; opacity: 0.9; }
    .header-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }

    /* ── Feed ── */
    .feed { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 8px; scroll-behavior: smooth; }

    /* ── Input Area ── */
    .input-area { border-top: 1px solid var(--divider); padding: 12px 16px; flex-shrink: 0; display: grid; gap: 10px; background: color-mix(in srgb, var(--bg) 90%, transparent); backdrop-filter: blur(8px); }

    /* ── Runtime Picker ── */
    .runtime-picker { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .runtime-field { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); white-space: nowrap; }
    .runtime-field span { font-weight: 600; font-size: 10px; letter-spacing: 0.04em; text-transform: uppercase; opacity: 0.7; }
    .runtime-field select, .runtime-field input {
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--divider);
      border-radius: var(--radius-sm);
      padding: 4px 8px;
      font: inherit;
      font-size: 12px;
      transition: border-color 0.15s ease;
    }
    .runtime-field select:focus, .runtime-field input:focus { border-color: var(--accent); outline: none; }
    .runtime-field select { min-width: 120px; }
    .runtime-field.runtime-model-field select { min-width: 180px; }
    .runtime-field.runtime-custom-model { display: none; }
    .runtime-field.runtime-custom-model.visible { display: flex; }
    .runtime-field.runtime-custom-model input { min-width: 160px; }

    /* ── Backoff Controls ── */
    .backoff-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 11px; color: var(--muted); padding-top: 6px; border-top: 1px solid var(--divider); }
    .backoff-toggle { display: flex; align-items: center; gap: 4px; cursor: pointer; white-space: nowrap; }
    .backoff-toggle input[type=checkbox] { accent-color: var(--accent); width: 13px; height: 13px; margin: 0; cursor: pointer; }
    .backoff-toggle span { font-weight: 600; font-size: 10px; letter-spacing: 0.04em; text-transform: uppercase; opacity: 0.7; }
    .backoff-limit { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
    .backoff-limit input[type=number] {
      width: 50px;
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--divider);
      border-radius: var(--radius-sm);
      padding: 3px 5px;
      font: inherit;
      font-size: 12px;
      text-align: right;
      transition: border-color 0.15s ease;
    }
    .backoff-limit input[type=number]:focus { border-color: var(--accent); outline: none; }
    .backoff-limit input[type=number]:disabled { opacity: 0.35; }
    .backoff-limit span { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.02em; opacity: 0.7; }
    .backoff-window { font-size: 10px; margin-left: auto; opacity: 0.5; }
    .runtime-sep { width: 1px; height: 16px; background: var(--divider); flex-shrink: 0; }
    .runtime-tab-card { display: flex; flex-direction: column; gap: 8px; }
    .runtime-tab-card[hidden] { display: none; }
    .runtime-status { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: var(--muted); margin-left: auto; white-space: nowrap; }
    .runtime-status:empty { display: none; }
    .runtime-status.loading::before { content: ''; width: 10px; height: 10px; border-radius: 50%; border: 1.5px solid var(--muted); border-top-color: transparent; animation: spin 0.8s linear infinite; }
    .runtime-status.success { color: #4e9a06; }
    .runtime-status.error { color: var(--danger); }

    /* ── Lifecycle Dock ── */
    .lifecycle-dock { display: none; gap: 10px; grid-template-rows: auto minmax(248px, 248px); }
    .lifecycle-dock.visible { display: grid; }
    .dock-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; min-width: 0; }
    .dock-tabs { display: flex; gap: 2px; flex-wrap: wrap; }
    .dock-actions { display: flex; align-items: center; gap: 6px; margin-left: auto; }
    .dock-actions input {
      width: 54px;
      min-width: 0;
      padding: 4px 6px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--divider);
      background: var(--input-bg);
      color: var(--text);
      font-size: 12px;
    }
    .dock-actions button { font-size: 12px; padding: 5px 10px; }
    .dock-tab {
      border-radius: var(--radius-sm);
      padding: 5px 12px;
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.01em;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: background 0.15s ease, color 0.15s ease;
    }
    .dock-tab:hover { background: color-mix(in srgb, var(--text) 6%, transparent); }
    .dock-tab-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      height: 18px;
      padding: 0 5px;
      border-radius: var(--radius-sm);
      font-size: 10px;
      font-weight: 700;
      background: color-mix(in srgb, var(--accent) 14%, transparent);
      color: var(--accent);
    }
    .dock-tab.active .dock-tab-badge { background: color-mix(in srgb, var(--accent-text) 14%, transparent); color: var(--accent-text); }
    .dock-panels { min-height: 0; height: 248px; }
    .dock-panel { display: none; height: 100%; overflow-wrap: break-word; word-break: break-word; }
    .dock-panel.active { display: block; }

    /* ── Form ── */
    form { display: flex; gap: 8px; align-items: flex-end; }
    textarea {
      flex: 1;
      min-height: 40px;
      max-height: 120px;
      resize: vertical;
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--divider);
      border-radius: var(--radius-md);
      padding: 9px 12px;
      font: inherit;
      font-size: 13px;
      line-height: 1.45;
      transition: border-color 0.15s ease;
    }
    textarea:focus { border-color: var(--accent); outline: none; }
    textarea::placeholder { color: var(--muted); opacity: 0.6; }

    /* ── Buttons ── */
    button {
      border: 1px solid var(--divider);
      background: var(--surface);
      color: var(--text);
      border-radius: var(--radius-md);
      padding: 8px 14px;
      cursor: pointer;
      font: inherit;
      font-size: 13px;
      font-weight: 500;
      white-space: nowrap;
      transition: background 0.12s ease, border-color 0.12s ease, opacity 0.12s ease;
    }
    button:hover { background: color-mix(in srgb, var(--text) 8%, var(--surface)); }
    button:active { opacity: 0.85; }
    button.primary {
      background: var(--accent);
      color: var(--accent-text);
      border-color: transparent;
      font-weight: 600;
    }
    button.primary:hover { background: var(--accent-hover); }
    button.ghost { background: transparent; border-color: transparent; opacity: 0.7; }
    button.ghost:hover { opacity: 1; background: color-mix(in srgb, var(--text) 6%, transparent); }
    button:disabled { opacity: 0.4; cursor: default; pointer-events: none; }
    button.loading { position: relative; color: transparent; }
    button.loading::after {
      content: '';
      position: absolute;
      inset: 0;
      margin: auto;
      width: 14px;
      height: 14px;
      border: 1.5px solid var(--muted);
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* ── Typography helpers ── */
    .muted { color: var(--muted); }
    code { font-family: var(--vscode-editor-font-family); }
    pre { white-space: pre-wrap; margin: 0; font-family: var(--vscode-editor-font-family); font-size: 12px; }

    /* ── Badges ── */
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: var(--radius-sm);
      padding: 2px 8px;
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.01em;
    }
    .badge.muted-badge { background: color-mix(in srgb, var(--text) 6%, transparent); color: var(--muted); }

    /* ── Chat Bubbles ── */
    .bubble {
      padding: 10px 14px;
      border-radius: var(--radius-lg);
      white-space: pre-wrap;
      font-size: 13px;
      line-height: 1.5;
      border: 1px solid transparent;
    }
    .bubble.user {
      background: color-mix(in srgb, var(--accent) 10%, transparent);
      border-color: color-mix(in srgb, var(--accent) 12%, transparent);
    }
    .bubble.assistant { background: transparent; border-color: var(--divider); }
    .bubble summary { cursor: pointer; font-weight: 600; }
    .bubble-details-body { margin-top: 8px; color: var(--muted); }

    /* ── Flow Cards ── */
    .flow-card {
      padding: 12px 14px;
      border-radius: var(--radius-md);
      border: 1px solid var(--divider);
      background: var(--surface);
      font-size: 13px;
    }
    .dock-panel .flow-card { height: 100%; overflow: auto; }
    .flow-card.error-card { border-color: color-mix(in srgb, var(--danger) 40%, var(--divider)); }
    .flow-card.warning-card { border-color: color-mix(in srgb, var(--warning) 40%, var(--divider)); }
    .flow-title { font-weight: 600; margin-bottom: 6px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); opacity: 0.8; }
    .flow-body { margin-bottom: 6px; line-height: 1.5; }
    .flow-meta { color: var(--muted); font-size: 12px; opacity: 0.8; }
    .flow-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
    .flow-actions button { font-size: 12px; padding: 5px 12px; font-weight: 500; }
    .flow-actions input {
      width: 58px;
      min-width: 0;
      padding: 4px 6px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--divider);
      background: var(--input-bg);
      color: var(--text);
      font-size: 12px;
    }
    .compact-list { display: grid; gap: 3px; margin-top: 8px; }
    .compact-item {
      padding: 7px 10px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--divider);
      background: var(--bg);
      font-size: 12px;
      line-height: 1.45;
    }
    .path { color: var(--vscode-textLink-foreground); cursor: pointer; }
    .path:hover { text-decoration: underline; }
    .error { border-left: 2px solid var(--danger); padding-left: 10px; }
    .warning { border-left: 2px solid var(--warning); padding-left: 10px; }
    .inline-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .inline-actions button { font-size: 12px; padding: 4px 10px; }

    /* ── Empty State ── */
    .empty-state { text-align: center; padding: 64px 24px; color: var(--muted); }
    .empty-state h2 { font-size: 15px; font-weight: 600; margin: 0 0 6px; color: var(--text); opacity: 0.85; }
    .empty-state p { margin: 0; font-size: 13px; opacity: 0.7; }

    /* ── Step Items (Timeline) ── */
    .step-item {
      padding: 8px 12px;
      border-radius: var(--radius-md);
      font-size: 12px;
      background: var(--bg);
      border: 1px solid var(--divider);
      display: flex;
      gap: 8px;
      align-items: baseline;
      transition: border-color 0.15s ease;
    }
    .step-item.error { border-left: 2px solid var(--danger); }
    .step-icon { flex-shrink: 0; font-size: 11px; }
    .step-icon.ok { color: #4e9a06; }
    .step-icon.fail { color: var(--danger); }
    .step-detail { flex: 1; min-width: 0; }
    .step-action { font-weight: 600; }
    .step-path { color: var(--vscode-textLink-foreground); cursor: pointer; }
    .step-path:hover { text-decoration: underline; }
    .step-thought { color: var(--muted); font-style: italic; white-space: pre-wrap; overflow-wrap: break-word; word-break: break-word; opacity: 0.85; }
    .step-summary { color: var(--muted); font-size: 11px; white-space: pre-wrap; overflow-wrap: break-word; word-break: break-word; }
    .step-patch-meta { margin-top: 4px; font-size: 11px; color: var(--muted); opacity: 0.8; }
    .step-patch-pair { margin-top: 6px; display: grid; gap: 4px; }
    .step-patch-row { display: grid; gap: 2px; }
    .step-patch-label { font-size: 10px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; opacity: 0.7; }
    .step-patch-text {
      white-space: pre-wrap;
      overflow-wrap: break-word;
      word-break: break-word;
      font-family: var(--vscode-editor-font-family);
      font-size: 11px;
      color: var(--text);
      background: var(--bg);
      border: 1px solid var(--divider);
      border-radius: var(--radius-sm);
      padding: 6px 8px;
    }
    .step-patch-diff { margin-top: 6px; }
    .step-patch-diff summary { cursor: pointer; color: var(--muted); font-size: 11px; }
    .step-patch-diff pre { margin-top: 6px; white-space: pre-wrap; overflow-wrap: break-word; word-break: break-word; }
    .step-inspect-meta { margin-top: 4px; font-size: 11px; color: var(--muted); opacity: 0.8; }
    .step-inspect-list { margin-top: 6px; display: grid; gap: 4px; }
    .step-inspect-item {
      display: grid;
      gap: 2px;
      padding: 6px 10px;
      border-radius: var(--radius-sm);
      background: var(--bg);
      border: 1px solid var(--divider);
    }
    .step-inspect-item.error { border-left: 2px solid var(--danger); }
    .step-inspect-path { color: var(--vscode-textLink-foreground); cursor: pointer; font-weight: 600; overflow-wrap: break-word; word-break: break-word; }
    .step-inspect-path:hover { text-decoration: underline; }
    .step-inspect-range { color: var(--muted); font-size: 11px; }
    .step-inspect-error { color: var(--danger); font-size: 11px; overflow-wrap: break-word; word-break: break-word; }
    .step-time { color: var(--muted); font-size: 11px; flex-shrink: 0; opacity: 0.7; }

    /* ── Card Details ── */
    .card-details { margin-top: 8px; }
    .card-details summary { cursor: pointer; font-size: 12px; color: var(--muted); user-select: none; }
    .card-details summary:hover { color: var(--text); }
    .card-details-body { margin-top: 6px; font-size: 12px; color: var(--muted); white-space: pre-wrap; line-height: 1.55; }
    .discovery-body { white-space: pre-wrap; line-height: 1.6; }
    .muted-block { color: var(--muted); white-space: pre-wrap; line-height: 1.55; }

    /* ── Timeline ── */
    .timeline-host { margin-top: 10px; }
    .timeline-list { display: grid; gap: 6px; margin-top: 8px; }

    /* ── Facts ── */
    .facts-wrapper { max-height: 72px; overflow-y: auto; transition: max-height 0.2s ease; }
    .facts-wrapper.expanded { max-height: 320px; }
    .facts-toggle { cursor: pointer; user-select: none; display: flex; justify-content: space-between; align-items: center; }
    .facts-toggle::after { content: '▸ expand'; font-size: 10px; color: var(--muted); opacity: 0.6; }
    .facts-toggle.open::after { content: '▾ collapse'; }

    /* ── Goal Headers ── */
    .goal-header {
      padding: 10px 14px;
      border-radius: var(--radius-md);
      border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--divider));
      background: color-mix(in srgb, var(--accent) 4%, var(--bg));
      font-size: 13px;
      display: flex;
      gap: 10px;
      align-items: flex-start;
      min-width: 0;
    }
    .goal-header .goal-index {
      flex-shrink: 0;
      font-weight: 700;
      font-size: 10px;
      text-transform: uppercase;
      color: var(--accent);
      letter-spacing: 0.04em;
      opacity: 0.9;
    }
    .goal-header .goal-label { font-weight: 600; min-width: 0; overflow-wrap: break-word; word-break: break-word; }
    .goal-done {
      opacity: 0.55;
      border-color: color-mix(in srgb, #4e9a06 20%, var(--divider));
      background: color-mix(in srgb, #4e9a06 4%, var(--bg));
    }
    .goal-done .goal-index { color: #4e9a06; }

    /* ── Execution Card ── */
    .exec-card { border-left: 2px solid var(--accent); }
    .exec-progress { margin-top: 8px; height: 2px; border-radius: 1px; background: color-mix(in srgb, var(--accent) 12%, var(--bg)); overflow: hidden; }
    .exec-bar { height: 100%; background: var(--accent); border-radius: 1px; transition: width 0.4s ease; }

    /* ── Lifecycle Card ── */
    .lifecycle-card { border-left: 2px solid var(--accent); }
    .phase-row {
      display: flex;
      gap: 8px;
      align-items: baseline;
      padding: 5px 0;
      border-bottom: 1px solid color-mix(in srgb, var(--border) 25%, transparent);
    }
    .phase-row:last-child { border-bottom: none; }
    .phase-label { flex-shrink: 0; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; }
    .phase-label.done { color: #4e9a06; }
    .phase-label.active { color: var(--accent); }
    .phase-label.muted-phase { color: var(--muted); opacity: 0.6; }
    .phase-body { font-size: 12px; color: var(--muted); line-height: 1.45; }
    .phase-meta { font-size: 11px; color: var(--muted); margin-left: auto; flex-shrink: 0; opacity: 0.7; }
    .goal-item-done { opacity: 0.55; }
    .goal-item-active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 5%, var(--bg)); }

    /* ── Inline Status ── */
    .inline-status {
      padding: 10px 14px;
      border-radius: var(--radius-md);
      border: 1px solid var(--divider);
      background: var(--bg);
      font-size: 12px;
      display: flex;
      gap: 10px;
      align-items: flex-start;
    }
    .inline-status.waiting::before {
      content: '';
      width: 12px;
      height: 12px;
      border-radius: 50%;
      border: 1.5px solid var(--muted);
      border-top-color: transparent;
      flex-shrink: 0;
      margin-top: 2px;
      animation: spin 0.8s linear infinite;
    }
    .inline-status-note { white-space: pre-wrap; line-height: 1.5; }
    .inline-status-label { font-weight: 600; color: var(--text); margin-right: 6px; }

    /* ── Lifecycle Trace ── */
    .lifecycle-trace {
      padding: 10px 14px;
      border-radius: var(--radius-md);
      border: 1px solid var(--divider);
      border-left: 2px solid var(--accent);
      background: var(--bg);
      font-size: 12px;
      display: grid;
      gap: 4px;
    }
    .lifecycle-trace .trace-title { font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); opacity: 0.8; }
    .lifecycle-trace .trace-row { display: flex; gap: 6px; align-items: baseline; line-height: 1.45; }
    .lifecycle-trace .trace-label { flex-shrink: 0; font-weight: 600; font-size: 11px; min-width: 56px; }
    .lifecycle-trace .trace-value { color: var(--muted); overflow-wrap: break-word; word-break: break-word; min-width: 0; }
    .lifecycle-trace .trace-outcome { border-top: 1px solid var(--divider); padding-top: 4px; margin-top: 2px; color: var(--text); white-space: pre-wrap; line-height: 1.5; }
    .lifecycle-trace.operational { border-left-color: color-mix(in srgb, var(--warning) 60%, var(--accent)); }

    /* ── Runtime Debug ── */
    .runtime-debug { margin-top: 10px; border-top: 1px solid var(--divider); padding-top: 10px; }
    .runtime-debug details {
      border: 1px solid var(--divider);
      border-radius: var(--radius-md);
      padding: 10px 12px;
      background: var(--bg);
    }
    .runtime-debug summary { cursor: pointer; font-weight: 600; font-size: 12px; }
    .runtime-debug summary:hover { color: var(--accent); }
    .runtime-debug-meta { font-size: 11px; color: var(--muted); margin-top: 6px; opacity: 0.7; }
    .runtime-debug-list { display: grid; gap: 6px; margin-top: 10px; }
    .runtime-debug-item {
      border: 1px solid var(--divider);
      border-radius: var(--radius-md);
      padding: 8px 10px;
      background: var(--surface);
    }
    .runtime-debug-head { display: flex; gap: 8px; align-items: baseline; justify-content: space-between; margin-bottom: 6px; }
    .runtime-debug-kind { font-size: 10px; font-weight: 600; text-transform: uppercase; color: var(--accent); letter-spacing: 0.03em; }
    .runtime-debug-time { font-size: 10px; color: var(--muted); white-space: nowrap; opacity: 0.6; }
    .runtime-debug-item pre { max-height: 160px; overflow: auto; margin: 0; }

    /* ── Scrollbar ── */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-thumb { background: color-mix(in srgb, var(--text) 14%, transparent); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: color-mix(in srgb, var(--text) 22%, transparent); }
    ::-webkit-scrollbar-track { background: transparent; }
  </style>
</head>
<body>
  <div class="header">
    <h1>Python Agent</h1>
    <div class="header-meta">
      <div id="backendMode" class="badge muted-badge"></div>
      <div id="runtime" class="badge muted-badge"></div>
      <div id="skillsBadge" class="badge muted-badge"></div>
      <div id="status" class="badge muted-badge"></div>
    </div>
  </div>
  <div id="runtimeTabCard" class="flow-card lifecycle-card runtime-tab-card" hidden>
    <div class="runtime-picker">
      <label class="runtime-field"><span>Provider</span><select id="providerSelect"></select></label>
      <div class="runtime-sep"></div>
      <label class="runtime-field runtime-model-field"><span>Model</span><select id="modelSelect"></select></label>
      <label id="customModelField" class="runtime-field runtime-custom-model"><span>Custom</span><input id="customModelInput" placeholder="model name" /></label>
      <div id="runtimeStatus" class="runtime-status"></div>
    </div>
    <div class="backoff-row">
      <label class="backoff-toggle"><input type="checkbox" id="backoffEnabled" /><span>Backoff</span></label>
      <label class="backoff-limit"><input type="number" id="backoffLimitK" min="1" max="9999" step="1" placeholder="30" disabled /><span>k input tokens/min</span></label>
      <span id="backoffWindow" class="backoff-window"></span>
    </div>
    <div>
      <div class="flow-title">Backend</div>
      <div id="backendSummary" class="compact-list"></div>
      <button id="switchBackendBtn" class="secondary small" style="margin-top:6px">Switch to Beta</button>
    </div>
    <div>
      <div class="flow-title">Loaded Skills</div>
      <div id="skillsSummary" class="compact-list"></div>
    </div>
    <div class="runtime-debug">
      <details open>
        <summary>Bridge Debug</summary>
        <div id="runtimeDebugMeta" class="runtime-debug-meta">No events yet.</div>
        <div id="runtimeDebugList" class="runtime-debug-list"></div>
      </details>
    </div>
  </div>
  <div id="feed" class="feed"></div>
  <div class="input-area">
    <div id="lifecycleDock" class="lifecycle-dock">
      <div class="dock-head">
        <div id="dockTabs" class="dock-tabs"></div>
        <div id="dockActions" class="dock-actions"></div>
      </div>
      <div id="dockPanels" class="dock-panels"></div>
    </div>
    <form id="promptForm">
      <textarea id="promptInput" rows="1" placeholder="Send a message..."></textarea>
      <button id="sendBtn" class="primary" type="submit">Send</button>
    </form>
  </div>
  <script>
    const vscode = acquireVsCodeApi();
    let state = ${initialState};
    let runtimeOptions = { providers: [], current_provider: '', current_model: '' };
    let backendInfo = {};

    const feedEl = document.getElementById('feed');
    const backendModeEl = document.getElementById('backendMode');
    const runtimeEl = document.getElementById('runtime');
    const skillsBadgeEl = document.getElementById('skillsBadge');
    const statusEl = document.getElementById('status');
    const runtimeTabCard = document.getElementById('runtimeTabCard');
    const providerSelect = document.getElementById('providerSelect');
    const modelSelect = document.getElementById('modelSelect');
    const customModelField = document.getElementById('customModelField');
    const customModelInput = document.getElementById('customModelInput');
    const runtimeStatusEl = document.getElementById('runtimeStatus');
    const backendSummaryEl = document.getElementById('backendSummary');
    const skillsSummaryEl = document.getElementById('skillsSummary');
    const runtimeDebugMetaEl = document.getElementById('runtimeDebugMeta');
    const runtimeDebugListEl = document.getElementById('runtimeDebugList');
    const switchBackendBtn = document.getElementById('switchBackendBtn');
    const backoffEnabled = document.getElementById('backoffEnabled');
    const backoffLimitK = document.getElementById('backoffLimitK');
    const backoffWindow = document.getElementById('backoffWindow');
    const lifecycleDockEl = document.getElementById('lifecycleDock');
    const dockTabsEl = document.getElementById('dockTabs');
    const dockActionsEl = document.getElementById('dockActions');
    const dockPanelsEl = document.getElementById('dockPanels');
    const promptForm = document.getElementById('promptForm');
    const promptInput = document.getElementById('promptInput');
    let activeDockTab = 'discovery';
    let lastAutoDockTab = 'discovery';
    let factsBadgeCount = 0;
    let lastFactKeys = [];
    let lifecycleHistory = [];
    let dockScrollTopByTab = {};
    let runtimeApplyTimer = null;
    let runtimeStatusTimer = null;
    let runtimeSwitchPending = false;

    function el(tag, className, text) {
      const node = document.createElement(tag);
      if (className) { node.className = className; }
      if (text) { node.textContent = text; }
      return node;
    }

    function truncate(text, limit = 220) {
      const v = String(text || '').trim();
      return !v ? '' : v.length <= limit ? v : v.slice(0, limit - 1) + '…';
    }

    function statusLabel() {
      const status = state.planner?.status || 'idle';
      const map = {
        awaiting_discovery_selection: 'Discovery Needed',
        awaiting_plan_approval: 'Plan Ready',
        awaiting_plan_revision: 'Plan Revision',
        completed: 'Completed',
        planning: 'Planning',
        executing: 'Executing',
        idle: 'Idle',
      };
      return map[status] || status;
    }

    function runtimeLabel() {
      const plannerRuntime = state.planner?.runtime_config || {};
      const workerRuntime = state.planner?.worker_state?.runtime_config || {};
      const provider = String(plannerRuntime.provider || workerRuntime.provider || '').trim();
      const model = String(plannerRuntime.model || workerRuntime.model || '').trim();
      if (provider && model) {
        return provider + ' / ' + model;
      }
      if (model) {
        return model;
      }
      if (provider) {
        return provider;
      }
      return 'Runtime pending';
    }

    function backendModeLabel() {
      const label = String(backendInfo.launched_runtime_label || '').trim();
      const script = String(backendInfo.launched_script_name || '').trim();
      if (label && script) {
        return label + ' · ' + script;
      }
      if (label) {
        return label;
      }
      if (script) {
        return script;
      }
      return 'Backend pending';
    }

    function currentSkillCatalog() {
      const skills = state.planner?.worker_state?.available_skills;
      if (!Array.isArray(skills)) {
        return [];
      }
      return skills.slice().sort((left, right) => {
        const leftPriority = Number(left?.priority || 0);
        const rightPriority = Number(right?.priority || 0);
        if (leftPriority !== rightPriority) {
          return rightPriority - leftPriority;
        }
        return String(left?.name || '').localeCompare(String(right?.name || ''));
      });
    }

    function skillsBadgeLabel() {
      const count = currentSkillCatalog().length;
      if (!count) {
        return 'Skills pending';
      }
      return count + ' skill' + (count === 1 ? '' : 's');
    }

    function renderRuntimeSummary() {
      if (!backendSummaryEl || !skillsSummaryEl) {
        return;
      }

      backendSummaryEl.innerHTML = '';
      const backendItems = [];
      const launchedLabel = String(backendInfo.launched_runtime_label || '').trim();
      const launchedScript = String(backendInfo.launched_script_name || '').trim();
      const configuredLabel = String(backendInfo.configured_runtime_label || '').trim();
      const configuredScript = String(backendInfo.configured_script_name || '').trim();

      backendItems.push(el('div', 'compact-item', (launchedLabel || 'Runtime') + (launchedScript ? ' · ' + launchedScript : '')));
      if (backendInfo.mismatch) {
        backendItems.push(el('div', 'compact-item warning', 'Configured: ' + (configuredLabel || 'Runtime') + (configuredScript ? ' · ' + configuredScript : '') + ' — restart backend to apply.'));
      } else if (backendInfo.launched_script_path) {
        backendItems.push(el('div', 'compact-item', 'Using configured backend script.'));
      }
      for (const item of backendItems) {
        backendSummaryEl.appendChild(item);
      }

      if (switchBackendBtn) {
        const runtimeKey = String(backendInfo.launched_runtime_key || backendInfo.configured_runtime_key || '').trim();
        if (runtimeKey === 'stable') {
          switchBackendBtn.textContent = 'Switch to Beta';
          switchBackendBtn.removeAttribute('disabled');
        } else if (runtimeKey === 'beta') {
          switchBackendBtn.textContent = 'Switch to Stable';
          switchBackendBtn.removeAttribute('disabled');
        } else {
          switchBackendBtn.textContent = 'Switch to Stable';
          switchBackendBtn.removeAttribute('disabled');
        }
      }

      skillsSummaryEl.innerHTML = '';
      const skills = currentSkillCatalog();
      if (!skills.length) {
        skillsSummaryEl.appendChild(el('div', 'compact-item muted', 'No registered skills reported by this runtime yet.'));
        return;
      }
      for (const skill of skills.slice(0, 8)) {
        const bits = [];
        const modes = Array.isArray(skill?.modes) ? skill.modes.filter(Boolean) : [];
        const category = String(skill?.category || '').trim();
        if (category) {
          bits.push(category);
        }
        if (modes.length) {
          bits.push('modes: ' + modes.join(', '));
        }
        const label = String(skill?.name || 'skill');
        const description = String(skill?.description || '').trim();
        const meta = bits.length ? ' [' + bits.join(' · ') + ']' : '';
        skillsSummaryEl.appendChild(el('div', 'compact-item', label + meta + (description ? ': ' + description : '')));
      }
      if (skills.length > 8) {
        skillsSummaryEl.appendChild(el('div', 'compact-item muted', '+' + (skills.length - 8) + ' more'));
      }
    }

    function currentRuntimeConfig() {
      const plannerRuntime = state.planner?.runtime_config || {};
      const workerRuntime = state.planner?.worker_state?.runtime_config || {};
      return {
        provider: String(plannerRuntime.provider || workerRuntime.provider || runtimeOptions.current_provider || '').trim(),
        model: String(plannerRuntime.model || workerRuntime.model || runtimeOptions.current_model || '').trim(),
      };
    }

    function findProviderOption(providerKey) {
      const list = Array.isArray(runtimeOptions.providers) ? runtimeOptions.providers : [];
      return list.find(item => String(item?.key || '').trim() === String(providerKey || '').trim()) || null;
    }

    function setRuntimeControlsDisabled(disabled) {
      providerSelect.disabled = !!disabled;
      modelSelect.disabled = !!disabled;
      customModelInput.disabled = !!disabled;
    }

    function setRuntimeStatus(kind, text) {
      runtimeStatusEl.className = 'runtime-status' + (kind ? ' ' + kind : '');
      runtimeStatusEl.textContent = text || '';
      if (runtimeStatusTimer) {
        clearTimeout(runtimeStatusTimer);
        runtimeStatusTimer = null;
      }
      if (kind === 'success') {
        runtimeStatusTimer = setTimeout(() => {
          runtimeStatusEl.className = 'runtime-status';
          runtimeStatusEl.textContent = '';
          runtimeStatusTimer = null;
        }, 1800);
      }
    }

    function selectedModelValue() {
      const selected = String(modelSelect.value || '').trim();
      if (selected === '__custom__') {
        return String(customModelInput.value || '').trim();
      }
      return selected;
    }

    function updateCustomModelVisibility() {
      const visible = String(modelSelect.value || '').trim() === '__custom__';
      customModelField.classList.toggle('visible', visible);
      if (!visible) {
        customModelInput.value = '';
      }
    }

    function syncModelOptionsForProvider(providerKey, preferredModel) {
      const option = findProviderOption(providerKey);
      const suggested = Array.isArray(option?.suggested_models) ? option.suggested_models : [];
      const nextValue = String(preferredModel || option?.active_model || option?.default_model || '').trim();
      modelSelect.innerHTML = '';
      let matchedSuggested = false;
      for (const modelName of suggested) {
        const opt = document.createElement('option');
        const normalized = String(modelName || '').trim();
        opt.value = normalized;
        opt.textContent = normalized;
        if (normalized === nextValue) {
          matchedSuggested = true;
        }
        modelSelect.appendChild(opt);
      }
      const customOption = document.createElement('option');
      customOption.value = '__custom__';
      customOption.textContent = 'Custom...';
      modelSelect.appendChild(customOption);
      if (nextValue && !matchedSuggested) {
        modelSelect.value = '__custom__';
        customModelInput.value = nextValue;
      } else {
        modelSelect.value = nextValue || String(suggested[0] || '__custom__');
        if (!modelSelect.value) {
          modelSelect.value = '__custom__';
        }
      }
      updateCustomModelVisibility();
    }

    function syncRuntimeControls() {
      const current = currentRuntimeConfig();
      const providers = Array.isArray(runtimeOptions.providers) ? runtimeOptions.providers : [];
      const currentProvider = current.provider || runtimeOptions.current_provider || String(providers[0]?.key || '').trim();
      const existingValue = String(providerSelect.value || '').trim();
      providerSelect.innerHTML = '';
      for (const provider of providers) {
        if (provider?.hidden) { continue; }
        const key = String(provider?.key || '').trim();
        if (!key) { continue; }
        const option = document.createElement('option');
        option.value = key;
        option.textContent = key;
        providerSelect.appendChild(option);
      }
      providerSelect.value = existingValue && findProviderOption(existingValue) ? existingValue : currentProvider;
      syncModelOptionsForProvider(providerSelect.value, current.model || runtimeOptions.current_model);
      setRuntimeControlsDisabled(providers.length === 0);
      // sync backoff from state if present
      const workerState = ((state || {}).planner || {}).worker_state || state || {};
      const bs = workerState.backoff;
      if (bs && typeof bs === 'object') {
        syncBackoffControls(bs);
      }
    }

    function syncBackoffControls(bs) {
      const enabled = Boolean(bs.enabled);
      backoffEnabled.checked = enabled;
      backoffLimitK.disabled = !enabled;
      if (bs.token_limit_k > 0) {
        backoffLimitK.value = String(bs.token_limit_k);
      }
      const used = parseInt(String(bs.window_tokens_used || 0), 10);
      if (enabled && used > 0) {
        backoffWindow.textContent = used.toLocaleString() + ' used';
      } else {
        backoffWindow.textContent = '';
      }
    }

    function runtimeSelectionMatchesCurrent() {
      const current = currentRuntimeConfig();
      return String(providerSelect.value || '').trim() === String(current.provider || '').trim()
        && selectedModelValue() === String(current.model || '').trim();
    }

    function triggerRuntimeSwitch() {
      const provider = String(providerSelect.value || '').trim();
      const model = selectedModelValue();
      if (!provider || !model || pendingAction || runtimeSelectionMatchesCurrent()) {
        return;
      }
      runtimeSwitchPending = true;
      lockButtons();
      setRuntimeStatus('loading', 'Updating runtime...');
      vscode.postMessage({ type: 'runtimeSwitch', provider, model });
    }

    if (switchBackendBtn) {
      switchBackendBtn.addEventListener('click', () => {
        const runtimeKey = String(backendInfo.launched_runtime_key || backendInfo.configured_runtime_key || '').trim();
        const target = (runtimeKey === 'stable') ? 'beta' : 'stable';
        switchBackendBtn.disabled = true;
        switchBackendBtn.textContent = 'Switching...';
        vscode.postMessage({ type: 'switchBackend', target });
      });
    }

    function scheduleRuntimeSwitch(delay = 250) {
      if (runtimeApplyTimer) {
        clearTimeout(runtimeApplyTimer);
      }
      runtimeApplyTimer = setTimeout(() => {
        runtimeApplyTimer = null;
        triggerRuntimeSwitch();
      }, delay);
    }

    /* Inlined from panelModel – webview can't use TS module imports */
    function combineSuggestedActions(s) {
      const pa = (s.planner && s.planner.suggested_next_actions) || [];
      const wa = (s.planner && s.planner.worker_state && s.planner.worker_state.suggested_next_actions) || [];
      return pa.map(a => ({ ...a, source: 'planner' })).concat(wa.map(a => ({ ...a, source: 'worker' })));
    }
    function isContinuousModeActive(planner) {
      const status = String(planner?.continuous_mode?.status || '').trim();
      return !!(planner?.continuous_mode?.enabled || (status && !['idle', 'stopped'].includes(status)));
    }
    function continuousModeOwnsLifecycle(planner) {
      const status = String(planner?.continuous_mode?.status || '').trim();
      return ['selecting_issue', 'discovering', 'planning', 'approving', 'executing', 'reviewing', 'closing_issue', 'creating_followups'].includes(status);
    }
    function buildPlannerActionMessage(action) {
      const payload = (typeof action.payload === 'object' && action.payload && !Array.isArray(action.payload)) ? { ...action.payload } : {};
      if (typeof action.mode === 'string' && action.mode.trim()) { payload.mode = action.mode.trim(); }
      if (typeof action.issue_id === 'string' && action.issue_id.trim()) { payload.issue_id = action.issue_id.trim(); }
      if (typeof action.max_cycles === 'number' && Number.isFinite(action.max_cycles)) {
        payload.max_cycles = Math.max(1, Math.floor(action.max_cycles));
      }
      return {
        action: action.type,
        mode: typeof action.mode === 'string' ? action.mode : undefined,
        payload: Object.keys(payload).length ? payload : undefined,
      };
    }
    function progressTimelineTarget(planner, pendingActionType) {
      if (planner?.executing) { return 'plan'; }
      if (planner?.pending_discovery || pendingActionType === 'select_discovery_mode') { return 'discovery'; }
      if (isContinuousModeActive(planner)) { return 'plan'; }
      return undefined;
    }
    function actionAllowedDuringContinuous(action) {
      const type = String(action?.type || '').trim();
      return [
        'stop_continuous',
        'read_file',
        'list_files',
        'git_diff',
        'show_diff',
        'review_changes',
        'list_issues',
        'show_issue',
      ].includes(type);
    }
    function buildReviewReport(review) {
      if (!review || !review.action_type) { return null; }
      if (review.action_type === 'review_changes') {
        return { title: 'Python Agent review_changes', language: 'json', content: JSON.stringify(review, null, 2) };
      }
      const content = [String(review.stat || ''), String(review.diff || '')].filter(Boolean).join('\\n\\n').trim();
      return content ? { title: 'Python Agent ' + review.action_type, language: 'diff', content } : null;
    }
    function primaryPathForReview(review) {
      if (!review) { return undefined; }
      if (review.path) { return review.path; }
      const files = Array.isArray(review.files) ? review.files : [];
      for (const item of files) {
        if (typeof item === 'string' && item.trim()) { return item; }
        if (typeof item === 'object' && item && typeof item.path === 'string' && item.path.trim()) { return item.path; }
      }
      return undefined;
    }

    let discoveryTimeline = [];
    let planTimeline = [];

    function goalListText(goals) {
      const list = Array.isArray(goals) ? goals : [];
      return list.map(g => g.title || g.goal_id || g.goal || 'Goal').join(', ');
    }

    function sameLifecycleRows(left, right) {
      const leftRows = Array.isArray(left) ? left : [];
      const rightRows = Array.isArray(right) ? right : [];
      if (leftRows.length !== rightRows.length) {
        return false;
      }
      for (let i = 0; i < leftRows.length; i++) {
        const a = leftRows[i] || {};
        const b = rightRows[i] || {};
        if (String(a.label || '') !== String(b.label || '') || String(a.value || '') !== String(b.value || '')) {
          return false;
        }
      }
      return true;
    }

    function upsertLifecycleHistory(entry) {
      if (!entry || !entry.requestKey) {
        return;
      }
      const lastEntry = lifecycleHistory.length ? lifecycleHistory[lifecycleHistory.length - 1] : null;
      if (
        lastEntry &&
        String(lastEntry.requestKey || '') === String(entry.requestKey || '') &&
        String(lastEntry.title || '') === String(entry.title || '') &&
        sameLifecycleRows(lastEntry.rows, entry.rows) &&
        String(lastEntry.outcome || '') === String(entry.outcome || '') &&
        String(lastEntry.tone || '') === String(entry.tone || '')
      ) {
        return;
      }
      if (lastEntry && String(lastEntry.requestKey || '') === String(entry.requestKey || '')) {
        lastEntry.title = entry.title || lastEntry.title;
        lastEntry.rows = Array.isArray(entry.rows) ? entry.rows : lastEntry.rows;
        lastEntry.outcome = entry.outcome || '';
        lastEntry.tone = entry.tone || '';
        return;
      }
      lifecycleHistory.push({
        requestKey: entry.requestKey,
        title: entry.title || 'Lifecycle',
        rows: Array.isArray(entry.rows) ? entry.rows : [],
        outcome: entry.outcome || '',
        tone: entry.tone || '',
      });
    }

    function buildLifecycleEntry(planner) {
      const continuous = planner.continuous_mode || {};
      const requestKey = String(planner.latest_request || continuous.active_issue_id || continuous.status || '').trim();
      if (!requestKey) {
        return null;
      }

      const rows = [];
      const pendingDiscovery = planner.pending_discovery;
      const lastDiscovery = planner.last_discovery;
      const plan = planner.pending_plan || planner.last_presented_plan || planner.last_completed_plan;

      if (isContinuousModeActive(planner) || continuous.stop_reason) {
        rows.push({ label: 'Auto Mode', value: String(continuous.status || 'active') });
        if (continuous.cycle || continuous.max_cycles) {
          rows.push({ label: 'Cycle', value: String(continuous.cycle || 0) + '/' + String(continuous.max_cycles || '?') });
        }
        if (continuous.active_issue_id) { rows.push({ label: 'Issue', value: String(continuous.active_issue_id) }); }
        if (continuous.selected_discovery_mode) { rows.push({ label: 'Discovery', value: String(continuous.selected_discovery_mode) }); }
        if (continuous.latest_review_decision) { rows.push({ label: 'Review', value: String(continuous.latest_review_decision) }); }
      }

      if (pendingDiscovery || lastDiscovery) {
        const discovery = lastDiscovery || pendingDiscovery || {};
        const prompt = (lastDiscovery && lastDiscovery.prompt) || (pendingDiscovery && pendingDiscovery.prompt) || '';
        const actionMeta = [];
        if (lastDiscovery && lastDiscovery.mode) { actionMeta.push(String(lastDiscovery.mode)); }
        if (lastDiscovery && lastDiscovery.duration_s) { actionMeta.push(Math.round(lastDiscovery.duration_s) + 's'); }
        if (lastDiscovery && lastDiscovery.tool_calls_used != null) { actionMeta.push(lastDiscovery.tool_calls_used + '/' + (lastDiscovery.tool_calls_max || '?') + ' tool calls'); }
        if (discovery.reason) { rows.push({ label: 'Discovery', value: discovery.reason }); }
        if (prompt) { rows.push({ label: 'Prompt', value: prompt }); }
        if (actionMeta.length) {
          rows.push({ label: 'Action', value: actionMeta.join(' · ') });
        } else if (pendingDiscovery) {
          rows.push({ label: 'Action', value: 'Awaiting choice' + (pendingDiscovery.recommended_mode ? ' · suggested ' + pendingDiscovery.recommended_mode : '') });
        }
      }

      if (plan) {
        if (plan.summary) { rows.push({ label: 'Plan', value: plan.summary }); }
        const goalsText = goalListText(plan.goals || []);
        if (goalsText) { rows.push({ label: 'Goals', value: goalsText }); }
        if (planner.last_execution_summary) {
          rows.push({ label: 'Status', value: 'Approved → Executed' });
        } else if (planner.executing) {
          rows.push({ label: 'Status', value: 'Approved → Execution started' });
        } else if (planner.pending_plan) {
          rows.push({ label: 'Status', value: 'Awaiting approval' });
        }
      }

      const outcome = planner.last_execution_summary || (lastDiscovery && lastDiscovery.final_message) || (continuous.stop_reason ? 'Continuous mode stopped: ' + continuous.stop_reason : '');
      if (!rows.length && !outcome) {
        return null;
      }

      return {
        requestKey,
        title: 'Planner Trace',
        rows,
        outcome,
      };
    }

    function recordLifecycleTransitions(previousPlanner, planner) {
      const entry = buildLifecycleEntry(planner);
      if (entry) {
        upsertLifecycleHistory(entry);
      }

      if (String(previousPlanner.latest_request || '') !== String(planner.latest_request || '') && String(planner.latest_request || '').trim()) {
        const latestEntry = buildLifecycleEntry(planner);
        if (latestEntry) {
          upsertLifecycleHistory(latestEntry);
        }
      }
    }

    function buildLifecycleHistoryNode(entry) {
      const trace = el('div', 'lifecycle-trace' + (entry.tone ? ' ' + entry.tone : ''));
      trace.appendChild(el('span', 'trace-title', entry.title || 'Lifecycle'));
      for (const rowData of entry.rows || []) {
        if (!rowData || !rowData.value) { continue; }
        const row = el('div', 'trace-row');
        row.appendChild(el('span', 'trace-label', rowData.label || 'Info'));
        row.appendChild(el('span', 'trace-value', rowData.value));
        trace.appendChild(row);
      }
      if (entry.outcome) {
        trace.appendChild(el('div', 'trace-outcome', entry.outcome));
      }
      return trace;
    }

      function currentLifecycleEntry(planner) {
        const current = buildLifecycleEntry(planner);
        if (current) {
          return current;
        }
        if (!lifecycleHistory.length) {
          return null;
        }
        const requestKey = String(planner.latest_request || '').trim();
        if (!requestKey) {
          return lifecycleHistory[lifecycleHistory.length - 1] || null;
        }
        for (let i = lifecycleHistory.length - 1; i >= 0; i -= 1) {
          const entry = lifecycleHistory[i];
          if (String(entry.requestKey || '') === requestKey) {
            return entry;
          }
        }
        return lifecycleHistory[lifecycleHistory.length - 1] || null;
      }

    function buildOperationalPlannerCard(planner) {
      const worker = planner.worker_state || {};
      const items = [];

      const llmActivity = worker.llm_activity || {};
      if (llmActivity.in_flight) {
        const turn = Number(llmActivity.turn || 0);
        const runtime = [worker.runtime_config?.provider, worker.runtime_config?.model].filter(Boolean).join(' / ');
        items.push({
          label: 'Model',
          value: 'Waiting on ' + (runtime || 'configured model') + (turn > 0 ? ' (turn ' + turn + ')' : ''),
        });
      } else if (String(llmActivity.last_event || '') === 'model_call_error') {
        items.push({
          label: 'Model',
          value: 'Last call failed' + (llmActivity.error ? ': ' + llmActivity.error : ''),
        });
      }

      if (worker.pending_verification && worker.pending_verification.path) {
        items.push({ label: 'Verification', value: worker.pending_verification.path + ' needs validation' + (worker.pending_verification.mode ? ' (' + worker.pending_verification.mode + ')' : '') });
      }

      if (worker.latest_review && worker.latest_review.action_type) {
        const reviewLabel = worker.latest_review.summary || worker.latest_review.action_type;
        const reviewStep = worker.latest_review.step ? ' [step ' + worker.latest_review.step + ']' : '';
        items.push({ label: 'Review', value: reviewLabel + reviewStep });
      }

      const batch = worker.edit_batch || {};
      if (batch.active && Array.isArray(batch.pending_paths) && batch.pending_paths.length) {
        items.push({ label: 'Edit Batch', value: batch.pending_paths.length + ' file(s): ' + batch.pending_paths.join(', ') });
      }

      if (!items.length) {
        return null;
      }

      const trace = el('div', 'lifecycle-trace operational');
      trace.appendChild(el('span', 'trace-title', 'Worker Status'));
      for (const item of items) {
        const row = el('div', 'trace-row');
        row.appendChild(el('span', 'trace-label', item.label));
        row.appendChild(el('span', 'trace-value', item.value));
        trace.appendChild(row);
      }
      return trace;
    }

    function renderStepItem(p) {
      const item = el('div', 'step-item' + (p.ok ? '' : ' error'));
      item.appendChild(el('span', 'step-icon ' + (p.ok ? 'ok' : 'fail'), p.ok ? '\u2713' : '\u2717'));
      const detail = el('div', 'step-detail');
      let label = p.action_type || 'step';
      if (p.action_type === 'model_call_start') {
        label = 'model call started';
      } else if (p.action_type === 'model_call_finish') {
        label = 'model call finished';
      } else if (p.action_type === 'model_call_error') {
        label = 'model call failed';
      } else if (p.action_type === 'model_call_interrupted') {
        label = 'model call interrupted';
      } else if (p.action_type === 'output_format_error') {
        label = 'output format';
      }
      if (p.action_type === 'skill') {
        if (p.skill_name) {
          label = 'skill ' + p.skill_name + (p.skill_mode ? ' [' + p.skill_mode + ']' : '');
        } else if (typeof p.skill_count === 'number' && p.skill_count > 0) {
          label = 'skill catalog (' + p.skill_count + ')';
        }
      }
      if (p.action_type === 'inspect_files' && typeof p.inspected_file_count === 'number' && p.inspected_file_count > 0) {
        label += ' (' + p.inspected_file_count + ' file' + (p.inspected_file_count === 1 ? '' : 's') + ')';
      }
      if (p.path) { label += ' ' + p.path.split('/').pop(); }
      detail.appendChild(el('span', 'step-action', '[' + p.step + '] ' + label));
      if (p.path) {
        const pathEl = el('span', 'step-path', ' ' + p.path);
        pathEl.addEventListener('click', () => vscode.postMessage({ type: 'openPath', path: p.path }));
        detail.appendChild(pathEl);
      }
      if (p.thought) { detail.appendChild(el('div', 'step-thought', p.thought)); }
      if (p.summary && p.summary !== p.thought) { detail.appendChild(el('div', 'step-summary', p.summary)); }
      if (p.action_type === 'skill') {
        const meta = [];
        if (p.skill_name) {
          meta.push('loaded: ' + p.skill_name);
        }
        if (p.skill_mode) {
          meta.push('mode: ' + p.skill_mode);
        }
        if (!p.skill_name && typeof p.skill_count === 'number' && p.skill_count > 0) {
          meta.push('available: ' + p.skill_count + ' skill' + (p.skill_count === 1 ? '' : 's'));
        }
        if (meta.length) {
          detail.appendChild(el('div', 'step-patch-meta', meta.join(' · ')));
        }
      }
      if (p.action_type === 'inspect_files') {
        const inspectedFiles = Array.isArray(p.inspected_files) ? p.inspected_files : [];
        if (typeof p.inspected_file_count === 'number' && p.inspected_file_count > 0) {
          detail.appendChild(el('div', 'step-inspect-meta', 'Inspecting ' + p.inspected_file_count + ' file' + (p.inspected_file_count === 1 ? '' : 's')));
        }
        if (inspectedFiles.length) {
          const list = el('div', 'step-inspect-list');
          for (const inspected of inspectedFiles.slice(0, 8)) {
            const row = el('div', 'step-inspect-item' + (inspected.ok === false ? ' error' : ''));
            const targetPath = String(inspected.path || 'Unknown path');
            const pathEl = el('div', 'step-inspect-path', targetPath);
            if (inspected.path) {
              pathEl.addEventListener('click', () => vscode.postMessage({
                type: 'openPath',
                path: inspected.path,
                line: typeof inspected.start_line === 'number' && inspected.start_line > 0 ? inspected.start_line : undefined,
              }));
            }
            row.appendChild(pathEl);
            const start = typeof inspected.start_line === 'number' && inspected.start_line > 0 ? inspected.start_line : 0;
            const end = typeof inspected.end_line === 'number' && inspected.end_line > 0 ? inspected.end_line : 0;
            if (start && end) {
              row.appendChild(el('div', 'step-inspect-range', 'Lines ' + start + '-' + end));
            } else if (start) {
              row.appendChild(el('div', 'step-inspect-range', 'From line ' + start));
            } else {
              row.appendChild(el('div', 'step-inspect-range', 'Full file'));
            }
            if (inspected.ok === false && inspected.error) {
              row.appendChild(el('div', 'step-inspect-error', inspected.error));
            }
            list.appendChild(row);
          }
          if (typeof p.inspected_file_count === 'number' && p.inspected_file_count > inspectedFiles.length) {
            list.appendChild(el('div', 'step-inspect-meta', '+' + (p.inspected_file_count - inspectedFiles.length) + ' more'));
          }
          detail.appendChild(list);
        }
      }
      if (p.action_type === 'patch_file') {
        const patchMeta = [];
        if (typeof p.added_lines === 'number' || typeof p.removed_lines === 'number') {
          patchMeta.push('+' + String(p.added_lines || 0) + '/-' + String(p.removed_lines || 0) + ' lines');
        }
        if (typeof p.replacements === 'number' && p.replacements > 0) {
          patchMeta.push(String(p.replacements) + ' replacement' + (p.replacements === 1 ? '' : 's'));
        }
        if (patchMeta.length) {
          detail.appendChild(el('div', 'step-patch-meta', patchMeta.join(' · ')));
        }
        if (p.search_excerpt || p.replace_excerpt) {
          const pair = el('div', 'step-patch-pair');
          if (p.search_excerpt) {
            const row = el('div', 'step-patch-row');
            row.appendChild(el('div', 'step-patch-label', 'Replaced'));
            row.appendChild(el('div', 'step-patch-text', p.search_excerpt));
            pair.appendChild(row);
          }
          if (p.replace_excerpt) {
            const row = el('div', 'step-patch-row');
            row.appendChild(el('div', 'step-patch-label', 'With'));
            row.appendChild(el('div', 'step-patch-text', p.replace_excerpt));
            pair.appendChild(row);
          }
          detail.appendChild(pair);
        }
        if (p.diff) {
          const diffDetails = document.createElement('details');
          diffDetails.className = 'step-patch-diff';
          const summaryEl = document.createElement('summary');
          summaryEl.textContent = 'Applied patch';
          const pre = document.createElement('pre');
          pre.textContent = String(p.diff || '');
          diffDetails.appendChild(summaryEl);
          diffDetails.appendChild(pre);
          detail.appendChild(diffDetails);
        }
      }
      item.appendChild(detail);
      item.appendChild(el('span', 'step-time', p.elapsed_s + 's'));
      return item;
    }

    function renderGoalEventItem(msg, done) {
      const header = el('div', 'goal-header' + (done ? ' goal-done' : ''));
      const idx = (msg.goal_index != null && msg.goal_index > 0) ? msg.goal_index : '?';
      const total = state.planner?.executing_goal_count || '?';
      header.appendChild(el('span', 'goal-index', (done ? '\u2713 ' : '') + 'Goal ' + idx + '/' + total));
      header.appendChild(el('span', 'goal-label', msg.goal_title || 'Goal'));
      return header;
    }

    function renderTimeline(items) {
      const wrap = el('div', 'timeline-list');
      for (const entry of items) {
        if (entry.kind === 'goal') {
          wrap.appendChild(renderGoalEventItem(entry.message, !!entry.done));
          continue;
        }
        wrap.appendChild(renderStepItem(entry.message));
      }
      return wrap;
    }

    function renderInlineStatus(label, body, waiting = false) {
      const node = el('div', 'inline-status' + (waiting ? ' waiting' : ''));
      const text = el('div', 'inline-status-note');
      const labelEl = el('span', 'inline-status-label', label);
      text.appendChild(labelEl);
      text.appendChild(document.createTextNode(body));
      node.appendChild(text);
      return node;
    }

    function renderFullAssistantMessage(content) {
      return el('div', 'bubble assistant', String(content || ''));
    }

    function isLifecycleChoiceMessage(item) {
      if (!item || item.role !== 'user') { return false; }
      const value = String(item.content || '').trim().toLowerCase();
      return ['quick', 'moderate', 'deep', 'approve', 'approve plan', 'reject', 'reject plan', 'skip discovery', 'reset session'].includes(value);
    }

    function isLifecycleAssistantMessage(item) {
      if (!item || item.role === 'user') { return false; }
      const value = String(item.content || '').trim();
      return /^(Discovery Suggested|Discovery Complete|Discovery Failed|Plan Ready|Outcome|Discovery Needed)/i.test(value);
    }

    function buildInlineLifecycleItems(planner, hasDock) {
      if (hasDock) {
        return [];
      }
      const items = lifecycleHistory.map(entry => buildLifecycleHistoryNode(entry));
      if (planner.pending_discovery && !planner.last_discovery) {
        items.push(renderInlineStatus('Awaiting action', 'Discovery needs your confirmation.', true));
      }
      if (planner.pending_plan && !planner.executing && !planner.last_execution_summary) {
        items.push(renderInlineStatus('Awaiting action', 'Plan is ready for approval.', true));
      } else if (planner.executing) {
        items.push(renderInlineStatus('Plan approved', 'Execution in progress.', true));
      }
      return items;
    }

    function buildFactsCard(planner) {
      const worker = planner.worker_state || {};
      const facts = worker.current_run_facts || [];
      const issueState = worker.issue_state || {};
      const activeIssue = issueState.active_issue || null;
      if (!facts.length && !activeIssue) { return null; }

      const card = el('div', 'flow-card lifecycle-card');
      card.appendChild(el('div', 'flow-title', 'Run Facts'));
      if (activeIssue && activeIssue.issue_id) {
        const summary = activeIssue.plan_summary || activeIssue.request_summary || '';
        card.appendChild(el('div', 'flow-body muted-block', 'Active issue: ' + activeIssue.issue_id + (summary ? ' - ' + summary : '')));
      }
      const list = el('div', 'compact-list');
      for (const fact of facts) {
        const typeLabel = fact.fact_type ? ' [' + fact.fact_type + ']' : '';
        list.appendChild(el('div', 'compact-item', (fact.key || 'fact') + typeLabel + ': ' + (fact.value || '')));
      }
      if (facts.length) {
        card.appendChild(list);
      }
      return card;
    }

    function buildIssuesCard(planner, actions) {
      const plannerIssueState = planner.issue_state || {};
      const workerIssueState = (planner.worker_state || {}).issue_state || {};
      const activeIssue = workerIssueState.active_issue || plannerIssueState.active_issue || null;
      const reopenableIssues = Array.isArray(workerIssueState.reopenable_issues)
        ? workerIssueState.reopenable_issues
        : (Array.isArray(plannerIssueState.reopenable_issues) ? plannerIssueState.reopenable_issues : []);
      const totalFacts = Number(workerIssueState.total_fact_count || plannerIssueState.total_fact_count || 0);
      const hasDeleteSessionAction = actions.some(a => a.type === 'delete_session');
      if (!activeIssue && !reopenableIssues.length && !hasDeleteSessionAction && totalFacts <= 0) { return null; }

      const card = el('div', 'flow-card lifecycle-card');
      card.appendChild(el('div', 'flow-title', 'Issues'));
      if (totalFacts > 0) {
        card.appendChild(el('div', 'flow-meta', totalFacts + ' durable fact' + (totalFacts === 1 ? '' : 's') + ' tracked across issues'));
      }

      if (activeIssue && activeIssue.issue_id) {
        card.appendChild(el('div', 'flow-meta', 'Active Issue'));
        const summary = activeIssue.plan_summary || activeIssue.request_summary || 'Active issue';
        const status = String(activeIssue.status || 'open').toUpperCase();
        card.appendChild(el('div', 'flow-body', activeIssue.issue_id + ' [' + status + ']: ' + summary));
        card.appendChild(el('div', 'flow-meta', 'Use manual close if the app or agent stopped before the issue was closed automatically.'));
        const closeRow = el('div', 'flow-actions');
        const closeButton = el('button', 'secondary', 'Close Active Issue');
        closeButton.type = 'button';
        if (continuousModeOwnsLifecycle(planner)) {
          closeButton.disabled = true;
          closeButton.title = 'Continuous mode will close or keep issues after review.';
        }
        closeButton.addEventListener('click', () => {
          unlockButtons();
          lockButtons('close_issue');
          render();
          debugAndPost({ type: 'closeIssue', issue_id: activeIssue.issue_id });
        });
        closeRow.appendChild(closeButton);
        card.appendChild(closeRow);
      }

      if (reopenableIssues.length) {
        if (activeIssue && activeIssue.issue_id) {
          card.appendChild(el('div', 'flow-meta', 'Recent Closed Issues'));
        } else {
          card.appendChild(el('div', 'flow-meta', 'Reopen A Recent Issue'));
        }
        const list = el('div', 'compact-list');
        for (const issue of reopenableIssues.slice(0, 5)) {
          const issueId = String(issue.issue_id || '').trim();
          if (!issueId) { continue; }
          const summary = String(issue.plan_summary || issue.request_summary || issueId);
          const factCount = Number(issue.fact_count || 0);
          const goalCount = Number(issue.goal_fact_count || 0);
          const architectureCount = Number(issue.architecture_fact_count || 0);
          const metaBits = [];
          if (goalCount > 0) { metaBits.push(goalCount + ' goal'); }
          if (architectureCount > 0) { metaBits.push(architectureCount + ' architecture'); }
          if (!metaBits.length && factCount > 0) { metaBits.push(factCount + ' facts'); }
          list.appendChild(el('div', 'compact-item', issueId + ': ' + summary + (metaBits.length ? ' [' + metaBits.join(', ') + ']' : '')));
        }
        card.appendChild(list);
      }

      const issueActions = actions.filter(a => a.type === 'reopen_issue');
      if (issueActions.length) {
        const row = el('div', 'flow-actions');
        for (const action of issueActions) {
          const issueId = String(action.issue_id || '').trim();
          if (!issueId) { continue; }
          const btn = el('button', action.style === 'primary' ? 'primary' : (action.style === 'ghost' ? 'ghost' : ''), action.label || action.type);
          if (continuousModeOwnsLifecycle(planner)) {
            btn.disabled = true;
            btn.title = 'Continuous mode is selecting issues automatically.';
          }
          btn.addEventListener('click', () => {
            submitPromptThroughChat('reopen ' + issueId);
          });
          row.appendChild(btn);
        }
        card.appendChild(row);
      }

      const deleteSessionAction = actions.find(a => a.type === 'delete_session');
      if (deleteSessionAction) {
        const row = el('div', 'flow-actions');
        const btn = el('button', deleteSessionAction.style === 'primary' ? 'primary' : (deleteSessionAction.style === 'ghost' ? 'ghost' : ''), deleteSessionAction.label || 'Delete Session');
        if (continuousModeOwnsLifecycle(planner)) {
          btn.disabled = true;
          btn.title = 'Stop continuous mode before deleting session state.';
        }
        btn.addEventListener('click', () => {
          postAction(deleteSessionAction);
        });
        row.appendChild(btn);
        card.appendChild(row);
      }

      return card;
    }

    function buildRuntimeCard() {
      runtimeTabCard.hidden = false;
      renderRuntimeSummary();
      return runtimeTabCard;
    }

    let pendingAction = false;
    let pendingActionType = '';
    let debugEvents = [];

    function sanitizeDebugValue(value, depth = 0) {
      if (depth > 3) { return '[depth-limit]'; }
      if (value == null) { return value; }
      if (typeof value === 'string') {
        return value.length > 800 ? value.slice(0, 800) + '…' : value;
      }
      if (typeof value === 'number' || typeof value === 'boolean') {
        return value;
      }
      if (Array.isArray(value)) {
        return value.slice(0, 12).map(item => sanitizeDebugValue(item, depth + 1));
      }
      if (typeof value === 'object') {
        const out = {};
        for (const [key, item] of Object.entries(value).slice(0, 24)) {
          out[key] = sanitizeDebugValue(item, depth + 1);
        }
        return out;
      }
      return String(value);
    }

    function pushDebugEvent(kind, payload) {
      const stamp = new Date();
      debugEvents.unshift({
        kind: String(kind || 'unknown'),
        time: stamp.toLocaleTimeString(),
        payload: sanitizeDebugValue(payload),
      });
      if (debugEvents.length > 40) {
        debugEvents = debugEvents.slice(0, 40);
      }
    }

    function debugAndPost(message) {
      pushDebugEvent('webview->extension', message);
      vscode.postMessage(message);
    }

    function submitPromptThroughChat(text) {
      const prompt = String(text || '').trim();
      if (!prompt || pendingAction) { return false; }
      const transcript = Array.isArray(state.transcript) ? state.transcript.slice() : [];
      transcript.push({ role: 'user', content: prompt });
      state = { ...state, transcript };
      lockButtons('submitPrompt');
      render();
      debugAndPost({ type: 'submitPrompt', text: prompt });
      return true;
    }

    function renderRuntimeDebug() {
      if (!runtimeDebugMetaEl || !runtimeDebugListEl) { return; }
      runtimeDebugMetaEl.textContent = debugEvents.length
        ? (debugEvents.length + ' recent event' + (debugEvents.length === 1 ? '' : 's'))
        : 'No events yet.';
      runtimeDebugListEl.innerHTML = '';
      for (const event of debugEvents) {
        const item = el('div', 'runtime-debug-item');
        const head = el('div', 'runtime-debug-head');
        head.appendChild(el('span', 'runtime-debug-kind', event.kind));
        head.appendChild(el('span', 'runtime-debug-time', event.time));
        item.appendChild(head);
        const pre = document.createElement('pre');
        pre.textContent = JSON.stringify(event.payload, null, 2);
        item.appendChild(pre);
        runtimeDebugListEl.appendChild(item);
      }
    }

    function lockButtons(actionType = '') {
      pendingAction = true;
      pendingActionType = String(actionType || '').trim();
      document.querySelectorAll('.flow-actions button, #sendBtn').forEach(b => {
        b.disabled = true;
        b.classList.add('loading');
      });
      providerSelect.disabled = true;
      modelSelect.disabled = true;
      customModelInput.disabled = true;
    }

    function unlockButtons() {
      pendingAction = false;
      pendingActionType = '';
      document.querySelectorAll('button.loading').forEach(b => {
        b.disabled = false;
        b.classList.remove('loading');
      });
      syncRuntimeControls();
    }

    function postAction(action) {
      if (pendingAction) { return; }
      if (continuousModeOwnsLifecycle(state.planner || {}) && !actionAllowedDuringContinuous(action)) {
        return;
      }
      if (action.requires_confirmation) {
        const confirmationPrompt = String(action.confirmation_prompt || action.confirmationPrompt || 'Proceed with this action?');
        const confirmed = window.confirm(confirmationPrompt);
        if (!confirmed) { return; }
      }
      lockButtons(action.type || '');
      render();
      if (action._source === 'worker') {
        const payload = { ...action };
        delete payload._source;
        delete payload.source;
        debugAndPost({ type: 'workerAction', action: payload });
        return;
      }
      const message = buildPlannerActionMessage(action);
      debugAndPost({ type: 'plannerAction', action: message.action, mode: message.mode || '', payload: message.payload || {} });
    }

    function postContinuousStart(maxCycles) {
      if (pendingAction) { return; }
      const cycles = Math.max(1, Math.min(25, Math.floor(Number(maxCycles) || 1)));
      if (cycles > 1 && !window.confirm('Start continuous mode for ' + cycles + ' cycles?')) {
        return;
      }
      debugAndPost({ type: 'startContinuous', maxCycles: cycles });
      lockButtons('start_continuous');
      render();
    }

    function postContinuousStop() {
      if (pendingAction) { return; }
      debugAndPost({ type: 'stopContinuous' });
      lockButtons('stop_continuous');
      render();
    }

    function actionRow(actions) {
      if (!actions.length) { return null; }
      const row = el('div', 'flow-actions');
      for (const action of actions) {
        const btn = el('button', action.style === 'primary' ? 'primary' : (action.style === 'ghost' ? 'ghost' : ''), action.label || action.type);
        btn.type = 'button';
        if (action.type === 'start_continuous') {
          btn.dataset.continuousAction = 'start';
          btn.dataset.maxCycles = String(action.max_cycles || (action.payload && action.payload.max_cycles) || 1);
        } else if (action.type === 'stop_continuous') {
          btn.dataset.continuousAction = 'stop';
        }
        if (continuousModeOwnsLifecycle(state.planner || {}) && !actionAllowedDuringContinuous(action)) {
          btn.disabled = true;
          btn.title = 'Continuous mode owns the lifecycle right now.';
        }
        btn.addEventListener('click', () => {
          if (action.type === 'start_continuous') {
            postContinuousStart(btn.dataset.maxCycles || 1);
            return;
          }
          if (action.type === 'stop_continuous') {
            postContinuousStop();
            return;
          }
          postAction(action);
        });
        row.appendChild(btn);
      }
      return row;
    }

    function discoveryChoiceRow() {
      const row = el('div', 'flow-actions');
      const choices = [
        { label: 'Quick Scan', text: '1', style: 'primary' },
        { label: 'Moderate Scan', text: '2', style: 'secondary' },
        { label: 'Deep Scan', text: '3', style: 'secondary' },
        { label: 'Skip Discovery', text: 'no', style: 'ghost' },
      ];
      for (const choice of choices) {
        const btn = el('button', choice.style === 'primary' ? 'primary' : (choice.style === 'ghost' ? 'ghost' : ''), choice.label);
        btn.type = 'button';
        if (continuousModeOwnsLifecycle(state.planner || {})) {
          btn.disabled = true;
          btn.title = 'Continuous mode is choosing discovery automatically.';
        }
        btn.addEventListener('click', () => {
          submitPromptThroughChat(choice.text);
        });
        row.appendChild(btn);
      }
      return row;
    }

    function planChoiceRow() {
      const row = el('div', 'flow-actions');
      const choices = [
        { label: 'Approve Plan', text: 'approve', style: 'primary' },
        { label: 'Reject Plan', text: 'reject', style: 'secondary' },
        { label: 'Reset Session', text: '/reset', style: 'ghost' },
      ];
      for (const choice of choices) {
        const btn = el('button', choice.style === 'primary' ? 'primary' : (choice.style === 'ghost' ? 'ghost' : ''), choice.label);
        btn.type = 'button';
        if (continuousModeOwnsLifecycle(state.planner || {})) {
          btn.disabled = true;
          btn.title = 'Continuous mode auto-approves or stops on policy gates.';
        }
        btn.addEventListener('click', () => {
          submitPromptThroughChat(choice.text);
        });
        row.appendChild(btn);
      }
      return row;
    }

    function flowCard({ title, body, meta = '', tone = '', actions = [], goals = [] }) {
      const card = el('div', 'flow-card ' + tone);
      card.appendChild(el('div', 'flow-title', title));
      if (body) { card.appendChild(el('div', 'flow-body', body)); }
      if (goals.length) {
        const list = el('div', 'compact-list');
        for (const g of goals.slice(0, 4)) {
          list.appendChild(el('div', 'compact-item', g.title || g.goal || 'Goal'));
        }
        card.appendChild(list);
      }
      if (meta) { card.appendChild(el('div', 'flow-meta', meta)); }
      const row = actionRow(actions);
      if (row) { card.appendChild(row); }
      return card;
    }

    function scopeBadge(scope) {
      const colors = { write: '#4e9a06', read: '#3465a4', validation: '#c18616', mixed: '#75507b' };
      return '<span style="display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;font-weight:600;background:' + (colors[scope] || colors.mixed) + '22;color:' + (colors[scope] || colors.mixed) + ';">' + (scope || 'write') + '</span>';
    }

    function buildContinuousModeCard(planner, actions) {
      const continuous = planner.continuous_mode || {};
      const worker = planner.worker_state || {};
      const active = isContinuousModeActive(planner);
      const hasStartAction = actions.some(a => a.type === 'start_continuous');
      const hasStopAction = actions.some(a => a.type === 'stop_continuous');
      if (!active && !hasStartAction && !hasStopAction && !continuous.stop_reason) { return null; }

      const card = el('div', 'flow-card lifecycle-card');
      card.appendChild(el('div', 'flow-title', 'Continuous Mode'));

      const status = String(continuous.status || (active ? 'running' : 'stopped'));
      const statusRow = el('div', 'phase-row');
      statusRow.appendChild(el('span', 'phase-label ' + (active ? 'active' : 'muted-phase'), active ? 'Active' : 'Stopped'));
      const cycle = Number(continuous.cycle || 0);
      const maxCycles = Number(continuous.max_cycles || 0);
      const cycleText = maxCycles > 0 ? ' cycle ' + cycle + '/' + maxCycles : '';
      statusRow.appendChild(el('span', 'phase-body', status + cycleText));
      card.appendChild(statusRow);

      const rows = [
        ['Issue', continuous.active_issue_id],
        ['Discovery', continuous.selected_discovery_mode],
        ['Review', continuous.latest_review_decision],
        ['Stop Reason', continuous.stop_reason],
      ];
      for (const [label, value] of rows) {
        if (!value) { continue; }
        const row = el('div', 'phase-row');
        row.appendChild(el('span', 'phase-label muted-phase', label));
        row.appendChild(el('span', 'phase-body', String(value)));
        card.appendChild(row);
      }

      const protectedPaths = Array.isArray(worker.protected_paths) ? worker.protected_paths : [];
      if (protectedPaths.length) {
        card.appendChild(el('div', 'flow-meta', 'Protected: ' + protectedPaths.join(', ')));
      }

      const followups = Array.isArray(continuous.created_followup_issue_ids) ? continuous.created_followup_issue_ids.filter(Boolean) : [];
      if (followups.length) {
        card.appendChild(el('div', 'flow-meta', 'Follow-up Issues'));
        const list = el('div', 'compact-list');
        for (const issueId of followups.slice(0, 6)) {
          list.appendChild(el('div', 'compact-item', String(issueId)));
        }
        card.appendChild(list);
      }

      const row = el('div', 'flow-actions');
      if (active || hasStopAction) {
        const stopAction = actions.find(a => a.type === 'stop_continuous') || { type: 'stop_continuous', label: 'Stop Continuous', source: 'planner' };
        const stop = el('button', 'secondary', stopAction.label || 'Stop Continuous');
        stop.type = 'button';
        stop.dataset.continuousAction = 'stop';
        stop.addEventListener('click', () => postContinuousStop());
        row.appendChild(stop);
      } else {
        const label = el('span', 'flow-meta', 'Cycles');
        const input = document.createElement('input');
        input.type = 'number';
        input.min = '1';
        input.max = '25';
        input.step = '1';
        input.value = String(Math.max(1, Number(continuous.max_cycles || 1)));
        const start = el('button', 'primary', 'Start Continuous');
        start.type = 'button';
        start.dataset.continuousAction = 'start';
        start.dataset.maxCyclesInput = 'true';
        start.addEventListener('click', () => postContinuousStart(input.value));
        row.appendChild(label);
        row.appendChild(input);
        row.appendChild(start);
      }
      card.appendChild(row);
      return card;
    }

    function buildUnifiedDiscoveryCard(planner, actions) {
      const pending = planner.pending_discovery;
      const result = planner.last_discovery;
      const hasPlanLifecycle = !!(planner.pending_plan || planner.executing || planner.last_execution_summary || planner.last_completed_plan || planner.last_presented_plan);
      if (!pending && !result && !hasPlanLifecycle) { return null; }

      const card = el('div', 'flow-card lifecycle-card');
      card.appendChild(el('div', 'flow-title', 'Discovery'));

      if (!pending && !result) {
        const phase = el('div', 'phase-row');
        phase.appendChild(el('span', 'phase-label muted-phase', 'Skipped'));
        phase.appendChild(el('span', 'phase-body', 'No discovery triggered, proceeding with planning.'));
        card.appendChild(phase);
        return card;
      }

      /* Phase 1: Suggested — always show the reason breadcrumb */
      const reason = (pending && pending.reason) || (result && result.reason) || '';
      if (reason) {
        const phase = el('div', 'phase-row');
        phase.appendChild(el('span', 'phase-label done', '\u2713 Suggested'));
        phase.appendChild(el('span', 'phase-body', reason));
        card.appendChild(phase);
      }

      const prompt = (pending && pending.prompt) || '';
      if (prompt) {
        const promptBlock = el('div', 'flow-body muted-block');
        promptBlock.textContent = prompt;
        card.appendChild(promptBlock);
      }

      /* Phase 2: User choice — show the mode if we have it */
      if (result && result.mode) {
        const phase = el('div', 'phase-row');
        phase.appendChild(el('span', 'phase-label done', '\u2713 ' + result.mode));
        const meta = [];
        if (result.duration_s) { meta.push(Math.round(result.duration_s) + 's'); }
        if (result.tool_calls_used != null) { meta.push(result.tool_calls_used + '/' + (result.tool_calls_max || '?') + ' tool calls'); }
        if (meta.length) { phase.appendChild(el('span', 'phase-meta', meta.join(' \u00b7 '))); }
        card.appendChild(phase);
      }

      const discoveryRunning = !!(pending && !result && discoveryTimeline.length);

      /* Still pending — show action buttons until work starts */
      if (pending && !result) {
        if (!discoveryRunning) {
          const row = discoveryChoiceRow();
          if (row) { card.appendChild(row); }
        }
        if (discoveryRunning) {
          const phase = el('div', 'phase-row');
          phase.appendChild(el('span', 'phase-label active', 'In Progress'));
          phase.appendChild(el('span', 'phase-body', 'Discovery is running.'));
          card.appendChild(phase);
          if (discoveryTimeline.length) {
            card.appendChild(renderTimeline(discoveryTimeline));
          }
        }
        /* Collapsible transcript detail */
        const lastMsg = (state.transcript || []).slice(-1)[0];
        if (!discoveryRunning && lastMsg && lastMsg.role !== 'user' && lastMsg.content && lastMsg.content.length > 100) {
          const details = document.createElement('details');
          details.className = 'card-details';
          const summary = document.createElement('summary');
          summary.textContent = 'Discovery options & details';
          const detailBody = el('div', 'card-details-body', lastMsg.content);
          details.appendChild(summary);
          details.appendChild(detailBody);
          card.appendChild(details);
        }
        return card;
      }

      /* Phase 3: Complete — final analysis */
      if (result && result.final_message) {
        const body = el('div', 'flow-body discovery-body');
        body.textContent = result.final_message;
        card.appendChild(body);
      }

      /* Collapsible breakdown */
      const hasDetails = (result && result.touched_paths && result.touched_paths.length) || (result && result.usage_summary) || discoveryTimeline.length;
      if (hasDetails) {
        const details = document.createElement('details');
        details.className = 'card-details';
        const summary = document.createElement('summary');
        summary.textContent = 'Breakdown';
        details.appendChild(summary);
        const body = el('div', 'card-details-body');
        if (discoveryTimeline.length) {
          body.appendChild(el('div', 'flow-meta', 'Actions taken'));
          body.appendChild(renderTimeline(discoveryTimeline));
        }
        if (result.touched_paths && result.touched_paths.length) {
          body.appendChild(el('div', 'flow-meta', 'Touched paths'));
          const pathsEl = el('div', 'compact-list');
          for (const p of result.touched_paths.slice(0, 6)) {
            const item = el('div', 'compact-item path', p);
            item.addEventListener('click', () => vscode.postMessage({ type: 'openPath', path: p }));
            pathsEl.appendChild(item);
          }
          if (result.touched_paths.length > 6) { pathsEl.appendChild(el('div', 'muted', '+ ' + (result.touched_paths.length - 6) + ' more')); }
          body.appendChild(pathsEl);
        }
        if (result.usage_summary) { body.appendChild(el('div', 'flow-meta', result.usage_summary)); }
        details.appendChild(body);
        card.appendChild(details);
      }
      return card;
    }

    function buildUnifiedPlanCard(planner, actions) {
      const pending = planner.pending_plan;
      const executing = planner.executing;
      const completed = planner.last_completed_plan;
      const executionSummary = planner.last_execution_summary;
      const plan = pending || planner.last_presented_plan || completed;
      const hasDiscoveryLifecycle = !!(planner.pending_discovery || planner.last_discovery);
      if (!plan && !executing && !executionSummary && !hasDiscoveryLifecycle) { return null; }

      const card = el('div', 'flow-card lifecycle-card');
      card.appendChild(el('div', 'flow-title', 'Plan'));

      const plannerTraceEntry = currentLifecycleEntry(planner);
      if (plannerTraceEntry) {
        card.appendChild(buildLifecycleHistoryNode(plannerTraceEntry));
      }

      if (!plan && !executing && !executionSummary) {
        const phase = el('div', 'phase-row');
        phase.appendChild(el('span', 'phase-label muted-phase', 'Skipped'));
        phase.appendChild(el('span', 'phase-body', 'No planning triggered.'));
        card.appendChild(phase);
        return card;
      }

      /* Plan summary — always shown if we have a plan */
      if (plan) {
        if (plan.summary) { card.appendChild(el('div', 'flow-body', plan.summary)); }
        if (plan.clarification_summary) { card.appendChild(el('div', 'muted', plan.clarification_summary)); }
        if (plan.assumptions && plan.assumptions.length) {
          card.appendChild(el('div', 'flow-meta', 'Assumptions: ' + plan.assumptions.join('; ')));
        }
      }

      /* Goals list */
      const goals = (plan && plan.goals) || [];
      const completedResults = planner.completed_results || [];
      const lastCompletedResults = planner.last_completed_results || [];
      const doneResults = executing ? completedResults : lastCompletedResults;
      const doneIds = new Set(doneResults.map(r => r.goal_id).filter(Boolean));

      if (goals.length) {
        const list = el('div', 'compact-list');
        for (let i = 0; i < goals.length; i++) {
          const g = goals[i];
          const isDone = doneIds.has(g.goal_id);
          const isCurrent = executing && planner.executing_goal_index === (i + 1);
          const result = doneResults.find(r => r.goal_id === g.goal_id);
          const item = el('div', 'compact-item' + (isDone ? ' goal-item-done' : '') + (isCurrent ? ' goal-item-active' : ''));
          const header = document.createElement('div');
          const prefix = isDone ? '\u2713 ' : (isCurrent ? '\u25b6 ' : '');
          const statusSuffix = result ? ' \u2014 ' + (result.status || '') : '';
          header.innerHTML = prefix + '<strong>' + (g.title || g.goal_id || 'Goal') + '</strong> ' + scopeBadge(g.estimated_scope) + (statusSuffix ? '<span class="muted">' + statusSuffix + '</span>' : '');
          item.appendChild(header);
          if (!isDone && g.goal) { item.appendChild(el('div', 'muted', g.goal)); }
          if (!isDone && g.success_signals && g.success_signals.length) {
            item.appendChild(el('div', 'flow-meta', '\u2713 ' + g.success_signals.join(' \u2713 ')));
          }
          list.appendChild(item);
        }
        card.appendChild(list);
      }

      /* Progress bar during execution */
      if (executing && typeof planner.executing_goal_index === 'number' && typeof planner.executing_goal_count === 'number' && planner.executing_goal_count > 0) {
        const pct = Math.max(0, Math.min(100, Math.round(((planner.executing_goal_index - 1) / planner.executing_goal_count) * 100)));
        const track = el('div', 'exec-progress');
        const bar = el('div', 'exec-bar');
        bar.style.width = pct + '%';
        track.appendChild(bar);
        card.appendChild(track);
      }

      /* Pending approval — action buttons */
      if (pending && !executing) {
        if (plan && plan.next_steps_preview && plan.next_steps_preview.length) {
          card.appendChild(el('div', 'flow-meta', 'Then: ' + plan.next_steps_preview.join(', ')));
        }
        const row = planChoiceRow();
        if (row) { card.appendChild(row); }
      }

      if ((executing || (!pending && executionSummary)) && planTimeline.length) {
        const details = document.createElement(executing ? 'div' : 'details');
        if (executing) {
          details.className = 'timeline-host';
          details.appendChild(renderTimeline(planTimeline));
          card.appendChild(details);
        } else {
          details.className = 'card-details';
          const summary = document.createElement('summary');
          summary.textContent = 'Execution trace';
          details.appendChild(summary);
          const body = el('div', 'card-details-body');
          body.appendChild(renderTimeline(planTimeline));
          details.appendChild(body);
          card.appendChild(details);
        }
      }

      const workerStatusCard = buildOperationalPlannerCard(planner);
      if (workerStatusCard) {
        card.appendChild(workerStatusCard);
      }

      /* Execution complete — summary */
      if (executionSummary && !executing && !pending) {
        const phase = el('div', 'phase-row');
        phase.appendChild(el('span', 'phase-label done', '\u2713 Complete'));
        phase.appendChild(el('span', 'phase-body', executionSummary));
        card.appendChild(phase);
      }

      return card;
    }

    function desiredDockTab(planner) {
      const plannerIssueState = planner.issue_state || {};
      const workerIssueState = (planner.worker_state || {}).issue_state || {};
      const hasIssueState = !!((workerIssueState.active_issue || plannerIssueState.active_issue) || ((workerIssueState.reopenable_issues || plannerIssueState.reopenable_issues || []).length));
      const discoveryActive = !!(planner.pending_discovery && !planner.last_discovery);
      if (isContinuousModeActive(planner)) { return 'continuous'; }
      if (discoveryActive) { return 'discovery'; }
      if (planner.pending_plan || planner.executing || planner.last_execution_summary || planner.last_completed_plan || planner.last_presented_plan) { return 'plan'; }
      if (planner.last_discovery) { return 'discovery'; }
      if (hasIssueState) { return 'issues'; }
      return activeDockTab;
    }

    function buildLifecycleDock(planner, actions) {
      const tabs = [];
      const continuousCard = buildContinuousModeCard(planner, actions);
      const discoveryCard = buildUnifiedDiscoveryCard(planner, actions);
      const planCard = buildUnifiedPlanCard(planner, actions);
      const issuesCard = buildIssuesCard(planner, actions);
      const factsCard = buildFactsCard(planner);
      const runtimeCard = buildRuntimeCard();
      if (continuousCard) { tabs.push({ id: 'continuous', label: 'Auto', node: continuousCard }); }
      if (discoveryCard) { tabs.push({ id: 'discovery', label: 'Discovery', node: discoveryCard }); }
      if (planCard) { tabs.push({ id: 'plan', label: 'Plan', node: planCard }); }
      if (issuesCard) { tabs.push({ id: 'issues', label: 'Issues', node: issuesCard }); }
      if (discoveryCard || planCard || factsCard || factsBadgeCount > 0) {
        const fallbackFactsCard = el('div', 'flow-card lifecycle-card');
        fallbackFactsCard.appendChild(el('div', 'flow-title', 'Run Facts'));
        fallbackFactsCard.appendChild(el('div', 'flow-body muted-block', 'No active run facts.'));
        tabs.push({ id: 'facts', label: 'Run Facts', badge: factsBadgeCount, node: factsCard || fallbackFactsCard });
      }
      if (runtimeCard) { tabs.push({ id: 'runtime', label: 'Runtime', node: runtimeCard }); }
      return tabs;
    }

    function renderDockActions(planner, actions) {
      dockActionsEl.innerHTML = '';
      const active = isContinuousModeActive(planner);
      const hasStartAction = actions.some(a => a.type === 'start_continuous');
      const hasStopAction = actions.some(a => a.type === 'stop_continuous');
      if (active || hasStopAction) {
        const stop = el('button', 'secondary', 'Stop Auto');
        stop.type = 'button';
        stop.dataset.continuousAction = 'stop';
        dockActionsEl.appendChild(stop);
        return;
      }
      if (!hasStartAction) {
        return;
      }
      const input = document.createElement('input');
      input.type = 'number';
      input.min = '1';
      input.max = '25';
      input.step = '1';
      input.value = '1';
      const start = el('button', 'primary', 'Start Auto');
      start.type = 'button';
      start.dataset.continuousAction = 'start';
      dockActionsEl.appendChild(input);
      dockActionsEl.appendChild(start);
    }

    function renderLifecycleDock() {
      const planner = state.planner || {};
      const actions = combineSuggestedActions(state).map(a => ({ ...a, _source: a.source }));
      const tabs = buildLifecycleDock(planner, actions);

      const activePanelBefore = dockPanelsEl.querySelector('.dock-panel.active');
      if (activePanelBefore && activeDockTab) {
        const activeScroller = activePanelBefore.querySelector('.flow-card');
        dockScrollTopByTab[activeDockTab] = activeScroller ? activeScroller.scrollTop : activePanelBefore.scrollTop;
      }

      dockTabsEl.innerHTML = '';
      dockActionsEl.innerHTML = '';
      dockPanelsEl.innerHTML = '';

      if (!tabs.length) {
        lifecycleDockEl.classList.remove('visible');
        return;
      }

      lifecycleDockEl.classList.add('visible');
      renderDockActions(planner, actions);
      const wantedTab = desiredDockTab(planner);
      if (!tabs.some(tab => tab.id === activeDockTab)) {
        activeDockTab = tabs[0].id;
      }
      if (wantedTab !== lastAutoDockTab && tabs.some(tab => tab.id === wantedTab)) {
        activeDockTab = wantedTab;
      }
      lastAutoDockTab = wantedTab;

      for (const tab of tabs) {
        const button = el('button', 'dock-tab' + (tab.id === activeDockTab ? ' active' : ''));
        button.type = 'button';
        button.appendChild(el('span', '', tab.label));
        if (tab.badge) {
          button.appendChild(el('span', 'dock-tab-badge', '+' + tab.badge));
        }
        button.addEventListener('click', () => {
          const currentPanel = dockPanelsEl.querySelector('.dock-panel.active');
          if (currentPanel && activeDockTab) {
            const currentScroller = currentPanel.querySelector('.flow-card');
            dockScrollTopByTab[activeDockTab] = currentScroller ? currentScroller.scrollTop : currentPanel.scrollTop;
          }
          activeDockTab = tab.id;
          if (tab.id === 'facts') { factsBadgeCount = 0; }
          render();
        });
        dockTabsEl.appendChild(button);

        const panel = el('div', 'dock-panel' + (tab.id === activeDockTab ? ' active' : ''));
        panel.appendChild(tab.node);
        dockPanelsEl.appendChild(panel);

        if (tab.id === activeDockTab && dockScrollTopByTab[tab.id] != null) {
          const targetScrollTop = dockScrollTopByTab[tab.id];
          const scroller = panel.querySelector('.flow-card');
          if (scroller) {
            // Force synchronous layout so scrollHeight is available
            void scroller.scrollHeight;
            scroller.scrollTop = targetScrollTop;
            // Auto-follow: if user was near the bottom, snap to new bottom
            if (scroller.scrollHeight - targetScrollTop - scroller.clientHeight < 60) {
              scroller.scrollTop = scroller.scrollHeight;
            }
          } else {
            panel.scrollTop = targetScrollTop;
          }
        }
      }
    }

    /* Build inline state cards only for non-empty, actionable state */
    function buildStateCards() {
      const planner = state.planner || {};
      const worker = planner.worker_state || {};
      const actions = combineSuggestedActions(state).map(a => ({ ...a, _source: a.source }));
      const cards = [];
      const hasDockVisible = lifecycleDockEl.classList.contains('visible');

      /* Active error */
      if (worker.active_error && worker.active_error.message) {
        cards.push(flowCard({
          title: 'Error',
          body: truncate(worker.active_error.message, 280),
          tone: 'error-card',
          actions,
        }));
      }

      /* Pending verification */
      if (worker.pending_verification && worker.pending_verification.path && !hasDockVisible) {
        cards.push(flowCard({
          title: 'Verification Pending',
          body: worker.pending_verification.path + ' needs validation.',
          tone: 'warning-card',
          actions,
        }));
      }

      /* Diagnostics - only when present */
      const diag = worker.latest_diagnostics;
      if (diag && Array.isArray(diag.diagnostics) && diag.diagnostics.length) {
        const card = el('div', 'flow-card error-card');
        card.appendChild(el('div', 'flow-title', 'Diagnostics'));
        card.appendChild(el('div', 'flow-body', (diag.diagnostic_engine || 'diagnostics') + ': ' + (diag.message || 'Issues detected')));
        for (const item of diag.diagnostics.slice(0, 6)) {
          const p = item.path || diag.path || '';
          const loc = item.line ? ':' + item.line : '';
          const code = item.code ? '[' + item.code + '] ' : '';
          const entry = el('div', p ? 'path' : 'muted', (p ? p + loc + ' ' : '') + code + (item.message || ''));
          if (p) { entry.addEventListener('click', () => vscode.postMessage({ type: 'openPath', path: p, line: item.line, column: item.column })); }
          card.appendChild(entry);
        }
        cards.push(card);
      }

      /* Review - only when present */
      const review = worker.latest_review;
      if (review && review.action_type && !hasDockVisible) {
        const card = el('div', 'flow-card');
        card.appendChild(el('div', 'flow-title', 'Review'));
        card.appendChild(el('div', 'flow-body', (review.summary || review.action_type) + (review.step ? ' [step ' + review.step + ']' : '')));
        const report = buildReviewReport(review);
        const reviewPath = primaryPathForReview(review);
        if (report || reviewPath) {
          const ia = el('div', 'inline-actions');
          if (report) {
            const btn = el('button', '', 'Open Report');
            btn.addEventListener('click', () => vscode.postMessage({ type: 'openReport', title: report.title, content: report.content, language: report.language }));
            ia.appendChild(btn);
          }
          if (reviewPath) {
            const btn = el('button', '', 'Open Diff');
            btn.addEventListener('click', () => vscode.postMessage({ type: 'openFileDiff', path: reviewPath }));
            ia.appendChild(btn);
          }
          card.appendChild(ia);
        }
        cards.push(card);
      }

      /* Edit batch - only when active */
      const batch = worker.edit_batch || {};
      if (batch.active && Array.isArray(batch.pending_paths) && batch.pending_paths.length && !hasDockVisible) {
        cards.push(flowCard({
          title: 'Edit Batch',
          body: batch.pending_paths.length + ' file(s) pending: ' + batch.pending_paths.join(', '),
          tone: 'warning-card',
        }));
      }

      /* Completion summary — suppress when lifecycle dock is handling it */
      if (planner.last_execution_summary && !cards.length && !hasDockVisible) {
        cards.push(flowCard({
          title: 'Outcome',
          body: truncate(planner.last_execution_summary, 280),
          actions,
        }));
      }

      /* Fallback: actions with no other state — suppress when dock handles actions */
      const lifecycleActions = new Set(['approve_plan', 'reject_plan', 'reset', 'discovery_quick', 'discovery_moderate', 'discovery_deep', 'skip_discovery', 'reopen_issue', 'start_continuous', 'stop_continuous']);
      const nonLifecycleActions = actions.filter(a => !lifecycleActions.has(a.type));
      if (!cards.length && nonLifecycleActions.length && state.last_message && !hasDockVisible) {
        cards.push(flowCard({
          title: statusLabel(),
          body: truncate(state.last_message, 280),
          actions: nonLifecycleActions,
        }));
      }

      return cards;
    }

    function renderBubble(item) {
      const content = String(item.content || '');
      const isLong = item.role !== 'user' && content.length > 360;
      if (!isLong) {
        return el('div', 'bubble ' + (item.role === 'user' ? 'user' : 'assistant'), content);
      }
      const details = document.createElement('details');
      details.className = 'bubble assistant';
      const summary = document.createElement('summary');
      summary.textContent = truncate(content, 180);
      const body = el('div', 'bubble-details-body', content);
      details.appendChild(summary);
      details.appendChild(body);
      return details;
    }

    function render() {
      backendModeEl.textContent = backendModeLabel();
      runtimeEl.textContent = runtimeLabel();
      skillsBadgeEl.textContent = skillsBadgeLabel();
      statusEl.textContent = statusLabel();
      feedEl.innerHTML = '';
      renderLifecycleDock();
      renderRuntimeDebug();

      const transcript = state.transcript || [];
      const stateCards = buildStateCards();
      const hasDock = lifecycleDockEl.classList.contains('visible');

      if (!transcript.length && !stateCards.length && !hasDock) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('h2', '', 'No conversation yet'));
        empty.appendChild(el('p', '', 'Send a message to begin.'));
        feedEl.appendChild(empty);
        feedEl.scrollTop = feedEl.scrollHeight;
        return;
      }

      /* Filter transcript: when lifecycle cards are visible, keep the transcript up to the last user turn only. */
      const planner = state.planner || {};
      const hasLifecycleCard = !!(isContinuousModeActive(planner) || planner.pending_discovery || planner.last_discovery || planner.pending_plan || planner.executing || planner.last_execution_summary);
      let items = transcript;
      if (hasLifecycleCard && transcript.length) {
        let lastUserIndex = -1;
        for (let i = transcript.length - 1; i >= 0; i -= 1) {
          if (transcript[i] && transcript[i].role === 'user') {
            lastUserIndex = i;
            break;
          }
        }
        const baseItems = lastUserIndex >= 0 ? transcript.slice(0, lastUserIndex + 1) : transcript;
        items = baseItems.filter(item => !isLifecycleChoiceMessage(item) && !isLifecycleAssistantMessage(item));
      }

      for (const item of items) {
        feedEl.appendChild(renderBubble(item));
      }

      if (hasLifecycleCard) {
        for (const item of buildInlineLifecycleItems(planner, hasDock)) {
          feedEl.appendChild(item);
        }
      }

      for (const card of stateCards) {
        feedEl.appendChild(card);
      }

      feedEl.scrollTop = feedEl.scrollHeight;
    }

    promptForm.addEventListener('submit', (event) => {
      event.preventDefault();
      if (pendingAction) { return; }
      const text = promptInput.value.trim();
      if (!text) { return; }
      const submitted = submitPromptThroughChat(text);
      if (submitted) {
        promptInput.value = '';
      }
    });

    providerSelect.addEventListener('change', () => {
      const providerKey = String(providerSelect.value || '').trim();
      if (!providerKey) { return; }
      const option = findProviderOption(providerKey);
      const current = currentRuntimeConfig();
      const preferredModel = current.provider === providerKey ? current.model : String(option?.default_model || '');
      syncModelOptionsForProvider(providerKey, preferredModel);
      syncRuntimeControls();
      scheduleRuntimeSwitch(0);
    });

    modelSelect.addEventListener('change', () => {
      updateCustomModelVisibility();
      if (String(modelSelect.value || '').trim() !== '__custom__') {
        scheduleRuntimeSwitch(0);
      }
    });

    customModelInput.addEventListener('input', () => {
      if (String(modelSelect.value || '').trim() === '__custom__') {
        scheduleRuntimeSwitch(450);
      }
    });

    customModelInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        if (runtimeApplyTimer) {
          clearTimeout(runtimeApplyTimer);
          runtimeApplyTimer = null;
        }
        triggerRuntimeSwitch();
      }
    });

    backoffEnabled.addEventListener('change', () => {
      const on = backoffEnabled.checked;
      backoffLimitK.disabled = !on;
      if (!on) {
        vscode.postMessage({ type: 'configureBackoff', enabled: false, tokenLimitK: 0 });
        backoffWindow.textContent = '';
      } else {
        const k = parseInt(backoffLimitK.value, 10);
        if (k > 0) {
          vscode.postMessage({ type: 'configureBackoff', enabled: true, tokenLimitK: k });
        }
      }
    });

    backoffLimitK.addEventListener('change', () => {
      const k = parseInt(backoffLimitK.value, 10);
      if (backoffEnabled.checked && k > 0) {
        vscode.postMessage({ type: 'configureBackoff', enabled: true, tokenLimitK: k });
      }
    });

    document.addEventListener('click', (event) => {
      const target = event.target;
      if (!(target instanceof Element)) { return; }
      const button = target.closest('[data-continuous-action]');
      if (!(button instanceof HTMLElement)) { return; }
      const action = String(button.dataset.continuousAction || '').trim();
      if (!action) { return; }
      event.preventDefault();
      event.stopPropagation();
      if (action === 'start') {
        const row = button.closest('.flow-actions');
        const input = row ? row.querySelector('input[type="number"]') : null;
        const cycles = input instanceof HTMLInputElement ? input.value : (button.dataset.maxCycles || 1);
        postContinuousStart(cycles);
      } else if (action === 'stop') {
        postContinuousStop();
      }
    }, true);

    window.addEventListener('message', (event) => {
      const message = event.data;
      pushDebugEvent(message?.type || 'unknown', message);
      if (message?.type === 'goal_start') {
        if (message.state) {
          const previousPlanner = state.planner || {};
          state = message.state || state;
          recordLifecycleTransitions(previousPlanner, state.planner || {});
        }
        unlockButtons();
        planTimeline.push({ kind: 'goal', done: false, message });
        render();
        return;
      }
      if (message?.type === 'goal_finish') {
        if (message.state) {
          const previousPlanner = state.planner || {};
          state = message.state || state;
          recordLifecycleTransitions(previousPlanner, state.planner || {});
        }
        unlockButtons();
        planTimeline.push({ kind: 'goal', done: true, message });
        render();
        return;
      }
      if (message?.type === 'progress') {
        const previousPlanner = state.planner || {};
        const actionTypeAtDispatch = pendingActionType;
        if (message.state) {
          state = message.state || state;
          recordLifecycleTransitions(previousPlanner, state.planner || {});
        }
        unlockButtons();
        const planner = (message.state && message.state.planner) || state.planner || {};
        const domain = String(message.domain || '').trim();
        const target = domain === 'discovery' || domain === 'plan' ? domain : progressTimelineTarget(planner, actionTypeAtDispatch);
        if (target === 'plan' && ['plan_presented', 'plan_approved', 'plan_revision_requested', 'plan_rejected'].includes(String(message.action_type || ''))) {
          planTimeline = [];
        }
        if (target === 'plan') {
          planTimeline.push({ kind: 'step', message });
        } else if (target === 'discovery') {
          discoveryTimeline.push({ kind: 'step', message });
        }
        render();
        return;
      }
      if (message?.type === 'actionStatus') {
        unlockButtons();
        render();
        return;
      }
      if (message?.type === 'state') {
        unlockButtons();
        const previousPlanner = state.planner || {};
        state = message.state || state;
        const planner = state.planner || {};
        recordLifecycleTransitions(previousPlanner, planner);
        const facts = (((planner || {}).worker_state || {}).current_run_facts) || [];
        const nextFactKeys = Array.from(new Set(facts.map(f => String(f.key || '').trim()).filter(Boolean))).sort();
        const previousFactKeys = new Set(lastFactKeys);
        const newFactKeyCount = nextFactKeys.filter(key => !previousFactKeys.has(key)).length;
        if (lastFactKeys.length && newFactKeyCount > 0 && activeDockTab !== 'facts') {
          factsBadgeCount += newFactKeyCount;
        }
        lastFactKeys = nextFactKeys;
        if (planner.pending_discovery && !previousPlanner.pending_discovery && !planner.last_discovery) {
          discoveryTimeline = [];
        }
        if (!planner.pending_discovery && !planner.last_discovery) {
          discoveryTimeline = [];
        }
        if (planner.executing && !previousPlanner.executing) {
          planTimeline = [];
        }
        if (!planner.pending_plan && !planner.executing && !planner.last_execution_summary && !planner.last_completed_plan) {
          planTimeline = [];
        }
        unlockButtons();
        if (runtimeSwitchPending) {
          runtimeSwitchPending = false;
          setRuntimeStatus('success', 'Runtime updated');
        }
        render();
        syncRuntimeControls();
        return;
      }
      if (message?.type === 'runtimeOptions') {
        runtimeOptions = message.options || runtimeOptions;
        setRuntimeControlsDisabled(false);
        syncRuntimeControls();
        render();
        return;
      }
      if (message?.type === 'backendInfo') {
        backendInfo = message.info || backendInfo;
        render();
        return;
      }
      if (message?.type === 'runtimeStatus') {
        runtimeSwitchPending = false;
        unlockButtons();
        setRuntimeStatus(String(message.level || ''), String(message.message || ''));
        return;
      }
      if (message?.type === 'backoffStatus') {
        syncBackoffControls(message.backoff || {});
        return;
      }
    });

    recordLifecycleTransitions({}, state.planner || {});
    setRuntimeControlsDisabled(true);
    setRuntimeStatus('', '');
    render();
  </script>
</body>
</html>`;
  }

  public dispose(): void {
    for (const disposable of this.disposables) {
      disposable.dispose();
    }
    this.panel?.dispose();
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const bridge = new skillzAgentBridge(context);
  const panel = new AgentPanel(context, bridge);
  const diagnostics = vscode.languages.createDiagnosticCollection('python-agent');
  context.subscriptions.push(bridge, panel, diagnostics);
  void bridge.syncConfiguration().catch(() => {
    // Lazy startup entrypoints will surface concrete setting or launch failures later.
  });

  context.subscriptions.push(
    bridge.onDidUpdateState((state) => {
      syncDiagnosticsCollection(state, diagnostics);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('skillzAgent.openAgent', async () => {
      await panel.show();
    })
  );

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration(async (event) => {
      const providerChanged = event.affectsConfiguration('skillzAgent.provider');
      const modelChanged = event.affectsConfiguration('skillzAgent.model');
      const backendScriptChanged = event.affectsConfiguration('skillzAgent.backendScript');
      if (!providerChanged && !modelChanged && !backendScriptChanged) {
        return;
      }
      if (backendScriptChanged) {
        if (bridge.isRunning()) {
          await bridge.stopBackend();
          if (panel.isOpen()) {
            await panel.reconnectBackend();
          }
          void vscode.window.showInformationMessage('Python Agent backend script changed. The backend was restarted with the selected runtime.');
        } else {
          await panel.syncBackendInfo();
        }
      } else {
        await panel.syncBackendInfo();
      }
      if (!providerChanged && !modelChanged) {
        return;
      }
      if (suppressConfigDrivenRuntimeUpdateCount > 0) {
        suppressConfigDrivenRuntimeUpdateCount -= 1;
        return;
      }
      if (!bridge.isRunning()) {
        return;
      }
      const config = skillzAgentConfig(primaryWorkspaceFolder()?.uri);
      const provider = String(config.get<string>('provider') || 'gemini').trim();
      const model = String(config.get<string>('model') || defaultModelForProvider(provider)).trim() || defaultModelForProvider(provider);
      try {
        const response = await bridge.reconfigureRuntime(provider, model);
        if (!response.ok) {
          throw new Error(response.message || 'Unknown runtime update failure');
        }
        void vscode.window.showInformationMessage(`Python Agent runtime updated to ${provider} / ${model}.`);
      } catch (error) {
        void vscode.window.showErrorMessage(`Python Agent runtime update failed: ${String(error)}`);
      }
    })
  );
}

export function deactivate(): void {
  // Nothing explicit; disposables are registered on activate.
}
