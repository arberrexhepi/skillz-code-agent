import { createContext, useContext } from 'react';
import type { AgentUiState } from '../../../shared/agentCore';
import type { AgentStartOptions } from '../../../shared/contracts';
import type { AgentBackoff, JsonMap, SuggestedAction } from '../../../shared/agentTypes';

export interface RuntimeSelection extends AgentStartOptions {
  backendScript: string;
}

export interface AgentWorkspaceValue {
  state: AgentUiState;
  runtime: RuntimeSelection;
  backoff?: AgentBackoff;
  setRuntime: (runtime: Partial<RuntimeSelection>) => void;
  start: () => Promise<boolean>;
  stop: () => Promise<void>;
  submit: (text: string) => Promise<boolean>;
  plannerAction: (action: string, extras?: JsonMap) => Promise<boolean>;
  createIssue: (summary: string) => Promise<void>;
  workerAction: (action: JsonMap) => Promise<boolean>;
  runSuggestedAction: (action: SuggestedAction) => Promise<boolean>;
  switchRuntime: (provider: string, model: string) => Promise<boolean>;
  setBackoff: (enabled: boolean, tokenLimitK: number) => Promise<boolean>;
  clearNotice: () => void;
}

export const AgentWorkspaceContext = createContext<AgentWorkspaceValue | null>(null);

export function useAgentWorkspace(): AgentWorkspaceValue {
  const value = useContext(AgentWorkspaceContext);
  if (!value) throw new Error('useAgentWorkspace must be used inside AgentWorkspaceProvider.');
  return value;
}
