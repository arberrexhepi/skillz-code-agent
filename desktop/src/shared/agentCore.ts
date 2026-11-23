import type { AgentBridgeState, AgentProgressMessage, AgentTranscriptEntry, DiagnosticItem, DiscoveryResult, GoalExecutionResult, IssueSummary, LatestReview, PlannerPlan, RuntimeOptionsPayload, SuggestedAction } from './agentTypes';
import { conversationTimeline, latestTurnThought } from './agentTimeline';

export type AgentStatus = 'stopped' | 'starting' | 'running' | 'error';
export type LiveAgentProgress = AgentProgressMessage & { modelCallStartedAt?: number; lastModelUpdateAt?: number; draftHasCommands?: boolean };

export interface AgentUiState {
  bridge: AgentBridgeState;
  status: AgentStatus;
  activity: AgentProgressMessage[];
  runtimeOptions?: RuntimeOptionsPayload;
  notice: string;
  pendingAction: string;
  turnThought?: AgentProgressMessage;
  liveProgress?: LiveAgentProgress;
  reasoningSummary?: AgentProgressMessage;
  draftStrategy?: string[];
}

export const initialAgentUiState: AgentUiState = {
  bridge: { planner: {}, transcript: [] },
  status: 'stopped',
  activity: [],
  notice: '',
  pendingAction: '',
};

export type AgentUiAction =
  | { type: 'bridge-state'; state: AgentBridgeState }
  | { type: 'progress'; progress: AgentProgressMessage; receivedAt?: number }
  | { type: 'status'; status: AgentStatus; message?: string }
  | { type: 'notice'; message: string }
  | { type: 'pending'; action: string }
  | { type: 'runtime-options'; options: RuntimeOptionsPayload }
  | { type: 'reset' };

export function reduceAgentUi(state: AgentUiState, action: AgentUiAction): AgentUiState {
  switch (action.type) {
    case 'bridge-state': return { ...state, bridge: action.state };
    case 'progress': {
      const { progress } = action;
      const bridge = progress.state || state.bridge;
      const live = state.liveProgress;
      const sameModelCall = (live?.action_type === 'model_call_start' || live?.action_type === 'model_response_received')
        && live.turn === progress.turn && live.domain === progress.domain;
      switch (progress.action_type) {
        case 'model_reasoning_summary': {
          if (!sameModelCall) return state;
          // Provider retries send an empty delta to clear the interrupted summary.
          const reasoningSummary = progress.thought
            ? { ...progress, thought: (state.reasoningSummary?.thought || '') + progress.thought }
            : undefined;
          const liveProgress = reasoningSummary ? { ...live, lastModelUpdateAt: action.receivedAt }
            : { ...live, output_started: false, output_chars: undefined, draftHasCommands: false, lastModelUpdateAt: undefined,
              modelCallStartedAt: action.receivedAt ?? live.modelCallStartedAt };
          return { ...state, reasoningSummary, liveProgress, draftStrategy: progress.thought ? state.draftStrategy : undefined };
        }
        case 'model_output_start':
          // Older workers report the start of a response without subsequent counts.
          return sameModelCall ? { ...state, liveProgress: { ...live, output_started: true,
            lastModelUpdateAt: undefined } } : state;
        case 'model_output_progress':
          return sameModelCall ? { ...state, liveProgress: { ...live, output_started: true,
            output_chars: progress.output_chars, lastModelUpdateAt: action.receivedAt,
            draftHasCommands: live.draftHasCommands || progress.draft_strategy?.some(entry => !entry.startsWith('>>')) },
            draftStrategy: progress.draft_strategy?.length
              ? [...(state.draftStrategy || []), ...progress.draft_strategy] : state.draftStrategy } : state;
        case 'model_response_received':
          return sameModelCall ? { ...state, liveProgress: { ...live, ...progress } } : state;
        case 'tool_start':
          return { ...state, liveProgress: progress };
        case 'tool_finish':
          return live?.action_type === 'tool_start' && live.turn === progress.turn && live.domain === progress.domain
            ? { ...state, liveProgress: undefined } : state;
      }
      const turnThought = latestTurnThought([...(state.turnThought ? [state.turnThought] : []), progress]);
      const next = { ...state, bridge, turnThought, activity: [...state.activity, progress].slice(-250) };
      if (progress.action_type === 'goal_handoff') return { ...next, liveProgress: progress };
      if (progress.action_type === 'model_call_start') {
        return { ...next, liveProgress: { ...progress, modelCallStartedAt: action.receivedAt },
          reasoningSummary: undefined, draftStrategy: undefined };
      }
      if (progress.type === 'goal_start'
        || ['discovery_start', 'discovery_mode_selected', 'plan_execution_start'].includes(progress.action_type || '')) {
        return { ...next, liveProgress: undefined, reasoningSummary: undefined, draftStrategy: undefined };
      }
      if (sameModelCall && ['model_call_error', 'model_call_interrupted'].includes(progress.action_type || '')) {
        return { ...next, liveProgress: undefined, reasoningSummary: undefined, draftStrategy: undefined };
      }
      if ((sameModelCall && progress.action_type === 'model_call_finish') || progress.type === 'goal_finish') {
        return { ...next, liveProgress: undefined, draftStrategy: progress.ok === false ? undefined : state.draftStrategy };
      }
      return next;
    }
    case 'status': return { ...state, status: action.status, notice: action.message || (action.status === 'running' ? '' : state.notice),
      liveProgress: action.status === 'running' ? state.liveProgress : undefined };
    case 'notice': return { ...state, notice: action.message };
    case 'pending': return { ...state, pendingAction: action.action, liveProgress: undefined,
      reasoningSummary: action.action ? undefined : state.reasoningSummary,
      draftStrategy: action.action ? undefined : state.draftStrategy,
      turnThought: action.action ? undefined : state.turnThought };
    case 'runtime-options': return { ...state, runtimeOptions: action.options };
    case 'reset': return initialAgentUiState;
  }
}

