export type ProcessStatus = 'starting' | 'running' | 'stopping' | 'stopped' | 'failed';

export interface CommandCandidate {
  id: string;
  label: string;
  command: string;
  args: string[];
  source: 'package.json';
  kind: 'server' | 'task';
  primary: boolean;
}

export interface ManagedProcess {
  id: string;
  rootId: string;
  candidateId: string;
  pid?: number;
  status: ProcessStatus;
  startedAt: string;
  stoppedAt?: string;
  exitCode?: number | null;
  signal?: string | null;
  ports: number[];
  logs: string;
}

export interface Repository {
  id: string;
  label: string;
  readOnly: boolean;
  candidates: CommandCandidate[];
  processes: ManagedProcess[];
  error?: string;
}

export interface Inventory {
  repositories: Repository[];
  limits: {
    concurrentProcesses: number;
    logBytes: number;
  };
}

async function checked(url: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({})) as { error?: string };
    throw new Error(detail.error || 'Server Manager request failed.');
  }
  return response;
}

export const manager = {
  inventory: async (): Promise<Inventory> =>
    (await checked('/manager/repositories')).json(),

  start: async (rootId: string, candidateId: string): Promise<{ process: ManagedProcess }> =>
    (await checked(`/manager/repositories/${encodeURIComponent(rootId)}/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ candidateId }),
    })).json(),

  stop: async (processId: string): Promise<{ process: ManagedProcess }> =>
    (await checked(`/manager/processes/${encodeURIComponent(processId)}/stop`, {
      method: 'POST',
    })).json(),
};
