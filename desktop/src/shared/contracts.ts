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

export interface AgentStartOptions {
  provider: string;
  model: string;
  backendScript?: string;
}

export interface AgentTranscriptEntry {
  role: string;
  content: string;
}

export interface AgentBridgeState {
  planner: Record<string, unknown>;
  transcript: AgentTranscriptEntry[];
  last_message?: string;
  bridge_warning?: string;
}

export interface AgentResponse {
  id?: string;
  ok: boolean;
  message?: string;
  state: AgentBridgeState;
  [key: string]: unknown;
}

export type AgentEvent =
  | { type: 'state'; state: AgentBridgeState }
  | { type: 'progress'; payload: Record<string, unknown> }
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
  workspace: {
    current(): Promise<WorkspaceInfo | null>;
    choose(): Promise<WorkspaceInfo | null>;
    open(root: string): Promise<WorkspaceInfo>;
    list(path?: string): Promise<FileEntry[]>;
    read(path: string): Promise<FileDocument>;
    write(path: string, content: string): Promise<FileDocument>;
    onChange(listener: (paths: string[]) => void): () => void;
  };
  git: {
    status(): Promise<GitStatus>;
    fileDiff(path: string, staged?: boolean): Promise<GitFileDiff>;
    stage(paths: string[]): Promise<GitStatus>;
    unstage(paths: string[]): Promise<GitStatus>;
    commit(message: string): Promise<GitStatus>;
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
    plannerAction(action: string, extras?: Record<string, unknown>): Promise<AgentResponse>;
    stop(): Promise<void>;
    onEvent(listener: (event: AgentEvent) => void): () => void;
  };
}