const quietAfterSeconds = 5;

export function liveProgressLabel(progress: LiveAgentProgress, hasSummary: boolean, updateAge = 0): string {
  if (progress.action_type === 'tool_start') return progress.summary || 'Running tool';
  if (progress.action_type === 'goal_handoff') return progress.summary || 'Carrying forward prior goal results';
  if (progress.action_type === 'model_response_received') return 'Checking commands before dispatch';
  const response = progress.domain === 'planner' ? 'planner response' : 'command response';
  const quiet = updateAge >= quietAfterSeconds && (!progress.output_started || progress.output_chars !== undefined);
  const quietResponse = progress.draftHasCommands ? 'Waiting for model to finish response' : 'Waiting for more response text';
  const status = progress.output_started ? (quiet ? quietResponse : 'Receiving ' + response)
    : hasSummary ? (quiet ? 'Waiting for ' + response : 'Receiving reasoning summary') : 'Waiting for model';
  return progress.phase ? progress.phase + ' · ' + status : status;
}

export function modelProgressDetail(progress: LiveAgentProgress, updateAge = 0): string {
  const count = progress.output_chars;
  if (progress.output_started && count === undefined) return 'Response counts will appear in a fresh agent session.';
  const received = count ? count.toLocaleString() + (count === 1 ? ' character received' : ' characters received') : '';
  const lastUpdate = updateAge < quietAfterSeconds || progress.action_type === 'model_response_received' ? ''
    : progress.lastModelUpdateAt !== undefined ? 'Last model update ' + updateAge + 's ago'
      : progress.modelCallStartedAt !== undefined ? 'Waiting ' + updateAge + 's for the first model update' : '';
  const effort = progress.reasoning_effort;
  let reasoning = '';
  if (effort) {
    const { value, requested } = effort;
    const display = (level: string) => level === 'none' ? 'off' : level;
    if (value === null) {
      reasoning = 'Model default effort' + (requested === 'auto' ? '' : ` (${display(requested)} requested)`);
    } else if (typeof value === 'number') {
      reasoning = value < 0 ? 'Dynamic reasoning budget' : `Reasoning budget ${value.toLocaleString()} tokens`;
    } else {
      reasoning = `Reasoning effort ${display(value)}` + (value === requested ? '' : ` (${display(requested)} requested)`);
    }
  }
  return [reasoning, received, lastUpdate].filter(Boolean).join(' · ');
}

export function combineSuggestedActions(state: AgentBridgeState): SuggestedAction[] {
  return [
    ...(state.planner.suggested_next_actions || []).map((action) => ({ ...action, source: 'planner' as const })),
    ...(state.planner.worker_state?.suggested_next_actions || []).map((action) => ({ ...action, source: 'worker' as const })),
  ];
}

export function groupDiagnostics(state: AgentBridgeState): Record<string, DiagnosticItem[]> {
  const latest = state.planner.worker_state?.latest_diagnostics;
  const diagnostics = [...(latest?.diagnostics || []), ...(state.planner.worker_state?.active_error?.diagnostics || [])];
  const grouped: Record<string, DiagnosticItem[]> = {};
  for (const item of diagnostics) {
    const path = String(item.path || latest?.path || '').trim();
    if (path) (grouped[path] ||= []).push(item);
  }
  return grouped;
}

export function currentReview(state: AgentBridgeState): LatestReview | null {
  return state.planner.worker_state?.latest_review || null;
}

