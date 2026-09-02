import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { initialAgentUiState, reduceAgentUi } from '../../../shared/agentCore';
import { createInactiveIssue } from '../../../shared/issueCreation';
import type { AgentEvent, AgentResponse } from '../../../shared/contracts';
import type { AgentBackoff, JsonMap, SuggestedAction } from '../../../shared/agentTypes';
import { AgentWorkspaceContext, type AgentWorkspaceValue, type RuntimeSelection } from './agentWorkspace';

const defaultRuntime: RuntimeSelection = {
  provider: 'gemini',
  model: 'gemini-3-flash-preview',
  backendScript: 'main.py',
};

export function AgentWorkspaceProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const [state, dispatch] = useReducer(reduceAgentUi, initialAgentUiState);
  const [runtime, setRuntimeState] = useState(defaultRuntime);
  const [backoff, setBackoffState] = useState<AgentBackoff>();
  const starting = useRef<Promise<boolean> | null>(null);

  useEffect(() => window.workbench.agent.onEvent((event: AgentEvent) => {
    if (event.type === 'state') dispatch({ type: 'bridge-state', state: event.state });
    if (event.type === 'progress') dispatch({ type: 'progress', progress: event.payload });
    if (event.type === 'status') dispatch({ type: 'status', status: event.status, message: event.message });
    if (event.type === 'stderr') dispatch({ type: 'notice', message: event.message });
  }), []);

  const accept = useCallback((response: AgentResponse): boolean => {
    dispatch({ type: 'bridge-state', state: response.state });
    if (response.backoff) setBackoffState(response.backoff);
    if (!response.ok) dispatch({ type: 'notice', message: response.message || 'Agent request failed.' });
    return response.ok;
  }, []);

  const run = useCallback(async (name: string, task: () => Promise<AgentResponse>): Promise<boolean> => {
    dispatch({ type: 'pending', action: name });
    dispatch({ type: 'notice', message: '' });
    try {
      return accept(await task());
    } catch (cause) {
      dispatch({ type: 'notice', message: cleanError(cause) });
      return false;
    } finally {
      dispatch({ type: 'pending', action: '' });
    }
  }, [accept]);

  const loadRuntimeOptions = useCallback(async (provider: string, model: string): Promise<void> => {
    try {
      const options = await window.workbench.agent.runtimeOptions(provider, model);
      dispatch({ type: 'runtime-options', options });
      setRuntimeState((current) => ({
        ...current,
        provider: options.current_provider || current.provider,
        model: options.current_model || current.model,
      }));
    } catch {
      // Some older bridge builds do not expose runtime discovery; manual controls remain usable.
    }
  }, []);

  useEffect(() => { void loadRuntimeOptions(defaultRuntime.provider, defaultRuntime.model); }, [loadRuntimeOptions]);

  const start = useCallback(async (): Promise<boolean> => {
    dispatch({ type: 'pending', action: 'start' });
    dispatch({ type: 'notice', message: '' });
    try {
      const ok = accept(await window.workbench.agent.start(runtime));
      if (ok) await loadRuntimeOptions(runtime.provider, runtime.model);
      return ok;
    } catch (cause) {
      dispatch({ type: 'status', status: 'error', message: cleanError(cause) });
      return false;
    } finally {
      dispatch({ type: 'pending', action: '' });
    }
  }, [accept, loadRuntimeOptions, runtime]);

  const ensureRunning = useCallback(async (): Promise<boolean> => {
    if (state.status === 'running') return true;
    if (!starting.current) starting.current = start().finally(() => { starting.current = null; });
    return starting.current;
  }, [start, state.status]);

  const submit = useCallback(async (text: string): Promise<boolean> => {
    if (!(await ensureRunning())) return false;
    return run('submit', () => window.workbench.agent.submit(text));
  }, [ensureRunning, run]);

  const plannerAction = useCallback(async (action: string, extras: JsonMap = {}): Promise<boolean> => {
    if (!(await ensureRunning())) return false;
    return run(action, () => window.workbench.agent.plannerAction(action, extras));
  }, [ensureRunning, run]);

  const createIssue = useCallback(async (summary: string): Promise<void> => {
    if (!(await ensureRunning())) throw new Error('Could not start the agent. Check the runtime settings and retry; your issue details have been kept.');
    // The bridge queues this behind any running action. Do not use run(): its
    // pending/finally updates would overwrite the execution action's UI state.
    accept(await createInactiveIssue(window.workbench.agent, summary));
  }, [accept, ensureRunning]);

  const workerAction = useCallback(async (action: JsonMap): Promise<boolean> => {
    if (!(await ensureRunning())) return false;
    return run(String(action.type || 'worker_action'), () => window.workbench.agent.workerAction(action));
  }, [ensureRunning, run]);

  const runSuggestedAction = useCallback(async (action: SuggestedAction): Promise<boolean> => {
    if (action.requires_confirmation && !window.confirm(action.confirmation_prompt || 'Proceed with this action?')) return false;
    const payload = isMap(action.payload) ? { ...action.payload } : {};
    if (action.mode) payload.mode = action.mode;
    if (action.issue_id) payload.issue_id = action.issue_id;
    if (action.max_cycles) payload.max_cycles = action.max_cycles;
    if (action.source === 'worker') {
      const { source: _source, ...workerPayload } = action;
      return workerAction(workerPayload);
    }
    return plannerAction(action.type, payload);
  }, [plannerAction, workerAction]);

  const switchRuntime = useCallback(async (provider: string, model: string): Promise<boolean> => {
    if (state.status !== 'running') {
      setRuntimeState((current) => ({ ...current, provider, model }));
      return true;
    }
    const ok = await run('runtime_switch', () => window.workbench.agent.reconfigureRuntime(provider, model));
    if (ok) {
      setRuntimeState((current) => ({ ...current, provider, model }));
      await loadRuntimeOptions(provider, model);
    }
    return ok;
  }, [loadRuntimeOptions, run, state.status]);

  const setBackoff = useCallback(async (enabled: boolean, tokenLimitK: number): Promise<boolean> => {
    if (!(await ensureRunning())) return false;
    const ok = await run('configure_backoff', async () => {
      const response = await window.workbench.agent.configureBackoff(enabled, tokenLimitK);
      setBackoffState(response.backoff || { enabled, token_limit_k: tokenLimitK, window_tokens_used: 0 });
      return response;
    });
    return ok;
  }, [ensureRunning, run]);

  const value = useMemo<AgentWorkspaceValue>(() => ({
    state,
    runtime,
    backoff,
    setRuntime: (next) => setRuntimeState((current) => ({ ...current, ...next })),
    start,
    stop: async () => { await window.workbench.agent.stop(); dispatch({ type: 'reset' }); },
    submit,
    plannerAction,
    createIssue,
    workerAction,
    runSuggestedAction,
    switchRuntime,
    setBackoff,
    clearNotice: () => dispatch({ type: 'notice', message: '' }),
  }), [backoff, createIssue, plannerAction, runSuggestedAction, setBackoff, start, state, submit, switchRuntime, workerAction, runtime]);

  return <AgentWorkspaceContext.Provider value={value}>{children}</AgentWorkspaceContext.Provider>;
}

function isMap(value: unknown): value is JsonMap {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function cleanError(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '');
}
