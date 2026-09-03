import type { ArtifactsApi } from './artifacts';
import type { WorkspaceIssuesSnapshot } from './workspaceIssues';
import type { RepoFactsSnapshot } from './repoFacts';

export interface WorkspaceInfo {
  root: string;
  name: string;
}

export interface FileEntry {
  name: string;
  path: string;
  kind: 'file' | 'directory';
}

export interface FileDocument {
  path: string;
  content: string;
  language: string;
  modifiedAt: number;
}

export interface GitFileStatus {
  path: string;
  indexStatus: string;
  workTreeStatus: string;
  originalPath?: string;
}

export interface GitStatus {
  isRepository: boolean;
  branch: string;
  upstream?: string;
  ahead: number;
  behind: number;
  files: GitFileStatus[];
}

export interface GitFileDiff {
  path: string;
  original: string;
  modified: string;
  language: string;
}

export interface GitDiscardResult {
  status: GitStatus;
  discarded: boolean;
}

export interface GitCommit {
  hash: string;
  shortHash: string;
  subject: string;
  body: string;
  authorName: string;
  authorEmail: string;
  authoredAt: string;
  parents: string[];
}

export interface AgentStartOptions {
  provider: string;
  model: string;
  backendScript?: string;
}

import type { AgentBackoff, AgentBridgeState, AgentProgressMessage, CodexSubscriptionStatus, JsonMap, RuntimeOptionsPayload } from './agentTypes';
export type { AgentBridgeState } from './agentTypes';

export interface AgentResponse {
  id?: string;
  ok: boolean;
  message?: string;
  state: AgentBridgeState;
  backoff?: AgentBackoff;
  runtime_options?: RuntimeOptionsPayload;
  [key: string]: unknown;
}

export type AgentEvent =
  | { type: 'state'; state: AgentBridgeState }
  | { type: 'progress'; payload: AgentProgressMessage }
  | { type: 'status'; status: 'stopped' | 'starting' | 'running' | 'error'; message?: string }
  | { type: 'stderr'; message: string };

export type TerminalEvent =
  | { type: 'data'; sessionId: string; data: string }
  | { type: 'exit'; sessionId: string; exitCode: number };

export interface TerminalCreateOptions {
  cols: number;
  rows: number;
}

export interface WorkbenchApi {
  artifacts: ArtifactsApi;
  workspace: {
    current(): Promise<WorkspaceInfo | null>;
    choose(): Promise<WorkspaceInfo | null>;
    open(root: string): Promise<WorkspaceInfo>;
    list(path?: string): Promise<FileEntry[]>;
    read(path: string): Promise<FileDocument>;
    repoFacts(workspaceRoot: string): Promise<RepoFactsSnapshot>;
    issues(workspaceRoot: string): Promise<WorkspaceIssuesSnapshot>;
    issueAction(workspaceRoot: string, action: 'create_issue' | 'close_issue' | 'reopen_issue', extras: JsonMap): Promise<WorkspaceIssuesSnapshot>;
    write(path: string, content: string): Promise<FileDocument>;
    onChange(listener: (paths: string[]) => void): () => void;
  };
  git: {
    status(): Promise<GitStatus>;
    initialize(workspaceRoot: string): Promise<GitStatus>;
    history(limit?: number): Promise<GitCommit[]>;
    fileDiff(path: string, staged?: boolean): Promise<GitFileDiff>;
    stage(paths: string[]): Promise<GitStatus>;
    stageAll(): Promise<GitStatus>;
    unstage(paths: string[]): Promise<GitStatus>;
    discard(path: string): Promise<GitDiscardResult>;
    commit(message: string): Promise<GitStatus>;
    push(): Promise<GitStatus>;
  };
  terminal: {
    create(options: TerminalCreateOptions): Promise<string>;
    write(sessionId: string, data: string): void;
    resize(sessionId: string, cols: number, rows: number): void;
    dispose(sessionId: string): void;
    onEvent(listener: (event: TerminalEvent) => void): () => void;
  };
  agent: {
    start(options: AgentStartOptions): Promise<AgentResponse>;
    submit(text: string): Promise<AgentResponse>;
    plannerAction(action: string, extras?: JsonMap): Promise<AgentResponse>;
    workerAction(action: JsonMap): Promise<AgentResponse>;
    reconfigureRuntime(provider: string, model: string): Promise<AgentResponse>;
    configureBackoff(enabled: boolean, tokenLimitK: number): Promise<AgentResponse>;
    runtimeOptions(provider?: string, model?: string): Promise<RuntimeOptionsPayload>;
    codexSubscriptionStatus(): Promise<CodexSubscriptionStatus>;
    codexSubscriptionLogin(): Promise<CodexSubscriptionStatus>;
    chooseCodexCli(): Promise<string | null>;
    setCodexCliPath(path: string | null): Promise<CodexSubscriptionStatus>;
    stop(): Promise<void>;
    onEvent(listener: (event: AgentEvent) => void): () => void;
  };
}