export function primaryReviewPath(review?: LatestReview | null): string | undefined {
  if (review?.path) return review.path;
  for (const file of review?.files || []) {
    if (typeof file === 'string' && file.trim()) return file;
    if (typeof file === 'object' && file.path?.trim()) return file.path;
  }
  return undefined;
}

export function issueState(state: AgentBridgeState): { active?: IssueSummary; open: IssueSummary[]; reopenable: IssueSummary[]; totalFacts: number } {
  const worker = state.planner.worker_state;
  const source = worker?.issue_state || (state.planner.issue_state as WorkerStateLike | undefined) || {};
  const active = source.active_issue || worker?.issue_context?.active_durable_issue || undefined;
  const open = (source.open_issues || source.issues || [])
    .filter((item) => !item.status || item.status === 'open')
    .filter((item) => item.issue_id !== active?.issue_id);
  return { active: active || undefined, open, reopenable: source.reopenable_issues || [], totalFacts: Number(source.total_fact_count || 0) };
}

interface WorkerStateLike {
  active_issue?: IssueSummary | null;
  issues?: IssueSummary[];
  open_issues?: IssueSummary[];
  reopenable_issues?: IssueSummary[];
  total_fact_count?: number;
}

export function continuousIsActive(state: AgentBridgeState): boolean {
  const continuous = state.planner.continuous_mode;
  const status = String(continuous?.status || '');
  return Boolean(continuous?.enabled || (status && !['idle', 'stopped'].includes(status)));
}

export interface AgentHandoff {
  discovery: DiscoveryResult | null;
  nextSteps: string[];
  goalResults: GoalExecutionResult[];
  executionSummary: string;
  plan: PlannerPlan | null;
  executionState: 'executing' | 'paused' | 'complete' | 'idle';
  completedGoalCount: number;
  totalGoalCount: number;
  currentGoalTitle: string;
}

export function agentHandoff(state: AgentBridgeState): AgentHandoff {
  const planner = state.planner;
  const plan = planner.pending_plan || planner.paused_plan || planner.last_completed_plan || planner.last_presented_plan || null;
  const goalResults = planner.executing
    ? planner.completed_results || []
    : planner.execution_paused
      ? [...(planner.paused_completed_results || []), ...(planner.completed_results?.length ? planner.completed_results : planner.last_completed_results || []).filter((result) => result.status !== 'completed')]
      : planner.last_completed_results?.length
        ? planner.last_completed_results
        : planner.completed_results || [];
  const executionState = planner.executing ? 'executing'
    : planner.execution_paused ? 'paused'
      : planner.last_completed_plan || planner.last_execution_summary ? 'complete'
        : 'idle';
  const preview = (plan?.next_steps_preview || []).filter((step) => step.trim());
  const suggested = combineSuggestedActions(state)
    .filter((action) => !['reset_session', 'delete_session', 'create_issue'].includes(action.type))
    .map((action) => String(action.label || action.type).trim())
    .filter(Boolean);
  const nextSteps = executionState === 'executing'
    ? []
    : executionState === 'complete' || executionState === 'paused'
      ? (planner.last_next_steps || []).filter((step) => step.trim())
      : [...new Set(preview.length ? preview : suggested)].slice(0, 4);
  return {
    discovery: planner.last_discovery || null,
    nextSteps,
    goalResults,
    executionSummary: executionState === 'executing' ? '' : String(planner.last_execution_summary || '').trim(),
    plan,
    executionState,
    completedGoalCount: goalResults.filter((result) => result.status === 'completed').length,
    totalGoalCount: Number(planner.executing_goal_count || plan?.goals?.length || goalResults.length || 0),
    currentGoalTitle: String(planner.executing_goal_title || '').trim(),
  };
}

export function presentationTranscript(state: AgentBridgeState): AgentTranscriptEntry[] {
  return conversationTimeline(state).flatMap((item) => item.kind === 'message' ? [item.entry] : []);
}

export function describeProgress(item: AgentProgressMessage): { title: string; detail: string; tone: 'good' | 'bad' | 'neutral' } {
  const action = String(item.action_type || item.type).replaceAll('_', ' ');
  const title = item.type === 'goal_start' ? `Started ${item.summary || item.thought || 'goal'}`
    : item.type === 'goal_finish' ? `Finished ${item.summary || item.thought || 'goal'}`
      : item.summary || item.thought || action;
  const detail = [item.path, item.command, item.elapsed_s !== undefined ? `${item.elapsed_s}s` : ''].filter(Boolean).join(' · ');
  return { title: String(title), detail, tone: item.ok === false ? 'bad' : item.ok === true ? 'good' : 'neutral' };
}
